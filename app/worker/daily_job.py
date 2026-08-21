"""날짜별(멀티콜) 영농일지 생성 작업 1건 — generate_job 의 daily 버전.

멤버 call 들의 저장된 세그먼트(call_audio.segments_json)를 merge_calls 로 재병합해 하나의 전사를 만들고,
합성 CallContext(call_id=diary_id, hints.diary_date=대상 날짜)로 기존 파이프라인을 그대로 돌린다.
산출물은 작물별 영농일지만 저장하고 보고서는 버린다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from ..agents.interface import PipelineEmpty
from ..clients.callback import send_callback
from ..db import repo
from ..db.models import Call, CallAudio, DailyDiary, utcnow
from ..runtime import Runtime
from ..schemas.pipeline import CallContext, CallHints, Participant, PipelineResult
from ..services.artifacts import persist_daily_result
from ..services.results import utc
from ..services.transcripts import merge_calls, transcript_markdown

log = logging.getLogger(__name__)


def build_daily_context(dd: DailyDiary, calls: list[Call]) -> CallContext:
    ordered = sorted(calls, key=lambda c: (c.started_at is None,
                                           c.started_at.timestamp() if c.started_at else 0.0, c.call_id))
    hints_raw: dict[str, Any] = {}
    if isinstance(dd.metadata_json, dict) and isinstance(dd.metadata_json.get("hints"), dict):
        hints_raw = dict(dd.metadata_json["hints"])
    hints_raw["diary_date"] = dd.diary_date   # 집계 날짜로 고정 (diary_date_for 가 최우선으로 사용)
    hints = CallHints(**{k: v for k, v in hints_raw.items() if k in CallHints.model_fields})

    participants: list[Participant] = []
    for c in ordered:
        if c.participants_json:
            participants = [Participant(**p) for p in c.participants_json]
            break
    farm = next((c.farm_json for c in ordered if c.farm_json is not None), None)
    started = next((c.started_at for c in ordered if c.started_at is not None), None)
    ended = next((c.ended_at for c in reversed(ordered) if c.ended_at is not None), None)

    metadata: dict[str, Any] = dict(dd.metadata_json or {})
    metadata["daily"] = {"diary_date": dd.diary_date, "call_ids": [c.call_id for c in ordered],
                         "call_count": len(ordered)}
    return CallContext(
        call_id=dd.diary_id, started_at=utc(started), ended_at=utc(ended),
        participants=participants, farm=farm, metadata=metadata,
        farm_access_token=dd.farm_access_token,
        language=dd.language or "ko", generation_run=dd.generation_run, hints=hints,
    )


async def _finalize_daily(rt: Runtime, diary_id: str, *, status: str, error_code: str | None = None,
                          error_message: str | None = None, model: str | None = None,
                          warnings: list[str] | None = None, usage: dict[str, Any] | None = None,
                          speaker_map: dict[str, str] | None = None) -> DailyDiary:
    async with rt.db.session() as s:
        dd = await repo.get_daily(s, diary_id)
        assert dd is not None
        dd.status = status
        dd.gen_state = "IDLE"
        dd.gen_next_attempt_at = None
        dd.generation_finished_at = utcnow()
        dd.error_code, dd.error_message = error_code, error_message
        if model is not None:
            dd.generation_model = model
        if warnings is not None:
            dd.generation_warnings_json = warnings
        if usage is not None:
            dd.usage_json = usage
        if speaker_map is not None:
            dd.speaker_map_json = speaker_map
        if rt.settings.token_purge_on_terminal:
            dd.farm_access_token = None
        await repo.add_event(s, diary_id, "daily_gen_finished", {"status": status, "error": error_code})
        await s.commit()
        await s.refresh(dd)
        return dd


async def _daily_callback(rt: Runtime, dd: DailyDiary) -> None:
    if not (rt.settings.callback_enabled and dd.callback_url):
        return
    payload = {"daily_diary_id": dd.diary_id, "diary_date": dd.diary_date, "status": dd.status,
               "error": {"code": dd.error_code, "message": dd.error_message} if dd.error_code else None,
               "call_ids": list(dd.call_ids_json or []),
               "result_url": f"{rt.settings.public_base_url.rstrip('/')}/v1/daily-diaries/{dd.diary_id}",
               "generation_run": dd.generation_run}
    ok, attempts = await send_callback(rt.settings, dd.callback_url, payload)
    async with rt.db.session() as s:
        d = await repo.get_daily(s, dd.diary_id)
        if d is not None:
            d.callback_status = "SENT" if ok else "FAILED"
            d.callback_attempts = (d.callback_attempts or 0) + attempts
            await repo.add_event(s, dd.diary_id, "daily_callback", {"ok": ok, "attempts": attempts})
            await s.commit()


async def run_daily_generate(rt: Runtime, diary_id: str) -> None:
    settings = rt.settings
    async with rt.db.session() as s:
        dd = await repo.get_daily(s, diary_id)
        if dd is None or dd.gen_state != "RUNNING":
            return
        dd.generation_run += 1
        dd.generation_attempts += 1
        dd.status = "PROCESSING"
        run_no, attempt = dd.generation_run, dd.generation_attempts
        await repo.add_event(s, diary_id, "daily_gen_started", {"run": run_no, "attempt": attempt})
        await s.commit()
        await s.refresh(dd)
        call_ids = list(dd.call_ids_json or [])
        calls = await repo.get_calls_by_ids(s, call_ids)
        per_call: list[tuple[Call, list[CallAudio]]] = []
        for c in calls:
            audios = await repo.list_audio(s, c.call_id)
            done = [a for a in audios if a.status in ("TRANSCRIBED", "FAILED")]
            if done:
                per_call.append((c, done))
        ctx = build_daily_context(dd, calls)

    log.info("[%s] daily generation start (run %d, attempt %d, %d calls)", diary_id, run_no, attempt, len(calls))
    warnings = [f"call {cid} missing" for cid in call_ids if cid not in {c.call_id for c in calls}]
    for c, done in per_call:
        warnings += [f"call {c.call_id} audio#{a.id} STT failed: {a.last_error}" for a in done if a.status == "FAILED"]
    if not any(a.status == "TRANSCRIBED" for _, done in per_call for a in done):
        d = await _finalize_daily(rt, diary_id, status="EMPTY", error_code="NO_TRANSCRIPT",
                                  error_message="no transcribed audio in member calls", warnings=warnings)
        await _daily_callback(rt, d)
        return

    transcript = merge_calls(diary_id, per_call)
    tkey = rt.s3.keys.daily_transcript_json(diary_id)
    try:
        await rt.s3.put_json(tkey, transcript.model_dump(mode="json"))
        await rt.s3.put_text(rt.s3.keys.daily_transcript_md(diary_id), transcript_markdown(transcript))
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] daily transcript put failed: %s", diary_id, e)

    if transcript.is_empty:
        d = await _finalize_daily(rt, diary_id, status="EMPTY", error_code="NO_TRANSCRIPT",
                                  error_message="no speech recognised", warnings=warnings)
        await _daily_callback(rt, d)
        return

    try:
        result: PipelineResult = await asyncio.wait_for(rt.pipeline.run(transcript, ctx), settings.gen_timeout_sec)
    except PipelineEmpty as e:
        d = await _finalize_daily(rt, diary_id, status="EMPTY", error_code="NO_CONTENT", error_message=str(e) or None,
                                  warnings=warnings)
        await _daily_callback(rt, d)
        return
    except Exception as e:  # noqa: BLE001 — 타임아웃 포함
        msg = f"{type(e).__name__}: {e}"[:1000]
        log.exception("[%s] daily pipeline failed (attempt %d)", diary_id, attempt)
        async with rt.db.session() as s:
            dd = await repo.get_daily(s, diary_id)
            assert dd is not None
            if attempt < settings.gen_max_attempts:
                dd.gen_state = "QUEUED"
                dd.gen_next_attempt_at = utcnow() + timedelta(seconds=60)
                dd.generation_run -= 1  # 실패한 시도는 run 번호를 소비하지 않음
                await repo.add_event(s, diary_id, "daily_gen_retry", {"error": msg})
                await s.commit()
                return
            await s.commit()
        d = await _finalize_daily(rt, diary_id, status="FAILED", error_code="GENERATION_FAILED", error_message=msg,
                                  warnings=warnings)
        await _daily_callback(rt, d)
        return

    # 성공 — 산출물 저장 (daily 는 작물별 일지만: result.report 는 버린다)
    if result.report is not None:
        log.info("[%s] discarding report artifact (daily scope is diary-only)", diary_id)
    if result.speaker_map:
        try:
            await rt.s3.put_text(rt.s3.keys.daily_transcript_md(diary_id),
                                 transcript_markdown(transcript, result.speaker_map))
        except Exception:  # noqa: BLE001
            pass
    all_warnings = warnings + list(result.warnings or [])
    async with rt.db.session() as s:
        dd = await repo.get_daily(s, diary_id)
        assert dd is not None
        await persist_daily_result(rt, s, dd, result, tkey)
        await s.commit()
    d = await _finalize_daily(rt, diary_id, status="COMPLETED", model=result.model, warnings=all_warnings,
                              usage=result.usage or None, speaker_map=result.speaker_map)
    log.info("[%s] daily generation COMPLETED: %d diaries", diary_id, len(result.diaries))
    await _daily_callback(rt, d)
