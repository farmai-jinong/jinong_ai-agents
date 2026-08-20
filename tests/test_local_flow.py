"""STORAGE_IMPL=local 로 전체 플로우 (moto/s3_env 미사용) — start → audio(local) → end → drain → COMPLETED."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
import respx

from app.config import Settings, get_settings
from app.main import create_app
from tests.conftest import BUCKET, STT_URL


@pytest.fixture
def local_settings(tmp_path) -> Settings:
    get_settings.cache_clear()
    return Settings(
        agent_api_key="", allow_no_auth=True, db_path=str(tmp_path / "agent.db"),
        pipeline_impl="fake", s3_bucket=BUCKET, s3_prefix="agents/voicecall",
        storage_impl="local", local_storage_dir=str(tmp_path / "storage"),
        local_audio_dir=str(tmp_path / "audio"),
        stt_base_url=STT_URL, stt_api_key="stt-key", stt_max_attempts=3,
        llm_base_url="https://llm.test/v1", farmos_base_url="https://farmos.test",
        gen_timeout_sec=10, worker_poll_sec=0.1, callback_enabled=False, log_level="warning",
    )


@pytest_asyncio.fixture
async def local_app(local_settings, tmp_path):
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "sample.wav").write_bytes(b"RIFF" + b"\x00" * 100)
    application = create_app(local_settings, start_worker=False)
    rt = application.state.rt
    await rt.startup(start_worker=False)
    try:
        yield application
    finally:
        await rt.shutdown()


@pytest_asyncio.fixture
async def local_client(local_app):
    transport = httpx.ASGITransport(app=local_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_local_file_e2e_flow(local_client, local_app, stt_sample, tmp_path):
    rt = local_app.state.rt
    now = datetime.now(UTC)
    r = await local_client.post("/v1/calls", json={
        "call_id": "lc1", "started_at": (now - timedelta(minutes=15)).isoformat(),
        "participants": [{"role": "farmer", "user_id": "u1", "name": "홍길동"},
                         {"role": "consultant", "user_id": "c1", "name": "김상담"}],
        "metadata": {"hints": {"prdlst_code": "0804MM", "prdlst_nm": "딸기"}},
    })
    assert r.status_code in (200, 201), r.text
    r = await local_client.post("/v1/calls/lc1/audio",
                                json={"bucket": "local", "key": "sample.wav", "seq": 1})
    assert r.status_code in (200, 202), r.text
    r = await local_client.post("/v1/calls/lc1/end", json={"ended_at": now.isoformat()})
    assert r.status_code in (200, 202), r.text

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{STT_URL}/v1/audio/transcriptions").mock(
            return_value=httpx.Response(200, json=stt_sample))
        await rt.worker.drain()

    body = (await local_client.get("/v1/calls/lc1")).json()
    assert body["status"] == "COMPLETED", body
    base = tmp_path / "storage" / "agents/voicecall/lc1"
    assert (base / "artifacts/result.json").is_file()
    assert list((base / "stt").glob("*.json"))


@pytest.mark.asyncio
async def test_local_missing_file_rejected_on_ingest(local_client):
    now = datetime.now(UTC)
    r = await local_client.post("/v1/calls", json={
        "call_id": "lc2", "started_at": now.isoformat(),
        "participants": [{"role": "farmer", "user_id": "u1", "name": "홍길동"}],
    })
    assert r.status_code in (200, 201), r.text
    r = await local_client.post("/v1/calls/lc2/audio",
                                json={"bucket": "local", "key": "nope.wav", "seq": 1})
    assert r.status_code == 422, r.text
