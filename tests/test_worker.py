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


# --- 통화요약 콜백 (백엔드 call-summary-callback) ---------------------------------

SUMMARY_HOOK = "https://cb.test/voicetalk/public/call-summary-callback"


def _enable_summary_callback(rt):
    """콜백 설정을 켜고 원복 함수를 돌려준다 (기본 픽스처는 callback_enabled=False)."""
    prev = (rt.settings.callback_enabled, rt.settings.summary_callback_url, rt.settings.callback_api_key)

    def restore():
        (rt.settings.callback_enabled, rt.settings.summary_callback_url,
         rt.settings.callback_api_key) = prev

    rt.settings.callback_enabled = True
    rt.settings.summary_callback_url = SUMMARY_HOOK
    rt.settings.callback_api_key = "cb-secret"
    return restore


async def test_summary_callback_completed(client, app, stt_mock, s3_env):
    """COMPLETED → 일지 마크다운 원문을 content 로 전송. 컨설팅 보고서는 포함하지 않는다.

    콜백 전송은 기존 S3 저장을 대체하지 않는다 — 산출물은 §6 경로에 그대로 올라가고,
    call_id 로 조회(GET /v1/calls/{id}, artifact 엔드포인트)도 종전과 같이 동작한다.
    """
    import json

    from tests.conftest import BUCKET

    rt = app.state.rt
    restore = _enable_summary_callback(rt)
    try:
        with respx.mock(assert_all_called=True) as router:
            route = router.post(SUMMARY_HOOK).mock(return_value=httpx.Response(200))
            await full_flow(client, "cb1")
            await rt.worker.drain()
    finally:
        restore()

    req = route.calls[0].request
    assert req.headers["X-API-Key"] == "cb-secret"
    payload = json.loads(req.content)
    assert payload["call_id"] == "cb1"
    assert payload["summary_type"] == "SUMMARY"
    assert payload["status"] == "COMPLETED"
    assert payload["content"].startswith("# 영농일지 —")
    assert "컨설팅 보고서" not in payload["content"]              # 보고서는 내부 저장 전용
    assert payload["engine_version"].startswith("jinong-diary-v1")
    assert "fail_reason" not in payload

    # (1) call_id 로 조회 — 결과·산출물 키가 그대로 노출된다
    body = (await client.get("/v1/calls/cb1")).json()
    assert body["callback_status"] == "SENT"
    diary = body["result"]["diaries"][0]
    assert diary["s3_key_md"] == "agents/voicecall/cb1/artifacts/diary/0804MM.md"
    assert body["result"]["report"]["s3_key_md"] == "agents/voicecall/cb1/artifacts/report.md"
    assert body["result"]["transcript_key"] == "agents/voicecall/cb1/transcript/merged.json"

    # (2) S3 에 실제 객체가 있고, 콜백 content 는 저장된 일지 본문과 같다
    keys = {o["Key"] for o in s3_env.list_objects_v2(Bucket=BUCKET, Prefix="agents/voicecall/cb1/")["Contents"]}
    assert {"agents/voicecall/cb1/call.json",
            "agents/voicecall/cb1/transcript/merged.json",
            "agents/voicecall/cb1/transcript/merged.md",
            "agents/voicecall/cb1/artifacts/diary/0804MM.md",
            "agents/voicecall/cb1/artifacts/diary/0804MM.json",
            "agents/voicecall/cb1/artifacts/report.md",
            "agents/voicecall/cb1/artifacts/report.json",
            "agents/voicecall/cb1/artifacts/result.json"} <= keys
    stored = s3_env.get_object(Bucket=BUCKET, Key=diary["s3_key_md"])["Body"].read().decode()
    assert stored.strip() == payload["content"]

    # (3) artifact 엔드포인트로도 id 조회 가능 (보고서는 여기서만 나온다)
    r = await client.get("/v1/calls/cb1/artifacts/diary/0804MM")
    assert r.status_code == 200 and r.text.strip() == payload["content"]
    r = await client.get("/v1/calls/cb1/artifacts/report")
    assert r.status_code == 200 and "컨설팅 보고서" in r.text


async def test_summary_callback_multi_crop_joined(client, app, stt_mock):
    """작물 2건 → content 는 구분선으로 병합한 1건."""
    import json

    from app.schemas.pipeline import DiaryArtifact, PipelineResult

    rt = app.state.rt

    class TwoCropPipeline:
        async def run(self, transcript, ctx):
            def mk(code, nm):
                return DiaryArtifact(prdlst_code=code, prdlst_nm=nm, diary_date="2026-08-26",
                                     status="OK", markdown=f"# 영농일지 — {nm} (2026-08-26)\n\n본문\n")
            return PipelineResult(diaries=[mk("0804MM", "딸기"), mk("0805MM", "파프리카")],
                                  report=None, model="fake", prompt_version="0")

    restore = _enable_summary_callback(rt)
    orig = rt.pipeline
    rt.pipeline = TwoCropPipeline()
    try:
        with respx.mock(assert_all_called=True) as router:
            route = router.post(SUMMARY_HOOK).mock(return_value=httpx.Response(200))
            await full_flow(client, "cb2")
            await rt.worker.drain()
    finally:
        rt.pipeline = orig
        restore()

    content = json.loads(route.calls[0].request.content)["content"]
    assert content.count("\n\n---\n\n") == 1
    assert "딸기" in content and "파프리카" in content


async def test_summary_callback_empty_has_no_content(client, app):
    """오디오 없이 종료 → EMPTY, content 없음(명세상 COMPLETED 일 때만 필수)."""
    import json

    rt = app.state.rt
    restore = _enable_summary_callback(rt)
    try:
        with respx.mock(assert_all_called=True) as router:
            route = router.post(SUMMARY_HOOK).mock(return_value=httpx.Response(200))
            await client.post("/v1/calls", json={"call_id": "cb3"})
            await client.post("/v1/calls/cb3/end")
            await rt.worker.drain()
    finally:
        restore()

    payload = json.loads(route.calls[0].request.content)
    assert payload["status"] == "EMPTY" and "content" not in payload


async def test_summary_callback_failed_has_reason(client, app, stt_mock):
    """생성 실패 → FAILED + fail_reason."""
    import json

    rt = app.state.rt
    restore = _enable_summary_callback(rt)
    orig = rt.pipeline

    class Boom:
        async def run(self, t, c):
            raise RuntimeError("llm down")

    rt.pipeline = Boom()
    try:
        with respx.mock(assert_all_called=True) as router:
            route = router.post(SUMMARY_HOOK).mock(return_value=httpx.Response(200))
            await full_flow(client, "cb4")
            for _ in range(3):
                await rt.worker.drain(timeout=5)
                async with rt.db.session() as s:
                    call = await repo.get_call(s, "cb4")
                    call.gen_next_attempt_at = utcnow()
                    await s.commit()
    finally:
        rt.pipeline = orig
        restore()

    payload = json.loads(route.calls[0].request.content)
    assert payload["status"] == "FAILED"
    assert payload["fail_reason"].startswith("GENERATION_FAILED:")
    assert "content" not in payload


async def test_summary_callback_disabled_without_url(client, app, stt_mock):
    """summary_callback_url 이 비면 발사하지 않는다 (기본값)."""
    rt = app.state.rt
    prev = rt.settings.callback_enabled
    rt.settings.callback_enabled = True
    try:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(SUMMARY_HOOK).mock(return_value=httpx.Response(200))
            await full_flow(client, "cb5")
            await rt.worker.drain()
    finally:
        rt.settings.callback_enabled = prev
    assert not route.called
    body = (await client.get("/v1/calls/cb5")).json()
    assert body["status"] == "COMPLETED" and body["callback_status"] is None


async def test_send_callback_no_retry_on_4xx(settings):
    """4xx 는 재시도하지 않는다 (명세 권고: 400/401/404 반복 재시도 금지). 5xx 는 재시도."""
    from app.clients.callback import send_callback

    settings.callback_api_key = "cb-secret"
    with respx.mock(assert_all_called=True) as router:
        route = router.post(SUMMARY_HOOK).mock(return_value=httpx.Response(400, text="missing content"))
        ok, attempts = await send_callback(settings, SUMMARY_HOOK, {"call_id": "x"}, delays=(0.0, 0.0, 0.0))
    assert (ok, attempts) == (False, 1) and route.call_count == 1

    with respx.mock(assert_all_called=True) as router:
        route = router.post(SUMMARY_HOOK).mock(return_value=httpx.Response(500))
        ok, attempts = await send_callback(settings, SUMMARY_HOOK, {"call_id": "x"}, delays=(0.0, 0.0, 0.0))
    assert (ok, attempts) == (False, 3) and route.call_count == 3
