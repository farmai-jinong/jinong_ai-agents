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

    r = await client.post("/v1/daily-diaries", json=DAILY)          # 클레임 전 재-POST — 대기 중 실행이 새 목록을 읽는다
    assert r.status_code == 200 and "already queued" in r.json()["note"]

    await app.state.rt.worker.drain()
    body = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}")).json()
    assert body["status"] == "COMPLETED" and body["generation"]["run"] == 1

    # terminal 재-POST → 새 생성 회차 (백엔드 자동 배치는 콜백마다 전체 목록을 재전송한다)
    r = await client.post("/v1/daily-diaries", json=DAILY)
    assert r.status_code == 200 and "regeneration queued" in r.json()["note"]
    assert r.json()["status"] == "PROCESSING"
    await app.state.rt.worker.drain()
    body = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}")).json()
    assert body["status"] == "COMPLETED" and body["generation"]["run"] == 2


async def test_retrigger_adds_new_call(client, app, stt_mock):
    """통화가 추가되면 같은 diary_id 로 전체 call_ids 를 재전송 — 목록 갱신 + 새 회차."""
    await _complete_calls(client, app)
    assert (await client.post("/v1/daily-diaries", json=DAILY)).status_code == 201
    await app.state.rt.worker.drain()

    await _complete_calls(client, app, ids=("c3",))
    r = await client.post("/v1/daily-diaries", json={**DAILY, "call_ids": ["c1", "c2", "c3"]})
    assert r.status_code == 200 and "regeneration queued" in r.json()["note"]
    assert r.json()["call_ids"] == ["c1", "c2", "c3"]

    await app.state.rt.worker.drain()
    body = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}")).json()
    assert body["status"] == "COMPLETED"
    assert body["call_ids"] == ["c1", "c2", "c3"] and body["generation"]["run"] == 2


async def test_auto_batch_request_shape(client, app, stt_mock):
    """AP 백엔드 자동 배치 요청 그대로 — farm_access_token 없음, hints 에 농가 복합 키."""
    await _complete_calls(client, app)
    r = await client.post("/v1/daily-diaries", json={
        "diary_id": "daily_1_test7_20260826", "diary_date": "2026-08-26",
        "call_ids": ["c1", "c2"],
        "callback_url": "https://backend.test/voicetalk/public/agent-callback",
        "language": "ko",
        "metadata": {"hints": {"diary_date": "2026-08-26",
                               "farmer_engn_id": "1", "farmer_user_id": "test7"}},
    })
    assert r.status_code == 201, r.text          # 토큰 없이도 접수 — farmos 조회 없이 생성
    await app.state.rt.worker.drain()

    body = (await client.get("/v1/daily-diaries/daily_1_test7_20260826?inline=true")).json()
    assert body["status"] == "COMPLETED"
    assert body["call_ids"] == ["c1", "c2"]
    assert body["result"]["diaries"], "작물별 일지가 최소 1건"
    assert body["result"]["diaries"][0]["diary_date"] == "2026-08-26"   # 요청 날짜로 고정


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


async def test_farmer_composite_key_mismatch(client, app, stt_mock):
    # 농가 구분은 (engn_id, user_id) 복합 키 — engn_id 가 다르면 같은 user_id 여도 차단
    await _complete_calls(client, app)
    rt = app.state.rt
    async with rt.db.session() as s:
        (await repo.get_call(s, "c1")).participants_json = [
            {"role": "farmer", "user_id": "u1", "engn_id": "18", "name": "홍길동"}]
        (await repo.get_call(s, "c2")).participants_json = [
            {"role": "farmer", "user_id": "u1", "engn_id": "19", "name": "홍길동"}]
        await s.commit()
    r = await client.post("/v1/daily-diaries", json={**DAILY, "diary_id": "d-engn-x"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "FARM_MISMATCH"

    # 복합 키가 같으면 통과
    async with rt.db.session() as s:
        (await repo.get_call(s, "c2")).participants_json = [
            {"role": "farmer", "user_id": "u1", "engn_id": "18", "name": "홍길동"}]
        await s.commit()
    r = await client.post("/v1/daily-diaries", json={**DAILY, "diary_id": "d-engn-ok"})
    assert r.status_code == 201, r.text


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
    assert r.status_code == 200 and r.text.startswith("> 📝") and "## 주요 농작업" in r.text
    r = await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}/artifacts/diary/{code}", params={"format": "json"})
    assert r.status_code == 200 and r.json()["diary_date"] == "2026-08-20"

    tr = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}/transcript")).json()
    assert sorted({f["call_id"] for f in tr["files"]}) == ["c1", "c2"]


async def test_list_cursor_pagination(client, app, stt_mock):
    """keyset 커서: 페이지 순회 시 누락/중복 없음, 잘못된 커서는 422."""
    await _complete_calls(client, app)
    ids = [f"daily_pg_{i}" for i in range(1, 4)]
    for did in ids:
        r = await client.post("/v1/daily-diaries", json={**DAILY, "diary_id": did})
        assert r.status_code == 201, r.text
    await app.state.rt.worker.drain()

    page1 = (await client.get("/v1/daily-diaries", params={"limit": 2})).json()
    assert [i["diary_id"] for i in page1["items"]] == ["daily_pg_3", "daily_pg_2"]
    assert page1["next_cursor"]

    page2 = (await client.get("/v1/daily-diaries", params={"limit": 2, "cursor": page1["next_cursor"]})).json()
    assert [i["diary_id"] for i in page2["items"]] == ["daily_pg_1"]
    assert page2["next_cursor"] is None

    seen = [i["diary_id"] for i in page1["items"] + page2["items"]]
    assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen))

    r = await client.get("/v1/daily-diaries", params={"cursor": "not-a-cursor"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "INVALID_CURSOR"
