"""생성 작업 1건: 전사 병합 → S3 → 파이프라인 → 산출물 저장 → 상태 확정 → 콜백."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from ..agents.interface import PipelineEmpty
from ..agents.summarize import summary_from_report
from ..clients.callback import send_callback
from ..db import repo
from ..db.models import Artifact, Call, utcnow
from ..runtime import Runtime
from ..schemas.pipeline import CallContext, CallHints, CallSummaryResult, DiaryArtifact, Participant, PipelineResult
from ..services.artifacts import artifact_keys, persist_result
from ..services.results import utc
from ..services.transcripts import apply_speaker_map, merge_transcripts, transcript_markdown

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
EMPTY_DIARY_STATUSES = ("EMPTY", "UNRESOLVED_CROP")
NO_DIARY_CONTENT = "NO_DIARY_CONTENT"
# 백엔드가 받는 empty_reason 허용값(백엔드 연동 문서 §6) — 이 밖의 값은 콜백에 싣지 않는다.
ALLOWED_EMPTY_REASONS = ("NO_AUDIO", "NO_TRANSCRIPT", "NO_CONTENT", NO_DIARY_CONTENT)


def has_diary_content(diaries: list[DiaryArtifact]) -> bool:
    """저장되는 일지 중 실질 내용이 있는 건이 하나라도 있는가.

    콜백 content(통화 단순요약)와 무관하게 **저장되는 일지 기준**으로 판정한다. 빈 템플릿
    (EMPTY/UNRESOLVED_CROP — 검수 강등 포함)뿐이면 요약을 만들지 않고 EMPTY 로 통보한다.
    """
    return any(d.status not in EMPTY_DIARY_STATUSES and d.markdown and d.markdown.strip() for d in diaries)


async def build_summary(rt: Runtime, transcript, ctx: CallContext, result: PipelineResult,
                        call_id: str) -> tuple[CallSummaryResult | None, list[str]]:
    """통화 단순요약 — 일지와 독립된 LLM 패스. 실패해도 생성을 막지 않는다(fail-open).

    실패 시 이미 만들어진 보고서 요약으로 폴백한다(추가 LLM 호출 없음).
    """
    if rt.summarizer is None:
        return None, []
    try:
        summary = await asyncio.wait_for(rt.summarizer.summarize(transcript, ctx), rt.settings.node_timeout_s)
        if summary.markdown.strip():
            return summary, []
        warn = "통화 단순요약이 비어 보고서 요약으로 대체"
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] call summary failed: %s", call_id, e)
        warn = f"통화 단순요약 실패({type(e).__name__}) — 보고서 요약으로 대체"
    fallback = summary_from_report(result.report.structured if result.report else None)
    return fallback, [warn if fallback else warn.replace("보고서 요약으로 대체", "대체 불가")]


async def _summary_callback(rt: Runtime, call: Call, result: PipelineResult | None = None,
                            summary_md: str | None = None, artifacts: list[Artifact] | None = None) -> None:
    """백엔드 통화요약 콜백 — content 는 **통화 단순요약**(불릿).

    영농일지·컨설팅 보고서 **본문**은 싣지 않는다. 일지는 `GET /v1/calls/{id}` 의 `result.diaries[]`,
    전사는 `/transcript` 로 조회한다 — 이 콜백 도착이 그 결과가 준비됐다는 신호를 겸한다.
    COMPLETED 에는 산출물 S3 키(`diaries[]`/`report`, 전달용 + 근거 포함 내부용)만 같이 싣는다
    (`CALLBACK_INCLUDE_ARTIFACT_KEYS`).
    """
    st = rt.settings
    if not (st.callback_enabled and st.summary_callback_url):
        return
    content = (summary_md or "").strip()
    status = call.status
    empty_reason = call.error_code if call.error_code in ALLOWED_EMPTY_REASONS else NO_DIARY_CONTENT
    # 일지 유무는 저장되는 산출물 기준으로 판정한다(콜백 content 와 무관).
    if status == "COMPLETED" and result is not None and not has_diary_content(result.diaries):
        status, empty_reason = "EMPTY", NO_DIARY_CONTENT
    elif status == "COMPLETED" and not content:   # 본문 없는 COMPLETED 는 명세 위반(content 필수)
        # 일지는 있는데 요약이 폴백까지 실패한 경우 — 백엔드 허용값에 맞춰 NO_CONTENT 로 접는다.
        status, empty_reason = "EMPTY", "NO_CONTENT"
    engine = f"{st.summary_engine_version}/{call.generation_model}" if call.generation_model \
        else st.summary_engine_version
    payload: dict[str, Any] = {"call_id": call.call_id, "summary_type": SUMMARY_TYPE, "status": status,
                               "engine_version": engine[:100]}
    if status == "COMPLETED":
        payload["content"] = content
    elif status == "EMPTY":
        payload["empty_reason"] = empty_reason   # ALLOWED_EMPTY_REASONS 중 하나
    elif status == "FAILED":
        reason = f"{call.error_code}: {call.error_message}" if call.error_message else (call.error_code or "")
        payload["fail_reason"] = reason[:1000]
    # 화자 역할표 — 글자 A/B 만으로는 누가 농가인지 알 수 없으므로 추정 결과를 같이 싣는다(선택 필드).
    # 이번 실행의 결과가 있을 때만 — 이른 종료(NO_AUDIO/STT_FAILED)에서는 직전 run 의 값이 남아 있을 수 있다.
    if st.callback_include_speaker_map and result is not None and call.speaker_map_json:
        payload["speaker_map"] = dict(call.speaker_map_json)
    # 산출물 키 — 이번 실행에서 저장된 행 기준, COMPLETED 에만(EMPTY/FAILED 는 직전 run 의 산출물이 남아 있을 수 있다).
    if st.callback_include_artifact_keys and status == "COMPLETED" and artifacts:
        payload.update(artifact_keys(artifacts))
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

    # 성공 — 산출물 저장. 추정된 역할(농가/컨설턴트)을 전사에 되먹여 merged.json/md 를 다시 쓴다
    # (처음 쓸 때는 아직 역할을 모른다 — 같은 키를 덮어쓰므로 GET /transcript 가 곧바로 role 을 본다).
    if result.speaker_map:
        transcript = apply_speaker_map(transcript, result.speaker_map)
        try:
            await rt.s3.put_json(tkey, transcript.model_dump(mode="json"))
            await rt.s3.put_text(rt.s3.keys.transcript_md(call_id), transcript_markdown(transcript))
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] transcript rewrite with roles failed: %s", call_id, e)
    all_warnings = warnings + list(result.warnings or [])
    # 통화 단순요약 — 일지가 실질 내용을 가질 때만 만든다(잡담 통화에 LLM 을 쓰지 않는다).
    summary: CallSummaryResult | None = None
    if has_diary_content(result.diaries):
        summary, sum_warnings = await build_summary(rt, transcript, ctx, result, call_id)
        all_warnings += sum_warnings
    async with rt.db.session() as s:
        call = await repo.get_call(s, call_id)
        assert call is not None
        arts = await persist_result(rt, s, call, result, tkey, summary=summary)
        await s.commit()
    c = await _finalize(rt, call_id, status="COMPLETED", model=result.model, warnings=all_warnings,
                        usage=result.usage or None, speaker_map=result.speaker_map)
    log.info("[%s] generation COMPLETED: %d diaries, report=%s", call_id, len(result.diaries), result.report is not None)
    # 산출물 S3 저장(persist_result) 이 끝난 뒤에만 콜백 — 백엔드가 조회할 때 키가 이미 존재한다.
    await _summary_callback(rt, c, result, summary.markdown if summary else None, artifacts=arts)
