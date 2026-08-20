import httpx
import pytest
import respx

from app.db import repo
from app.db.models import utcnow
from app.worker.recovery import recover
from tests.conftest import STT_URL, full_flow


@pytest.mark.asyncio
async def test_generation_waits_until_all_audio_done(client, app, stt_sample):
    rt = app.state.rt
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{STT_URL}/v1/audio/transcriptions")
        route.side_effect = [httpx.Response(503), httpx.Response(200, json=stt_sample),
                             httpx.Response(200, json=stt_sample)]
        await full_flow(client, "w1", keys=("raw/sample1.wav", "raw/sample2.wav"))
        await rt.worker.run_once()
        # 두 STT 태스크 완료 대기
        import asyncio
        await asyncio.gather(*list(rt.worker._inflight), return_exceptions=True)
        async with rt.db.session() as s:
            audios = await repo.list_audio(s, "w1")
            call = await repo.get_call(s, "w1")
        statuses = sorted(a.status for a in audios)
        assert statuses == ["PENDING", "TRANSCRIBED"]          # 하나는 503 → 재시도 대기
        assert call.gen_state == "IDLE" and call.status == "PROCESSING"
        # 재시도 시각을 당겨서 진행
        async with rt.db.session() as s:
            for a in await repo.list_audio(s, "w1"):
                a.next_attempt_at = utcnow()
            await s.commit()
        await rt.worker.drain()
    body = (await client.get("/v1/calls/w1")).json()
    assert body["status"] == "COMPLETED"
    assert body["stt_progress"]["transcribed"] == 2
    assert body["audio"][0]["offset_sec"] == 0.0 and body["audio"][1]["offset_sec"] == 77.0


@pytest.mark.asyncio
async def test_permanent_stt_failure_all_failed(client, app):
    rt = app.state.rt
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{STT_URL}/v1/audio/transcriptions").mock(return_value=httpx.Response(415, text="bad audio"))
        await full_flow(client, "w2")
        await rt.worker.drain()
    body = (await client.get("/v1/calls/w2")).json()
    assert body["status"] == "FAILED" and body["error"]["code"] == "STT_FAILED"
    assert body["audio"][0]["status"] == "FAILED" and body["audio"][0]["attempts"] == 1


@pytest.mark.asyncio
async def test_partial_failure_generates_with_warning(client, app, stt_sample):
    rt = app.state.rt
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{STT_URL}/v1/audio/transcriptions")
        route.side_effect = [httpx.Response(200, json=stt_sample), httpx.Response(413)]
        await full_flow(client, "w3", keys=("raw/sample1.wav", "raw/sample2.wav"))
        await rt.worker.drain()
    body = (await client.get("/v1/calls/w3")).json()
    assert body["status"] == "COMPLETED"
    assert body["stt_progress"]["failed"] == 1
    assert any("STT failed" in w for w in body["generation"]["warnings"])


@pytest.mark.asyncio
async def test_transient_exhausts_attempts(client, app):
    rt = app.state.rt
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{STT_URL}/v1/audio/transcriptions").mock(return_value=httpx.Response(502))
        await full_flow(client, "w4")
        for _ in range(5):
            await rt.worker.drain(timeout=5)
            async with rt.db.session() as s:
                for a in await repo.list_audio(s, "w4"):
                    a.next_attempt_at = utcnow()
                await s.commit()
    body = (await client.get("/v1/calls/w4")).json()
    assert body["audio"][0]["status"] == "FAILED" and body["audio"][0]["attempts"] == 3   # stt_max_attempts=3
    assert body["status"] == "FAILED"


@pytest.mark.asyncio
async def test_recovery_resets_inflight(client, app):
    rt = app.state.rt
    await full_flow(client, "w5", end=False)
    async with rt.db.session() as s:
        ids = await repo.claim_audio(s, 5)
        await s.commit()
    assert ids
    async with rt.db.session() as s:
        call = await repo.get_call(s, "w5")
        call.state, call.status, call.gen_state = "ENDED", "PROCESSING", "RUNNING"
        await s.commit()
    stats = await recover(rt)
    assert stats["audio_reset"] == 1 and stats["gen_reset"] == 1
    async with rt.db.session() as s:
        a = (await repo.list_audio(s, "w5"))[0]
        call = await repo.get_call(s, "w5")
    assert a.status == "PENDING" and call.gen_state == "QUEUED"


@pytest.mark.asyncio
async def test_pipeline_empty_and_failure(client, app, stt_mock, monkeypatch):
    from app.agents.interface import PipelineEmpty

    rt = app.state.rt

    class Empty:
        async def run(self, t, c):
            raise PipelineEmpty("nothing")

    rt.pipeline = Empty()
    await full_flow(client, "w6")
    await rt.worker.drain()
    body = (await client.get("/v1/calls/w6")).json()
    assert body["status"] == "EMPTY" and body["error"]["code"] == "NO_CONTENT"

    class Boom:
        async def run(self, t, c):
            raise RuntimeError("llm down")

    rt.pipeline = Boom()
    await full_flow(client, "w7")
    for _ in range(3):
        await rt.worker.drain(timeout=5)
        async with rt.db.session() as s:
            call = await repo.get_call(s, "w7")
            call.gen_next_attempt_at = utcnow()
            await s.commit()
    body = (await client.get("/v1/calls/w7")).json()
    assert body["status"] == "FAILED" and body["error"]["code"] == "GENERATION_FAILED"
    assert body["generation"]["attempts"] == 2                   # gen_max_attempts=2
