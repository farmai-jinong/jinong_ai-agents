"""테스트 공통 픽스처 — tmp SQLite + moto S3 + respx STT + FakePipeline. 워커는 수동(run_once/drain)."""

from __future__ import annotations

import json
import os
import pathlib

import httpx
import pytest
import pytest_asyncio
import respx
from moto import mock_aws

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
# app.main 은 임포트 시 모듈 레벨 app 을 만든다(fail-closed) → 테스트는 무인증·fake 로 임포트
os.environ["ALLOW_NO_AUTH"] = "1"
os.environ["PIPELINE_IMPL"] = "fake"
os.environ["DB_PATH"] = "/tmp/jinong-agent-import.db"

from datetime import UTC

from app.config import Settings, get_settings  # noqa: E402
from app.main import create_app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
STT_URL = "https://stt.test"
BUCKET = "jinong-agri-stt"


@pytest.fixture
def stt_sample() -> list:
    return json.loads((FIXTURES / "gateway_diarize_sample.json").read_text())


@pytest.fixture
def settings(tmp_path) -> Settings:
    get_settings.cache_clear()
    return Settings(
        agent_api_key="", allow_no_auth=True, db_path=str(tmp_path / "agent.db"),
        pipeline_impl="fake", s3_bucket=BUCKET, s3_prefix="agents/voicecall",
        stt_base_url=STT_URL, stt_api_key="stt-key", stt_max_attempts=3,
        llm_base_url="https://llm.test/v1", farmos_base_url="https://farmos.test",
        gen_timeout_sec=10, worker_poll_sec=0.1, callback_enabled=False, log_level="warning",
    )


@pytest.fixture
def s3_env():
    with mock_aws():
        import boto3

        c = boto3.client("s3", region_name="ap-northeast-2")
        c.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-2"})
        c.put_object(Bucket=BUCKET, Key="raw/sample1.wav", Body=b"RIFF" + b"\x00" * 100)
        c.put_object(Bucket=BUCKET, Key="raw/sample2.wav", Body=b"RIFF" + b"\x01" * 100)
        yield c


@pytest_asyncio.fixture
async def app(settings, s3_env):
    application = create_app(settings, start_worker=False)
    rt = application.state.rt
    await rt.startup(start_worker=False)
    try:
        yield application
    finally:
        await rt.shutdown()


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def stt_mock(stt_sample):
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{STT_URL}/v1/audio/transcriptions").mock(return_value=httpx.Response(200, json=stt_sample))
        yield router


async def full_flow(client, call_id="call-1", keys=("raw/sample1.wav",), end=True):
    # 시각은 '지금' 기준 상대값 — 실제 통화와 같은 모양으로. (deadline 스윕은 서버 수신 시각을 쓰므로 여기에 의존하지 않는다)
    from datetime import datetime, timedelta
    now = datetime.now(UTC)
    started_at = (now - timedelta(minutes=15)).isoformat()
    ended_at = now.isoformat()
    r = await client.post("/v1/calls", json={
        "call_id": call_id, "started_at": started_at,
        "participants": [{"role": "farmer", "user_id": "u1", "name": "홍길동"},
                         {"role": "consultant", "user_id": "c1", "name": "김상담"}],
        "farm_access_token": "eyJ.secret.token", "metadata": {"hints": {"prdlst_code": "0804MM", "prdlst_nm": "딸기"}},
    })
    assert r.status_code in (200, 201), r.text
    for i, k in enumerate(keys):
        r = await client.post(f"/v1/calls/{call_id}/audio", json={"bucket": BUCKET, "key": k, "seq": i + 1})
        assert r.status_code in (200, 202), r.text
    if end:
        r = await client.post(f"/v1/calls/{call_id}/end", json={"ended_at": ended_at})
        assert r.status_code in (200, 202), r.text
    return r
