"""날짜별(멀티콜) 영농일지 API — 트리거 검증/멱등, regenerate, 조회."""

from __future__ import annotations

import pytest

from app.db import repo
from tests.conftest import full_flow

pytestmark = pytest.mark.asyncio

DAILY = {"diary_id": "daily_u1_20260820", "diary_date": "2026-08-20", "call_ids": ["c1", "c2"]}


async def _complete_calls(client, app, ids=("c1", "c2")):
    for cid in ids:
        await full_flow(client, cid)
    await app.state.rt.worker.drain()
    for cid in ids:
        assert (await client.get(f"/v1/calls/{cid}")).json()["status"] == "COMPLETED"


async def test_create_idempotent_and_complete(client, app, stt_mock):
    await _complete_calls(client, app)
    r = await client.post("/v1/daily-diaries", json=DAILY)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "PROCESSING" and body["generation"]["state"] == "QUEUED"
    assert body["call_ids"] == ["c1", "c2"]

    r = await client.post("/v1/daily-diaries", json=DAILY)          # 진행 중 재-POST
    assert r.status_code == 200 and "already processing" in r.json()["note"]

    await app.state.rt.worker.drain()
    body = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}")).json()
    assert body["status"] == "COMPLETED"

    r = await client.post("/v1/daily-diaries", json=DAILY)          # terminal 재-POST
    assert r.status_code == 200 and "regenerate" in r.json()["note"]


async def test_validation_errors(client, app, stt_mock):
    # 존재하지 않는 call
    r = await client.post("/v1/daily-diaries", json={**DAILY, "diary_id": "d-x1", "call_ids": ["nope"]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "CALLS_NOT_FOUND"

    # terminal 아님 (start 만 한 call → status NONE)
    await full_flow(client, "open1", end=False)
    r = await client.post("/v1/daily-diaries", json={**DAILY, "diary_id": "d-x2", "call_ids": ["open1"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CALLS_NOT_READY"

    # COMPLETED 0건 (오디오 없이 종료 → EMPTY)
    await full_flow(client, "empty1", keys=())
    await app.state.rt.worker.drain()
    assert (await client.get("/v1/calls/empty1")).json()["status"] == "EMPTY"
    r = await client.post("/v1/daily-diaries", json={**DAILY, "diary_id": "d-x3", "call_ids": ["empty1"]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "NO_TRANSCRIBED_CALLS"

    # call_ids 중복 → pydantic 422
    r = await client.post("/v1/daily-diaries", json={**DAILY, "diary_id": "d-x4", "call_ids": ["c1", "c1"]})
    assert r.status_code == 422


async def test_farm_mismatch(client, app, stt_mock):
    await _complete_calls(client, app)
    rt = app.state.rt
    async with rt.db.session() as s:
        (await repo.get_call(s, "c1")).farm_json = {"farm_id": "f1"}
        (await repo.get_call(s, "c2")).farm_json = {"farm_id": "f2"}
        await s.commit()
    r = await client.post("/v1/daily-diaries", json={**DAILY, "diary_id": "d-farm"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "FARM_MISMATCH"


async def test_regenerate_guards_and_token(client, app, stt_mock):
    r = await client.post("/v1/daily-diaries/none/regenerate", json={})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "DAILY_NOT_FOUND"

    await _complete_calls(client, app)
    await client.post("/v1/daily-diaries", json=DAILY)
    r = await client.post(f"/v1/daily-diaries/{DAILY['diary_id']}/regenerate", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_PROCESSING"   # QUEUED 중

    rt = app.state.rt
    await rt.worker.drain()
    r = await client.post(f"/v1/daily-diaries/{DAILY['diary_id']}/regenerate",
                          json={"farm_access_token": "eyJ.new.token"})
    assert r.status_code == 202
    async with rt.db.session() as s:
        dd = await repo.get_daily(s, DAILY["diary_id"])
        assert dd.gen_state == "QUEUED" and dd.farm_access_token == "eyJ.new.token"
        assert dd.generation_attempts == 0
    await rt.worker.drain()
    body = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}")).json()
    assert body["status"] == "COMPLETED" and body["generation"]["run"] == 2


async def test_list_and_artifact_endpoints(client, app, stt_mock):
    await _complete_calls(client, app)
    await client.post("/v1/daily-diaries", json=DAILY)
    await app.state.rt.worker.drain()

    body = (await client.get("/v1/daily-diaries", params={"diary_date": "2026-08-20"})).json()
    assert [i["diary_id"] for i in body["items"]] == [DAILY["diary_id"]]
    assert (await client.get("/v1/daily-diaries", params={"diary_date": "1999-01-01"})).json()["items"] == []

    detail = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}")).json()
    assert len(detail["result"]["diaries"]) == 1
    assert "report" not in detail["result"]                          # daily 는 일지만
    code = detail["result"]["diaries"][0]["prdlst_code"]

    r = await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}/artifacts/diary/{code}")
    assert r.status_code == 200 and r.text.startswith("# 영농일지")
    r = await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}/artifacts/diary/{code}", params={"format": "json"})
    assert r.status_code == 200 and r.json()["diary_date"] == "2026-08-20"

    tr = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}/transcript")).json()
    assert sorted({f["call_id"] for f in tr["files"]}) == ["c1", "c2"]
