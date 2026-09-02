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
    assert kinds == ["diary_json", "diary_md", "diary_md_internal", "result_json", "transcript"]   # report 없음
    assert all(a.s3_key.startswith("agents/voicecall/daily/") for a in arts)
    internal = [a for a in arts if a.kind == "diary_md_internal"][0]
    assert internal.s3_key == f"agents/voicecall/daily/{DAILY['diary_id']}/artifacts/internal/diary/{internal.prdlst_code}.md"
    assert "## 근거 발화" in internal.content and "## 근거 발화" not in [a for a in arts if a.kind == "diary_md"][0].content

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
    # 산출물 키(본문 없음) — daily 는 report 없음
    assert "report" not in payload and len(payload["diaries"]) == 1
    dk = payload["diaries"][0]
    assert dk["s3_key_md"] == f"agents/voicecall/daily/{DAILY['diary_id']}/artifacts/diary/{dk['prdlst_code']}.md"
    assert dk["s3_key_md_internal"] == f"agents/voicecall/daily/{DAILY['diary_id']}/artifacts/internal/diary/{dk['prdlst_code']}.md"
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


async def test_daily_multiple_unresolved_crops_no_collision(client, app, stt_mock, s3_env):
    """미확정(prdlst_code=None) 작물 일지가 2건 이상이어도 UNIQUE/S3 키 충돌 없이 COMPLETED.

    회귀: 멀티콜 daily 에서 통화별 작물이 모두 미확정이면 전부 'unresolved' 코드로 저장돼
    daily_artifacts UNIQUE(diary_id, kind, prdlst_code) 위반 → GENERATION_FAILED 였다.
    """
    from app.schemas.pipeline import DiaryArtifact, PipelineResult

    rt = app.state.rt
    await _complete_calls(client, app)

    class TwoUnresolvedPipeline:
        async def run(self, transcript, ctx):
            def mk(nm):
                return DiaryArtifact(prdlst_code=None, prdlst_nm=nm, diary_date="2026-08-20",
                                     status="PARTIAL", markdown=f"# 영농일지 — {nm}", markdown_public=f"# 영농일지 — {nm}")
            return PipelineResult(diaries=[mk("벼"), mk("콩")], report=None,
                                  model="fake", prompt_version="0")

    orig = rt.pipeline
    rt.pipeline = TwoUnresolvedPipeline()
    try:
        r = await client.post("/v1/daily-diaries", json=DAILY)
        assert r.status_code == 201
        await rt.worker.drain()
    finally:
        rt.pipeline = orig

    body = (await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}")).json()
    assert body["status"] == "COMPLETED", body.get("error")
    diaries = body["result"]["diaries"]
    assert [d["prdlst_nm"] for d in diaries] == ["벼", "콩"]
    assert [d["prdlst_code"] for d in diaries] == [None, None]       # 응답에서는 둘 다 미확정
    base = f"agents/voicecall/daily/{DAILY['diary_id']}/artifacts/diary"
    assert {d["s3_key_md"] for d in diaries} == {f"{base}/unresolved.md", f"{base}/unresolved-2.md"}
    # 두 번째 미확정 일지도 산출물 GET 으로 접근 가능
    r = await client.get(f"/v1/daily-diaries/{DAILY['diary_id']}/artifacts/diary/unresolved-2")
    assert r.status_code == 200 and "콩" in r.text
