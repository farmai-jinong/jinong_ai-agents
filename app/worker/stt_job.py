"""STT 작업 1건: S3 get → 게이트웨이 diarize → raw JSON S3 저장 → 행 갱신 → 생성 스케줄 시도."""

from __future__ import annotations

import logging
from datetime import timedelta

from ..clients.s3 import S3Error
from ..clients.stt import SttError, backoff_delay
from ..db import repo
from ..db.models import utcnow
from ..runtime import Runtime

log = logging.getLogger(__name__)


async def run_stt(rt: Runtime, audio_id: int) -> bool:
    """반환: 통화의 생성이 QUEUED 되었는지 (워커가 바로 generation 을 돌리도록)."""
    s3, stt, settings = rt.s3, rt.stt, rt.settings
    async with rt.db.session() as s:
        a = await repo.get_audio(s, audio_id)
        if a is None or a.status != "TRANSCRIBING":
            return False
        call = await repo.get_call(s, a.call_id)
        call_id, bucket, key = a.call_id, a.bucket, a.key
        num_speakers = call.num_speakers if call else None
        if not num_speakers and call and call.participants_json:
            num_speakers = len(call.participants_json) or None
        ordinal = a.id
        attempt = a.attempts

    log.info("[%s] STT start audio#%d s3://%s/%s (attempt %d)", call_id, audio_id, bucket, key, attempt + 1)
    err: SttError | None = None
    result = None
    try:
        audio_bytes = await s3.get_bytes(bucket, key)
        filename = key.rsplit("/", 1)[-1] or "audio.bin"
        result = await stt.diarize(audio_bytes, filename, num_speakers)
    except S3Error as e:
        err = SttError(f"S3 get failed: {e}", permanent=False)
    except SttError as e:
        err = e
    except Exception as e:  # noqa: BLE001
        log.exception("[%s] STT unexpected error audio#%d", call_id, audio_id)
        err = SttError(f"unexpected: {e}", permanent=False)

    raw_key = None
    if result is not None:
        raw_key = s3.keys.stt_raw(call_id, ordinal % 100, bucket, key)
        try:
            await s3.put_json(raw_key, result.raw)
        except Exception as e:  # noqa: BLE001 — raw 저장 실패는 치명적이지 않음(segments 는 DB 캐시)
            log.warning("[%s] STT raw put failed: %s", call_id, e)
            raw_key = None

    async with rt.db.session() as s:
        a = await repo.get_audio(s, audio_id)
        if a is None:
            return False
        if result is not None:
            a.status = "TRANSCRIBED"
            a.attempts += 1
            a.stt_seconds = result.seconds if result.seconds is not None else result.source_end_sec
            a.stt_raw_key = raw_key
            a.segments_json = result.segments
            a.segments_count = len(result.segments)
            a.last_error = None
            a.claimed_at = None
            await repo.add_event(s, call_id, "stt_done", {"segments": len(result.segments), "seconds": a.stt_seconds}, audio_id)
            log.info("[%s] STT done audio#%d: %d segments, %.1fs", call_id, audio_id, len(result.segments), a.stt_seconds or 0)
        else:
            assert err is not None
            a.attempts += 1
            a.last_error = str(err)[:1000]
            a.claimed_at = None
            if err.permanent or a.attempts >= settings.stt_max_attempts:
                a.status = "FAILED"
                a.error_permanent = err.permanent
                await repo.add_event(s, call_id, "stt_failed", {"error": a.last_error, "attempts": a.attempts}, audio_id)
                log.error("[%s] STT FAILED audio#%d after %d attempts: %s", call_id, audio_id, a.attempts, a.last_error)
            else:
                delay = err.retry_after if err.retry_after else backoff_delay(a.attempts - 1)
                a.status = "PENDING"
                a.next_attempt_at = utcnow() + timedelta(seconds=delay)
                await repo.add_event(s, call_id, "stt_retry", {"error": a.last_error, "delay": delay}, audio_id)
                log.warning("[%s] STT retry audio#%d in %.0fs: %s", call_id, audio_id, delay, a.last_error)
        await s.flush()
        queued = await repo.schedule_generation_if_ready(s, call_id)
        await s.commit()
        return queued
