"""통화 상태 전이 — start / audio / end / regenerate. 멱등 규칙은 docs/api-reference.md.

라우트는 이 서비스만 호출하고, 워커 wake 는 반환값(`Transition.wake`) 로 신호한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import update

from ..clients.s3 import S3Error
from ..clients.storage import StorageClient
from ..config import Settings
from ..db import repo
from ..db.models import Call, CallAudio, utcnow
from ..errors import ApiError
from ..runtime import Runtime
from ..schemas.calls import AudioReceivedRequest, CallEndRequest, CallStartRequest, RegenerateRequest

log = logging.getLogger(__name__)


@dataclass
class Transition:
    call: Call
    http_status: int
    note: str | None = None
    wake: bool = False
    audio: CallAudio | None = None


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class CallService:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.settings: Settings = rt.settings
        self.s3: StorageClient = rt.s3

    # --- start ---------------------------------------------------------
    async def start(self, req: CallStartRequest) -> Transition:
        async with self.rt.db.session() as s:
            call = await repo.get_call(s, req.call_id)
            created = call is None
            if call is None:
                call = Call(call_id=req.call_id, s3_prefix=self.s3.keys.base(req.call_id))
                s.add(call)
            if call.status in repo.TERMINAL_STATUSES:
                await s.commit()
                return Transition(call, 200, note="call already finalized")
            call.started_at = _aware(req.started_at) or call.started_at
            call.participants_json = [p.model_dump() for p in req.participants] or call.participants_json
            if req.farm_access_token:
                call.farm_access_token = req.farm_access_token
            if req.farm is not None:
                call.farm_json = req.farm
            if req.metadata is not None:
                call.metadata_json = req.metadata
            if req.num_speakers:
                call.num_speakers = req.num_speakers
            call.language = req.language or call.language
            if req.callback_url is not None:
                call.callback_url = req.callback_url
            await repo.add_event(s, call.call_id, "call_started" if created else "call_upserted")
            await s.commit()
            await s.refresh(call)
            await self._put_call_json(call)
            return Transition(call, 201 if created else 200)

    async def _put_call_json(self, call: Call) -> None:
        try:
            await self.s3.put_json(self.s3.keys.call_json(call.call_id), {
                "call_id": call.call_id, "state": call.state, "status": call.status,
                "started_at": call.started_at, "ended_at": call.ended_at,
                "participants": call.participants_json, "farm": call.farm_json, "metadata": call.metadata_json,
                "num_speakers": call.num_speakers, "language": call.language,
                "audio": [{"bucket": a.bucket, "key": a.key, "seq": a.seq, "status": a.status} for a in call.audio],
            })
        except Exception as e:  # noqa: BLE001 — 부수효과, 실패해도 흐름 유지
            log.warning("[%s] call.json put failed: %s", call.call_id, e)

    # --- audio ---------------------------------------------------------
    async def audio_received(self, call_id: str, req: AudioReceivedRequest) -> Transition:
        head = None
        if self.settings.s3_head_on_ingest:
            try:
                head = await self.s3.head(req.bucket, req.key)
            except S3Error as e:
                raise ApiError(e.code, e.message, 422) from e
            if head.size > self.settings.max_audio_mb * 1024 * 1024:
                raise ApiError("AUDIO_TOO_LARGE",
                               f"{head.size} bytes > {self.settings.max_audio_mb}MB (gateway upload limit)", 422)

        async with self.rt.db.session() as s:
            call = await repo.get_call(s, call_id)
            if call is None:
                if not self.settings.audio_autocreate_call:
                    raise ApiError("CALL_NOT_FOUND", f"call {call_id} not found", 404)
                call = Call(call_id=call_id, s3_prefix=self.s3.keys.base(call_id))
                s.add(call)
                await s.flush()
                await repo.add_event(s, call_id, "call_autocreated")
                log.info("[%s] call auto-created by audio event", call_id)

            existing = await repo.find_audio(s, call_id, req.bucket, req.key)
            note = None
            wake = False
            http = 202
            if existing is None:
                a = CallAudio(call_id=call_id, bucket=req.bucket, key=req.key, seq=req.seq,
                              recorded_at=_aware(req.recorded_at), duration_sec=req.duration_sec,
                              speaker_hint=req.speaker_hint,
                              size_bytes=head.size if head else None, etag=head.etag if head else None)
                s.add(a)
                await s.flush()
                await repo.add_event(s, call_id, "audio_received", {"bucket": req.bucket, "key": req.key}, a.id)
                wake = True
            else:
                a = existing
                # 메타 갱신은 허용 (seq/recorded_at 늦게 오는 경우)
                if req.seq is not None:
                    a.seq = req.seq
                if req.recorded_at is not None:
                    a.recorded_at = _aware(req.recorded_at)
                if req.duration_sec is not None:
                    a.duration_sec = req.duration_sec
                if req.speaker_hint is not None:
                    a.speaker_hint = req.speaker_hint
                if a.status in ("PENDING", "TRANSCRIBING"):
                    http, note = 200, "already queued"
                elif a.status == "TRANSCRIBED" and not req.force:
                    http, note = 200, "already transcribed"
                else:  # FAILED, 또는 force
                    a.status, a.attempts, a.next_attempt_at = "PENDING", 0, utcnow()
                    a.last_error, a.error_permanent, a.claimed_at = None, False, None
                    await repo.add_event(s, call_id, "audio_requeued", {"force": req.force}, a.id)
                    http, note, wake = 202, "re-queued", True
            if call.status in repo.TERMINAL_STATUSES and wake:
                call.stale = True
                note = (note + "; " if note else "") + "call already finalized — regenerate to include"
            await s.commit()
            await s.refresh(call)
            await s.refresh(a)
            return Transition(call, http, note=note, wake=wake, audio=a)

    # --- end -----------------------------------------------------------
    async def end(self, call_id: str, req: CallEndRequest) -> Transition:
        async with self.rt.db.session() as s:
            call = await repo.get_call(s, call_id)
            if call is None:
                raise ApiError("CALL_NOT_FOUND", f"call {call_id} not found", 404)
            if call.state == "ENDED":
                if req.ended_at and not call.ended_at:
                    call.ended_at = _aware(req.ended_at)
                await s.commit()
                return Transition(call, 200, note="already ended")
            call.state = "ENDED"
            call.status = "PROCESSING"
            call.ended_at = _aware(req.ended_at) or utcnow()
            if req.duration_sec is not None:
                call.duration_sec = req.duration_sec
            call.error_code = call.error_message = None
            await repo.add_event(s, call_id, "call_ended")
            await s.flush()
            queued = await repo.schedule_generation_if_ready(s, call_id)
            await s.commit()
            await s.refresh(call)
            await self._put_call_json(call)
            return Transition(call, 202, wake=True, note="generation queued" if queued else "waiting for STT")

    # --- regenerate ----------------------------------------------------
    async def regenerate(self, call_id: str, req: RegenerateRequest) -> Transition:
        async with self.rt.db.session() as s:
            call = await repo.get_call(s, call_id)
            if call is None:
                raise ApiError("CALL_NOT_FOUND", f"call {call_id} not found", 404)
            if call.state != "ENDED":
                raise ApiError("CALL_NOT_ENDED", "regenerate requires an ended call", 409)
            if call.gen_state != "IDLE" or (call.status == "PROCESSING" and any(
                    a.status in repo.ACTIVE_AUDIO for a in call.audio)):
                raise ApiError("ALREADY_PROCESSING", "a generation run is active", 409)
            call.status = "PROCESSING"
            call.stale = False
            call.error_code = call.error_message = None
            call.generation_attempts = 0
            if req.retranscribe:
                await s.execute(update(CallAudio).where(CallAudio.call_id == call_id)
                                .values(status="PENDING", attempts=0, next_attempt_at=utcnow(),
                                        last_error=None, error_permanent=False, claimed_at=None,
                                        segments_json=None, segments_count=None, stt_raw_key=None,
                                        stt_seconds=None, offset_sec=None, updated_at=utcnow()))
            await repo.add_event(s, call_id, "regenerate", {"retranscribe": req.retranscribe, "reason": req.reason})
            await s.flush()
            await repo.schedule_generation_if_ready(s, call_id)
            await s.commit()
            await s.refresh(call)
            return Transition(call, 202, wake=True)
