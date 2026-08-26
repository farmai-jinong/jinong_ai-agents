"""생성 작업 1건: 전사 병합 → S3 → 파이프라인 → 산출물 저장 → 상태 확정 → 콜백."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from ..agents.interface import PipelineEmpty
from ..clients.callback import send_callback
from ..db import repo
from ..db.models import Call, utcnow
from ..runtime import Runtime
from ..schemas.pipeline import CallContext, CallHints, DiaryArtifact, Participant, PipelineResult
from ..services.artifacts import persist_result
from ..services.results import utc
from ..services.transcripts import merge_transcripts, transcript_markdown

log = logging.getLogger(__name__)


def build_context(call: Call) -> CallContext:
    hints_raw: dict[str, Any] = {}
    if isinstance(call.metadata_json, dict) and isinstance(call.metadata_json.get("hints"), dict):
        hints_raw = call.metadata_json["hints"]
    hints = CallHints(**{k: v for k, v in hints_raw.items() if k in CallHints.model_fields})
    return CallContext(
        # SQLite 는 tz 를 버린다 → naive=UTC 로 재태깅 (렌더러는 naive 를 KST 로 오해)
        call_id=call.call_id, started_at=utc(call.started_at), ended_at=utc(call.ended_at),
        participants=[Participant(**p) for p in (call.participants_json or [])],
        farm=call.farm_json, metadata=call.metadata_json, farm_access_token=call.farm_access_token,
        language=call.language or "ko", generation_run=call.generation_run, hints=hints,
    )


async def _finalize(rt: Runtime, call_id: str, *, status: str, error_code: str | None = None,
                    error_message: str | None = None, model: str | None = None,
                    warnings: list[str] | None = None, usage: dict[str, Any] | None = None,
                    speaker_map: dict[str, str] | None = None) -> Call:
    async with rt.db.session() as s:
        call = await repo.get_call(s, call_id)
        assert call is not None
        call.status = status
        call.gen_state = "IDLE"
        call.gen_next_attempt_at = None
        call.generation_finished_at = utcnow()
        call.error_code, call.error_message = error_code, error_message
        if model is not None:
            call.generation_model = model
        if warnings is not None:
            call.generation_warnings_json = warnings
        if usage is not None:
            call.usage_json = usage
        if speaker_map is not None:
            call.speaker_map_json = speaker_map
        if rt.settings.token_purge_on_terminal:
            call.farm_access_token = None
        await repo.add_event(s, call_id, "gen_finished", {"status": status, "error": error_code})
        await s.commit()
        await s.refresh(call)
        return call


SUMMARY_TYPE = "SUMMARY"


def join_diaries(diaries: list[DiaryArtifact]) -> str:
    """작물별 일지 마크다운을 구분선으로 병합 — 콜백 content 는 통화당 1건이라 합쳐 보낸다."""
    return "\n\n---\n\n".join(d.markdown.strip() for d in diaries if d.markdown and d.markdown.strip())


async def _summary_callback(rt: Runtime, call: Call, result: PipelineResult | None = None) -> None:
    """백엔드 통화요약 콜백 — 일지 마크다운 원문 동봉. 컨설팅 보고서는 내부 저장 전용이라 보내지 않는다."""
    st = rt.settings
    if not (st.callback_enabled and st.summary_callback_url):
        return
    content = join_diaries(result.diaries) if result else ""
    status = call.status
    if status == "COMPLETED" and not content:   # 본문 없는 COMPLETED 는 명세 위반(content 필수)
        status = "EMPTY"
    engine = f"{st.summary_engine_version}/{call.generation_model}" if call.generation_model \
        else st.summary_engine_version
    payload: dict[str, Any] = {"call_id": call.call_id, "summary_type": SUMMARY_TYPE, "status": status,
                               "engine_version": engine[:100]}
    if status == "COMPLETED":
        payload["content"] = content
    elif status == "FAILED":
        reason = f"{call.error_code}: {call.error_message}" if call.error_message else (call.error_code or "")
        payload["fail_reason"] = reason[:1000]
    ok, attempts = await send_callback(rt.settings, st.summary_callback_url, payload)
    async with rt.db.session() as s:
        c = await repo.get_call(s, call.call_id)
        if c is not None:
            c.callback_status = "SENT" if ok else "FAILED"
            c.callback_attempts = (c.callback_attempts or 0) + attempts
            await repo.add_event(s, call.call_id, "summary_callback",
                                 {"ok": ok, "attempts": attempts, "status": status})
            await s.commit()


async def run_generate(rt: Runtime, call_id: str) -> None:
    settings = rt.settings
    async with rt.db.session() as s:
        call = await repo.get_call(s, call_id)
        if call is None or call.gen_state != "RUNNING":
            return
        call.generation_run += 1
        call.generation_attempts += 1
        call.status = "PROCESSING"
        run_no, attempt = call.generation_run, call.generation_attempts
        await repo.add_event(s, call_id, "gen_started", {"run": run_no, "attempt": attempt})
        await s.commit()
        await s.refresh(call)
        audios = await repo.list_audio(s, call_id)
        ctx = build_context(call)

    log.info("[%s] generation start (run %d, attempt %d, %d audio)", call_id, run_no, attempt, len(audios))
    done = [a for a in audios if a.status in ("TRANSCRIBED", "FAILED")]
    if not audios:
        c = await _finalize(rt, call_id, status="EMPTY", error_code="NO_AUDIO", error_message="no audio received")
        await _summary_callback(rt, c)
        return
    if not any(a.status == "TRANSCRIBED" for a in done):
        c = await _finalize(rt, call_id, status="FAILED", error_code="STT_FAILED",
                            error_message="; ".join(f"audio#{a.id}: {a.last_error}" for a in done if a.last_error)[:1000])
        await _summary_callback(rt, c)
        return

    warnings = [f"audio#{a.id} STT failed: {a.last_error}" for a in done if a.status == "FAILED"]
    transcript = merge_transcripts(call_id, done)
    # 오프셋을 행에 반영 (조회용)
    async with rt.db.session() as s:
        for f in transcript.files:
            a = await repo.get_audio(s, f.audio_id)
            if a is not None:
                a.offset_sec = f.offset_sec
        await s.commit()
    tkey = rt.s3.keys.transcript_json(call_id)
    try:
        await rt.s3.put_json(tkey, transcript.model_dump(mode="json"))
        await rt.s3.put_text(rt.s3.keys.transcript_md(call_id), transcript_markdown(transcript))
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] transcript put failed: %s", call_id, e)

    if transcript.is_empty:
        c = await _finalize(rt, call_id, status="EMPTY", error_code="NO_TRANSCRIPT",
                            error_message="no speech recognised", warnings=warnings)
        await _summary_callback(rt, c)
        return

    try:
        result: PipelineResult = await asyncio.wait_for(rt.pipeline.run(transcript, ctx), settings.gen_timeout_sec)
    except PipelineEmpty as e:
        c = await _finalize(rt, call_id, status="EMPTY", error_code="NO_CONTENT", error_message=str(e) or None,
                            warnings=warnings)
        await _summary_callback(rt, c)
        return
    except Exception as e:  # noqa: BLE001 — 타임아웃 포함
        msg = f"{type(e).__name__}: {e}"[:1000]
        log.exception("[%s] pipeline failed (attempt %d)", call_id, attempt)
        async with rt.db.session() as s:
            call = await repo.get_call(s, call_id)
            assert call is not None
            if attempt < settings.gen_max_attempts:
                call.gen_state = "QUEUED"
                call.gen_next_attempt_at = utcnow() + timedelta(seconds=60)
                call.generation_run -= 1  # 실패한 시도는 run 번호를 소비하지 않음
                await repo.add_event(s, call_id, "gen_retry", {"error": msg})
                await s.commit()
                return
            await s.commit()
        c = await _finalize(rt, call_id, status="FAILED", error_code="GENERATION_FAILED", error_message=msg,
                            warnings=warnings)
        await _summary_callback(rt, c)
        return

    # 성공 — 산출물 저장
    if transcript_markdown and result.speaker_map:
        try:
            await rt.s3.put_text(rt.s3.keys.transcript_md(call_id), transcript_markdown(transcript, result.speaker_map))
        except Exception:  # noqa: BLE001
            pass
    all_warnings = warnings + list(result.warnings or [])
    async with rt.db.session() as s:
        call = await repo.get_call(s, call_id)
        assert call is not None
        await persist_result(rt, s, call, result, tkey)
        await s.commit()
    c = await _finalize(rt, call_id, status="COMPLETED", model=result.model, warnings=all_warnings,
                        usage=result.usage or None, speaker_map=result.speaker_map)
    log.info("[%s] generation COMPLETED: %d diaries, report=%s", call_id, len(result.diaries), result.report is not None)
    # 산출물 S3 저장(persist_result) 이 끝난 뒤에만 콜백 — 백엔드가 조회할 때 키가 이미 존재한다.
    await _summary_callback(rt, c, result)
