"""백엔드 DB → 우리 `calls`/`daily_diaries` 행 갱신(참여자·농가 등록 작물 힌트). dev 전용, fail-open.

언제: STT 잡 시작(`reason="stt"`)과 **재생성(run ≥ 2, `reason="regenerate"`)**. 첫 생성(run 1)은 호출되지 않는다.
무엇을: `participants_json`(역할·이름·engn/user_id), `num_speakers`(비어 있을 때만), `metadata_json.hints` 의
`farmer_engn_id/farmer_user_id/farmer_crops`. **`farm_json`·`farm_access_token` 은 절대 건드리지 않는다**
(farm_id 의 유래 미확인, 토큰은 요청 body 로만). 결과는 `job_events`(`backend_refresh`) 와
`metadata_json["backend_refresh"]`(GET 응답 `metadata` 로 보임)에 남긴다.
"""

from __future__ import annotations

import logging
from typing import Any

from ..clients.backend_db import CONSULTANT_JOB_CODE, BackendDbError
from ..db import repo
from ..db.models import Call, DailyDiary, utcnow
from ..schemas.pipeline import Participant

log = logging.getLogger(__name__)

EVENT = "backend_refresh"
DAILY_EVENT = "daily_backend_refresh"


def participants_from_call(row: dict[str, Any]) -> tuple[list[Participant] | None, list[str]]:
    """sender/receiver → farmer/consultant. 컨설턴트 = `user_job_secode == '001001004'`(백엔드 checkIsConsultant).

    정확히 한쪽만 컨설턴트일 때만 확정한다 — 양쪽/어느 쪽도 아니면 None + 경고(payload 역할 유지).
    """
    sides = []
    for side in ("sender", "receiver"):
        engn, user = row.get(f"{side}_engn_id"), row.get(f"{side}_user_id")
        if not engn or not user:
            return None, [f"backend_db: {side} 식별자 없음"]
        sides.append({"engn_id": str(engn), "user_id": str(user), "name": row.get(f"{side}_nm") or None,
                      "job": str(row.get(f"{side}_job") or "").strip()})
    cons = [s for s in sides if s["job"] == CONSULTANT_JOB_CODE]
    if len(cons) != 1:
        why = "양쪽 모두 컨설턴트" if len(cons) == 2 else "컨설턴트 없음(업무 구분코드 미해당)"
        return None, [f"backend_db: 역할 판정 불가({why}) — payload 역할 유지"]
    out = [Participant(role="consultant" if s is cons[0] else "farmer", user_id=s["user_id"],
                       engn_id=s["engn_id"], name=s["name"]) for s in sides]
    out.sort(key=lambda p: p.role != "farmer")     # farmer 먼저(기존 payload 관례)
    return out, []


def hints_with_db(hints: dict[str, Any] | None, crops: list[dict[str, Any]], farmer_key: tuple[str, str] | None) -> dict[str, Any]:
    """기존 힌트(prdlst_code/nm·diary_date·topic 등)는 보존하고 농가 키·등록 작물만 DB 값으로 덮는다(새 dict)."""
    out = dict(hints or {})
    if farmer_key:
        out["farmer_engn_id"], out["farmer_user_id"] = farmer_key
    if crops:
        out["farmer_crops"] = [{"prdlstCode": c.get("prdlstCode"), "prdlstNm": c.get("prdlstNm"),
                                "reprsntPrdlstCnt": c.get("reprsntPrdlstCnt")} for c in crops]
    return out


def _farmer_key(participants: list[Participant], hints: dict[str, Any] | None = None) -> tuple[str, str] | None:
    """농가 복합 키 — participants 의 farmer 우선, 없으면 hints 의 farmer_engn_id/farmer_user_id(파이프라인 `farmer_key` 와 동일)."""
    for p in participants:
        if p.role == "farmer" and p.engn_id and p.user_id:
            return str(p.engn_id), str(p.user_id)
    h = hints or {}
    if h.get("farmer_engn_id") and h.get("farmer_user_id"):
        return str(h["farmer_engn_id"]), str(h["farmer_user_id"])
    return None


def _stamp(meta: dict[str, Any] | None, info: dict[str, Any], *, hints: dict[str, Any] | None = None) -> dict[str, Any]:
    out = dict(meta or {})
    if hints is not None:
        out["hints"] = hints
    out["backend_refresh"] = {**info, "at": utcnow().isoformat()}
    return out


async def refresh_call(rt, s, call: Call, *, reason: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """백엔드 DB 로 통화 1건 갱신. 세션 커밋은 호출자가. 반환 = 이벤트 detail(`found/changed/warnings`)."""
    info: dict[str, Any] = {"reason": reason, "found": False, "changed": [], "warnings": []}
    db = rt.backend_db
    if db is None:
        return info
    try:
        meta = call.metadata_json if isinstance(call.metadata_json, dict) else {}
        old_hints = meta.get("hints") if isinstance(meta.get("hints"), dict) else {}
        current = [Participant(**p) for p in (call.participants_json or []) if isinstance(p, dict)]
        row = await db.call(call.call_id)
        if row is None:
            info["warnings"].append("backend_db: 통화 행 없음 — 참여자 스냅샷 유지")
        else:
            info["found"] = True
            parts, warns = participants_from_call(row)
            info["warnings"] += warns
            if parts is not None:
                new = [p.model_dump() for p in parts]
                if new != (call.participants_json or []):
                    call.participants_json = new
                    info["changed"].append("participants")
                if not call.num_speakers:
                    call.num_speakers = len(new)
                    info["changed"].append("num_speakers")
                current = parts
        # 등록 작물은 통화 행 유무와 무관하게 농가 복합 키(참여자 → hints)로 읽는다
        key = _farmer_key(current, old_hints)
        crops: list[dict[str, Any]] = []
        if key is None:
            info["warnings"].append("backend_db: 농가 복합 키 없음 — 작물 목록 생략")
        else:
            crops = await db.farm_crops(*key)
            if not crops:
                info["warnings"].append(f"backend_db: 등록 작물 없음(engn:{key[0]})")
        new_hints = hints_with_db(old_hints, crops, key)
        if new_hints != old_hints:
            info["changed"].append("hints")
        info["crops"] = len(crops)
        call.metadata_json = _stamp(meta, info, hints=new_hints)
    except BackendDbError as e:
        info["error"] = str(e)
        info["warnings"].append(f"backend_db 조회 실패 — 스냅샷 유지: {e}")
        log.warning("[%s] backend refresh failed (%s): %s", call.call_id, reason, e)
        call.metadata_json = _stamp(call.metadata_json, info)
    except Exception as e:  # noqa: BLE001 — 갱신은 부수 경로, 생성 흐름을 막지 않는다
        info["error"] = f"{type(e).__name__}: {e}"[:200]
        info["warnings"].append("backend_db 갱신 중 예외 — 스냅샷 유지")
        log.exception("[%s] backend refresh unexpected error (%s)", call.call_id, reason)
        call.metadata_json = _stamp(call.metadata_json, info)
    await repo.add_event(s, call.call_id, EVENT, info)
    return info


async def refresh_daily(rt, s, dd: DailyDiary, calls: list[Call], *, reason: str = "regenerate") -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """멤버 call 각각 갱신 후 첫 농가 키의 등록 작물을 `dd.metadata_json.hints.farmer_crops` 에 반영."""
    info: dict[str, Any] = {"reason": reason, "calls": {}, "changed": [], "warnings": []}
    if rt.backend_db is None:
        return info
    key: tuple[str, str] | None = None
    for c in calls:
        r = await refresh_call(rt, s, c, reason=reason)
        info["calls"][c.call_id] = {"found": r.get("found"), "changed": r.get("changed")}
        if key is None:
            cm = c.metadata_json if isinstance(c.metadata_json, dict) else {}
            key = _farmer_key([Participant(**p) for p in (c.participants_json or []) if isinstance(p, dict)],
                              cm.get("hints") if isinstance(cm.get("hints"), dict) else None)
    if key is None:
        dm = dd.metadata_json if isinstance(dd.metadata_json, dict) else {}
        key = _farmer_key([], dm.get("hints") if isinstance(dm.get("hints"), dict) else None)
    crops: list[dict[str, Any]] = []
    try:
        if key is not None:
            crops = await rt.backend_db.farm_crops(*key)
        else:
            info["warnings"].append("backend_db: 멤버 통화에 농가 복합 키 없음 — 작물 목록 생략")
    except BackendDbError as e:
        info["error"] = str(e)
        info["warnings"].append(f"backend_db 작물 조회 실패 — 스냅샷 유지: {e}")
    meta = dd.metadata_json if isinstance(dd.metadata_json, dict) else {}
    old_hints = meta.get("hints") if isinstance(meta.get("hints"), dict) else {}
    new_hints = hints_with_db(old_hints, crops, key)
    if new_hints != old_hints:
        info["changed"].append("hints")
    info["crops"] = len(crops)
    dd.metadata_json = _stamp(meta, info, hints=new_hints)     # crop(작물 고정) 키는 그대로 보존된다
    await repo.add_event(s, dd.diary_id, DAILY_EVENT, info)
    return info
