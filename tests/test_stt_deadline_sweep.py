"""STT deadline 스윕 — 기준 시각은 호출자가 보낸 `ended_at` 이 아니라 서버가 종료를 받은 시각이다."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.db import repo
from app.db.models import Call, CallAudio, JobEvent, utcnow
from app.worker.recovery import sweep
from tests.conftest import BUCKET


async def _start(client, call_id: str, ended_at: str) -> None:
    r = await client.post("/v1/calls", json={
        "call_id": call_id, "started_at": ended_at,
        "participants": [{"role": "farmer", "user_id": "u1", "name": "홍길동"}],
    })
    assert r.status_code in (200, 201), r.text
    r = await client.post(f"/v1/calls/{call_id}/audio", json={"bucket": BUCKET, "key": "raw/sample1.wav", "seq": 1})
    assert r.status_code == 202, r.text
    r = await client.post(f"/v1/calls/{call_id}/end", json={"ended_at": ended_at})
    assert r.status_code == 202, r.text


@pytest.mark.asyncio
async def test_stale_ended_at_does_not_trip_sweep(client, app):
    """백엔드가 한참 지난 `ended_at`(시계 오차·지연 재전송)을 보내도 방금 받은 통화는 스윕되지 않는다."""
    rt = app.state.rt
    old = (utcnow() - timedelta(days=400)).isoformat()
    await _start(client, "sw1", old)

    assert await sweep(rt) == []
    async with rt.db.session() as s:
        audio = (await repo.list_audio(s, "sw1"))[0]
        call = await repo.get_call(s, "sw1")
    assert audio.status == "PENDING" and audio.last_error is None
    assert call.status == "PROCESSING"


@pytest.mark.asyncio
async def test_sweep_fires_on_server_receipt_time(client, app):
    """서버가 종료를 받은 지 deadline 이 지나면 스윕한다 — `ended_at` 이 최근이어도."""
    rt = app.state.rt
    await _start(client, "sw2", utcnow().isoformat())
    assert await sweep(rt) == []                                   # 방금 받았으므로 아직

    past = utcnow() - timedelta(seconds=rt.settings.end_stt_deadline_sec + 60)
    async with rt.db.session() as s:                               # 종료 수신·녹음 도착을 과거로 밀어놓는다
        await s.execute(update(JobEvent).where(JobEvent.call_id == "sw2", JobEvent.event == "call_ended")
                        .values(ts=past))
        await s.execute(update(CallAudio).where(CallAudio.call_id == "sw2").values(created_at=past))
        await s.commit()

    assert await sweep(rt) == ["sw2"]
    async with rt.db.session() as s:
        audio = (await repo.list_audio(s, "sw2"))[0]
    assert audio.status == "FAILED" and audio.error_permanent is True
    assert "STT_TIMEOUT" in audio.last_error


@pytest.mark.asyncio
async def test_audio_arriving_after_end_gets_its_own_deadline(client, app):
    """종료를 오래전에 받았어도, 그 뒤 도착한 녹음은 도착 시각부터 다시 잰다."""
    rt = app.state.rt
    await _start(client, "sw3", utcnow().isoformat())
    past = utcnow() - timedelta(seconds=rt.settings.end_stt_deadline_sec + 60)
    async with rt.db.session() as s:
        await s.execute(update(JobEvent).where(JobEvent.call_id == "sw3", JobEvent.event == "call_ended")
                        .values(ts=past))
        await s.execute(update(CallAudio).where(CallAudio.call_id == "sw3").values(created_at=past))
        await s.commit()

    r = await client.post("/v1/calls/sw3/audio", json={"bucket": BUCKET, "key": "raw/sample2.wav", "seq": 2})
    assert r.status_code == 202, r.text

    assert await sweep(rt) == ["sw3"]                              # 첫 녹음(옛날 도착)만
    async with rt.db.session() as s:
        by_key = {a.key: a for a in await repo.list_audio(s, "sw3")}
    assert by_key["raw/sample1.wav"].status == "FAILED"
    assert by_key["raw/sample2.wav"].status == "PENDING"


@pytest.mark.asyncio
async def test_sweep_falls_back_to_call_row_when_event_missing(client, app):
    """`call_ended` 이벤트가 없는 옛 행은 통화 행 생성 시각으로 잰다(이벤트 로그 정리 후에도 스윕이 죽지 않도록)."""
    rt = app.state.rt
    await _start(client, "sw4", utcnow().isoformat())
    past = utcnow() - timedelta(seconds=rt.settings.end_stt_deadline_sec + 60)
    async with rt.db.session() as s:
        rows = (await s.execute(select(JobEvent.id).where(JobEvent.call_id == "sw4"))).scalars().all()
        for jid in rows:
            await s.execute(update(JobEvent).where(JobEvent.id == jid).values(event="purged"))
        await s.execute(update(Call).where(Call.call_id == "sw4").values(created_at=past))
        await s.execute(update(CallAudio).where(CallAudio.call_id == "sw4").values(created_at=past))
        await s.commit()

    assert await sweep(rt) == ["sw4"]
