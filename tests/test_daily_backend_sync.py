"""날짜별 일지 재-POST/재생성 시 백엔드 DB 갱신 — 첫 생성 무변경, 재-POST 는 멤버 통화·힌트 갱신, crop(고정) 보존."""

from __future__ import annotations

from tests.conftest import full_flow
from tests.test_backend_sync import CROPS, ROW, FakeBackendDb

DAILY = {"diary_id": "daily_1_test7_20260820", "diary_date": "2026-08-20", "call_ids": ["d1", "d2"]}


async def _complete_calls(client, app):
    for cid in DAILY["call_ids"]:
        await full_flow(client, cid)
    await app.state.rt.worker.drain()


async def test_daily_first_run_untouched_then_repost_refreshes(client, app, stt_mock, s3_env):
    rt = app.state.rt
    rt.backend_db = FakeBackendDb(row=None)          # STT 시점엔 통화 행 없음(스냅샷 유지)
    await _complete_calls(client, app)
    rt.backend_db = FakeBackendDb(row=ROW, crops=CROPS)
    r = await client.post("/v1/daily-diaries", json={**DAILY, "crop": {"prdlst_nm": "토마토"}})
    assert r.status_code == 201
    await rt.worker.drain()
    body = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}")).json()
    assert body["status"] == "COMPLETED" and body["generation"]["run"] == 1
    assert rt.backend_db.call_ids == []                # 첫 생성(run 1)은 DB 를 보지 않는다
    assert "backend_refresh" not in (body["metadata"] or {})

    r = await client.post("/v1/daily-diaries", json=DAILY)          # 재-POST(같은 diary_id) → run 2
    assert r.status_code == 200 and r.json()["note"] == "regeneration queued", r.text
    await rt.worker.drain()
    body = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}")).json()
    assert body["status"] == "COMPLETED" and body["generation"]["run"] == 2
    assert sorted(rt.backend_db.call_ids) == ["d1", "d2"]
    assert body["crop"] == {"prdlst_code": None, "prdlst_nm": "토마토"}          # 고정 작물 보존
    md = body["metadata"]
    assert md["backend_refresh"]["reason"] == "regenerate" and md["backend_refresh"]["crops"] == 2
    assert [c["prdlstCode"] for c in md["hints"]["farmer_crops"]] == ["1326MM", "0803MM"]
    assert md["hints"]["farmer_engn_id"] == "1"
    # 멤버 통화도 갱신됐다
    c = (await client.get("/v1/calls/d1")).json()
    assert [(p["role"], p["user_id"]) for p in c["participants"]] == [("farmer", "test7"), ("consultant", "cons7")]
