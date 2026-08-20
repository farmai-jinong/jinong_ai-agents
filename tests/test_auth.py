import httpx
import pytest

from app.config import Settings
from app.main import create_app


def test_fail_closed_without_key(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(Settings(agent_api_key="", allow_no_auth=False, db_path=str(tmp_path / "a.db"),
                            pipeline_impl="fake"), start_worker=False)


@pytest.mark.asyncio
async def test_bearer_and_x_api_key(tmp_path, s3_env):
    app = create_app(Settings(agent_api_key="k1, k2", db_path=str(tmp_path / "a.db"), pipeline_impl="fake",
                              log_level="warning"), start_worker=False)
    await app.state.rt.startup(start_worker=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/healthz")).status_code == 200                       # 무인증
            assert (await c.get("/v1/calls/x")).status_code == 401
            assert (await c.get("/v1/calls/x", headers={"Authorization": "Bearer nope"})).status_code == 401
            r = await c.get("/v1/calls/x", headers={"Authorization": "Bearer k2"})
            assert r.status_code == 404 and r.json()["detail"]["code"] == "CALL_NOT_FOUND"
            assert (await c.get("/v1/calls/x", headers={"X-API-Key": "k1"})).status_code == 404
    finally:
        await app.state.rt.shutdown()
