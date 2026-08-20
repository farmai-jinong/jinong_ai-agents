"""GET 응답 조립 (CallDetail / AudioAck / 목록)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..db import repo
from ..db.models import Call, CallAudio
from ..schemas.calls import (
    AudioAck,
    AudioView,
    CallDetail,
    CallListItem,
    ErrorView,
    GenerationView,
    SttProgress,
)
from ..schemas.pipeline import Participant
from .artifacts import build_result_view


def utc(dt: datetime | None) -> datetime | None:
    """SQLite 는 tz 를 버린다 → naive 는 UTC 로 간주해 tz 를 붙여 응답."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def audio_view(a: CallAudio) -> AudioView:
    return AudioView(id=a.id, bucket=a.bucket, key=a.key, seq=a.seq, recorded_at=utc(a.recorded_at), status=a.status,
                     attempts=a.attempts, duration_sec=a.duration_sec, stt_seconds=a.stt_seconds,
                     offset_sec=a.offset_sec, stt_raw_key=a.stt_raw_key, segments_count=a.segments_count,
                     last_error=a.last_error)


def progress_of(audio: list[CallAudio]) -> SttProgress:
    p = SttProgress(total=len(audio))
    for a in audio:
        if a.status == "TRANSCRIBED":
            p.transcribed += 1
        elif a.status == "FAILED":
            p.failed += 1
        else:
            p.pending += 1
    return p


async def call_detail(s: AsyncSession, call: Call, *, inline: bool = True, note: str | None = None) -> CallDetail:
    audio = await repo.list_audio(s, call.call_id)
    artifacts = await repo.list_artifacts(s, call.call_id)
    return CallDetail(
        call_id=call.call_id, state=call.state, status=call.status,
        started_at=utc(call.started_at), ended_at=utc(call.ended_at), duration_sec=call.duration_sec,
        created_at=utc(call.created_at), updated_at=utc(call.updated_at),
        participants=[Participant(**p) for p in (call.participants_json or [])],
        farm=call.farm_json, metadata=call.metadata_json, stale=call.stale, note=note,
        stt_progress=progress_of(audio), audio=[audio_view(a) for a in audio],
        generation=GenerationView(run=call.generation_run, attempts=call.generation_attempts, state=call.gen_state,
                                  started_at=utc(call.generation_started_at), finished_at=utc(call.generation_finished_at),
                                  model=call.generation_model, warnings=list(call.generation_warnings_json or []),
                                  usage=call.usage_json),
        error=ErrorView(code=call.error_code, message=call.error_message) if call.error_code else None,
        result=build_result_view(call, artifacts, inline=inline),
        callback_status=call.callback_status,
    )


async def audio_ack(s: AsyncSession, call: Call, a: CallAudio, note: str | None) -> AudioAck:
    audio = await repo.list_audio(s, call.call_id)
    return AudioAck(call_id=call.call_id, audio=audio_view(a), call_status=call.status, call_state=call.state,
                    stt_progress=progress_of(audio), note=note)


def list_item(call: Call) -> CallListItem:
    return CallListItem(call_id=call.call_id, state=call.state, status=call.status, updated_at=utc(call.updated_at),
                        stt_progress=progress_of(list(call.audio)))
