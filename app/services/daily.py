"""날짜별(멀티콜) 영농일지 상태 전이 — create / 재-POST / regenerate. 멱등 규칙은 docs/api-reference.md.

백엔드가 call_id 목록을 지정해 명시적으로 트리거한다. 멤버 call 들은 이미 terminal 이어야 하며,
기존 call 단위 플로우/산출물과 별도 리소스로 공존한다.

`diary_id` 가 멱등성 키다. 같은 `diary_id` 로 다시 POST 하면 **새 생성 회차**를 돌린다(`_retrigger`) —
백엔드 자동 배치가 통화별 COMPLETED 콜백마다 그 날짜의 전체 `call_ids` 를 재전송하는 규약이기 때문.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..db import repo
from ..db.models import DailyDiary
from ..errors import ApiError
from ..runtime import Runtime
from ..schemas.daily import DailyDiaryCreateRequest, DailyRegenerateRequest

log = logging.getLogger(__name__)


@dataclass
class DailyTransition:
    daily: DailyDiary
    http_status: int
    note: str | None = None
    wake: bool = False


class DailyDiaryService:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    async def _validate_calls(self, s: Any, call_ids: list[str]) -> None:
        """멤버 call 검증 — 존재·terminal·전사 유무·같은 농가. create/재-POST 공통."""
        calls = await repo.get_calls_by_ids(s, call_ids)
        by_id = {c.call_id: c for c in calls}
        missing = [cid for cid in call_ids if cid not in by_id]
        if missing:
            raise ApiError("CALLS_NOT_FOUND", f"calls not found: {', '.join(missing)}", 422)
        not_ready = [f"{c.call_id}:{c.status}" for c in calls if c.status not in ("COMPLETED", "EMPTY")]
        if not_ready:
            raise ApiError("CALLS_NOT_READY",
                           "all calls must be terminal COMPLETED/EMPTY (FAILED calls: regenerate them first): "
                           + ", ".join(not_ready), 409)
        if not any(c.status == "COMPLETED" for c in calls):
            raise ApiError("NO_TRANSCRIBED_CALLS", "no COMPLETED call with a transcript in call_ids", 422)
        farm_ids = {str(c.farm_json.get("farm_id")) for c in calls
                    if isinstance(c.farm_json, dict) and c.farm_json.get("farm_id") is not None}
        if len(farm_ids) > 1:
            raise ApiError("FARM_MISMATCH", f"call_ids span multiple farms: {sorted(farm_ids)}", 422)
        # 농가는 (engn_id, user_id) 복합 키로 구분 — engn_id 없는 구형 콜은 위 farm_id 검사만 적용
        farmer_keys = {(str(p.get("engn_id")), str(p.get("user_id") or ""))
                       for c in calls for p in (c.participants_json or [])
                       if isinstance(p, dict) and p.get("role") == "farmer" and p.get("engn_id") is not None}
        if len(farmer_keys) > 1:
            pretty = sorted(f"engn:{e}/user:{u}" for e, u in farmer_keys)
            raise ApiError("FARM_MISMATCH", f"call_ids span multiple farmers: {', '.join(pretty)}", 422)

    async def _retrigger(self, s: Any, dd: DailyDiary, req: DailyDiaryCreateRequest) -> DailyTransition:
        """같은 `diary_id` 재-POST — 백엔드 자동 배치의 재요청 규약.

        백엔드는 통화별 COMPLETED 콜백마다 **그 날짜의 전체 `call_ids`** 를 같은 `diary_id` 로 다시 보낸다.
        terminal 이면 새 생성 회차를 돌린다(`generation_run` 은 워커가 클레임할 때 +1).
        진행 중인 실행은 이미 `call_ids` 를 읽었으므로 목록을 바꾸지 않는다 — 다음 트리거나 보정 배치가 잡는다.
        """
        if dd.gen_state == "RUNNING":
            return DailyTransition(dd, 200, note="generation in progress — re-POST after it finishes")
        await self._validate_calls(s, req.call_ids)
        dd.call_ids_json = list(req.call_ids)
        if req.metadata is not None:
            dd.metadata_json = req.metadata
        if req.callback_url is not None:
            dd.callback_url = req.callback_url
        if req.farm_access_token:            # 자동 배치는 JWT 를 보내지 않는다 — 있을 때만 갱신
            dd.farm_access_token = req.farm_access_token
        if req.language:
            dd.language = req.language
        if dd.gen_state == "QUEUED":         # 아직 클레임 전 — 대기 중인 실행이 새 목록을 읽는다
            note, wake = "already queued — call_ids updated", False
        else:                                # IDLE(terminal) — 새 생성 회차
            dd.status = "PROCESSING"
            dd.error_code = dd.error_message = None
            dd.generation_attempts = 0
            dd.gen_state = "QUEUED"
            dd.gen_next_attempt_at = None
            note, wake = "regeneration queued", True
        await repo.add_event(s, dd.diary_id, "daily_retriggered",
                             {"call_ids": list(req.call_ids), "gen_state": dd.gen_state})
        await s.commit()
        await s.refresh(dd)
        await self._put_daily_json(dd)
        return DailyTransition(dd, 200, wake=wake, note=note)

    async def create(self, req: DailyDiaryCreateRequest) -> DailyTransition:
        async with self.rt.db.session() as s:
            dd = await repo.get_daily(s, req.diary_id)
            if dd is not None:
                return await self._retrigger(s, dd, req)

            await self._validate_calls(s, req.call_ids)
            dd = DailyDiary(
                diary_id=req.diary_id, diary_date=req.diary_date, call_ids_json=list(req.call_ids),
                status="PROCESSING", gen_state="QUEUED",
                metadata_json=req.metadata, farm_access_token=req.farm_access_token,
                language=req.language or "ko", callback_url=req.callback_url,
                s3_prefix=self.rt.s3.keys.daily_base(req.diary_id),
            )
            s.add(dd)
            await repo.add_event(s, req.diary_id, "daily_created",
                                 {"diary_date": req.diary_date, "call_ids": list(req.call_ids)})
            await s.commit()
            await s.refresh(dd)
            await self._put_daily_json(dd)
            return DailyTransition(dd, 201, wake=True, note="generation queued")

    async def regenerate(self, diary_id: str, req: DailyRegenerateRequest) -> DailyTransition:
        async with self.rt.db.session() as s:
            dd = await repo.get_daily(s, diary_id)
            if dd is None:
                raise ApiError("DAILY_NOT_FOUND", f"daily diary {diary_id} not found", 404)
            if dd.gen_state != "IDLE":
                raise ApiError("ALREADY_PROCESSING", "a generation run is active", 409)
            dd.status = "PROCESSING"
            dd.error_code = dd.error_message = None
            dd.generation_attempts = 0
            dd.gen_state = "QUEUED"
            dd.gen_next_attempt_at = None
            if req.farm_access_token:
                dd.farm_access_token = req.farm_access_token
            await repo.add_event(s, diary_id, "daily_regenerate", {"reason": req.reason})
            await s.commit()
            await s.refresh(dd)
            return DailyTransition(dd, 202, wake=True)

    async def _put_daily_json(self, dd: DailyDiary) -> None:
        try:
            await self.rt.s3.put_json(self.rt.s3.keys.daily_meta_json(dd.diary_id), {
                "diary_id": dd.diary_id, "diary_date": dd.diary_date, "call_ids": dd.call_ids_json,
                "status": dd.status, "language": dd.language, "metadata": dd.metadata_json,
            })
        except Exception as e:  # noqa: BLE001 — 부수효과, 실패해도 흐름 유지
            log.warning("[%s] daily.json put failed: %s", dd.diary_id, e)
