"""날짜별(멀티콜) 생성 잡 — 산출물/전사/토큰 purge/콜백/재시도/복구."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from app.db import repo
from app.db.models import utcnow
from app.worker.recovery import recover
from tests.conftest import full_flow

pytestmark = pytest.mark.asyncio

DAILY = {"diary_id": "daily_u1_20260820", "diary_date": "2026-08-20", "call_ids": ["c1", "c2"]}


async def _complete_calls(client, app, ids=("c1", "c2")):
    for cid in ids:
        await full_flow(client, cid)
    await app.state.rt.worker.drain()


async def _run_inflight(rt):
    await rt.worker.run_once()
    await asyncio.gather(*list(rt.worker._inflight), return_exceptions=True)


async def test_daily_full_flow_artifacts_and_purge(client, app, stt_mock, s3_env):
    rt = app.state.rt
    await _complete_calls(client, app)
    r = await client.post("/v1/daily-diaries", json={**DAILY, "farm_access_token": "eyJ.daily.token"})
    assert r.status_code == 201
    await rt.worker.drain()

    body = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}")).json()
    assert body["status"] == "COMPLETED"

    async with rt.db.session() as s:
        dd = await repo.get_daily(s, DAILY["diary_id"])
        arts = await repo.list_daily_artifacts(s, DAILY["diary_id"])
    assert dd.farm_access_token is None                              # terminal 시 purge
    kinds = sorted({a.kind for a in arts})
    assert kinds == ["diary_json", "diary_md", "result_json", "transcript"]   # report 없음
    assert all(a.s3_key.startswith("agents/voicecall/daily/") for a in arts)

    # 병합 전사: 통화 2건이 전역 파일 인덱스로 리베이스
    tr = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}/transcript")).json()
    assert [f["file_index"] for f in tr["files"]] == [0, 1]
    assert sorted({f["call_id"] for f in tr["files"]}) == ["c1", "c2"]
    # 산출물 날짜는 요청 diary_date 로 고정 (hints.diary_date 경유)
    diary = body["result"]["diaries"][0]
    assert diary["diary_date"] == "2026-08-20"

    # 기존 call 산출물은 건드리지 않음 (별도 리소스 공존)
    for cid in ("c1", "c2"):
        cbody = (await client.get(f"/v1/calls/{cid}")).json()
        assert cbody["status"] == "COMPLETED" and cbody["result"]["report"] is not None


async def test_daily_callback(client, app, stt_mock):
    rt = app.state.rt
    await _complete_calls(client, app)
    rt.settings.callback_enabled = True
    try:
        with respx.mock(assert_all_called=True) as router:
            route = router.post("https://cb.test/hook").mock(return_value=httpx.Response(200))
            await client.post("/v1/daily-diaries", json={**DAILY, "callback_url": "https://cb.test/hook"})
            await rt.worker.drain()
    finally:
        rt.settings.callback_enabled = False
    import json
    payload = json.loads(route.calls[0].request.content)
    assert payload["daily_diary_id"] == DAILY["diary_id"] and payload["status"] == "COMPLETED"
    assert payload["call_ids"] == ["c1", "c2"] and payload["diary_date"] == "2026-08-20"
    assert f"/v1/daily-diaries/{DAILY['diary_id']}" in payload["result_url"]
    body = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}")).json()
    assert body["callback_status"] == "SENT"


async def test_daily_retry_then_failed(client, app, stt_mock, monkeypatch):
    rt = app.state.rt
    await _complete_calls(client, app)

    async def boom(transcript, ctx):
        raise RuntimeError("llm down")

    monkeypatch.setattr(rt.pipeline, "run", boom)
    await client.post("/v1/daily-diaries", json=DAILY)

    await _run_inflight(rt)                                          # attempt 1 → 재큐(+60s)
    async with rt.db.session() as s:
        dd = await repo.get_daily(s, DAILY["diary_id"])
        assert dd.gen_state == "QUEUED" and dd.status == "PROCESSING"
        assert dd.generation_run == 0                                # 실패한 시도는 run 미소비
        dd.gen_next_attempt_at = utcnow()
        await s.commit()

    await _run_inflight(rt)                                          # attempt 2 (gen_max_attempts=2) → FAILED
    body = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}")).json()
    assert body["status"] == "FAILED" and body["error"]["code"] == "GENERATION_FAILED"
    assert "llm down" in body["error"]["message"]


async def test_daily_recovery_resets_running(client, app, stt_mock):
    rt = app.state.rt
    await _complete_calls(client, app)
    await client.post("/v1/daily-diaries", json=DAILY)
    async with rt.db.session() as s:
        dd = await repo.get_daily(s, DAILY["diary_id"])
        dd.gen_state = "RUNNING"                                     # 크래시로 남은 상태 가정
        await s.commit()
    stats = await recover(rt)
    assert stats["daily_reset"] == 1
    async with rt.db.session() as s:
        dd = await repo.get_daily(s, DAILY["diary_id"])
        assert dd.gen_state == "QUEUED"
    await rt.worker.drain()
    assert (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}")).json()["status"] == "COMPLETED"
