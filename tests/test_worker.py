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
    """COMPLETED → content 는 **통화 단순요약 불릿**. 일지·보고서 마크다운은 싣지 않는다.

    콜백 전송은 기존 S3 저장·결과 조회를 대체하지 않는다 — 일지/보고서/전사는 §6 경로에 그대로
    올라가고 call_id 로 조회된다(백엔드 전달 명세 §3.5/§3.6 회귀 가드).
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
    content = payload["content"]
    assert content.startswith("- 주제:")                    # 불릿 요약
    assert len(content.splitlines()) <= 3
    assert "# 영농일지" not in content and "컨설팅 보고서" not in content   # 일지·보고서는 결과 API 로만
    assert "|" not in content                              # 표·서식 없음
    assert payload["engine_version"].startswith("jinong-summary-v1")
    assert "fail_reason" not in payload and "empty_reason" not in payload

    # (1) call_id 로 조회 — 일지·보고서·전사 키가 종전대로 노출된다
    body = (await client.get("/v1/calls/cb1")).json()
    assert body["callback_status"] == "SENT"
    res = body["result"]
    diary = res["diaries"][0]
    assert diary["s3_key_md"] == "agents/voicecall/cb1/artifacts/diary/0804MM.md"
    assert diary["markdown"].startswith("# 영농일지 —")      # 일지는 응답으로 그대로 나간다
    assert res["report"]["s3_key_md"] == "agents/voicecall/cb1/artifacts/report.md"
    assert res["transcript_key"] == "agents/voicecall/cb1/transcript/merged.json"
    # (2) 요약도 결과에 실려 id 로 다시 꺼낼 수 있다
    assert res["summary"]["markdown"] == content
    assert res["summary"]["s3_key_md"] == "agents/voicecall/cb1/artifacts/summary.md"
    assert res["summary"]["structured"]["source"] == "fake"

    # (3) S3 에 실제 객체가 있고, 콜백 content 는 저장된 summary.md 와 같다
    keys = {o["Key"] for o in s3_env.list_objects_v2(Bucket=BUCKET, Prefix="agents/voicecall/cb1/")["Contents"]}
    assert {"agents/voicecall/cb1/call.json",
            "agents/voicecall/cb1/transcript/merged.json",
            "agents/voicecall/cb1/transcript/merged.md",
            "agents/voicecall/cb1/artifacts/diary/0804MM.md",
            "agents/voicecall/cb1/artifacts/diary/0804MM.json",
            "agents/voicecall/cb1/artifacts/report.md",
            "agents/voicecall/cb1/artifacts/report.json",
            "agents/voicecall/cb1/artifacts/summary.md",
            "agents/voicecall/cb1/artifacts/summary.json",
            "agents/voicecall/cb1/artifacts/result.json"} <= keys
    stored = s3_env.get_object(Bucket=BUCKET, Key=res["summary"]["s3_key_md"])["Body"].read().decode()
    assert stored.strip() == content

    # (4) artifact 엔드포인트로도 id 조회 가능
    r = await client.get("/v1/calls/cb1/artifacts/summary")
    assert r.status_code == 200 and r.text.strip() == content
    r = await client.get("/v1/calls/cb1/artifacts/diary/0804MM")
    assert r.status_code == 200 and "영농일지" in r.text
    r = await client.get("/v1/calls/cb1/artifacts/report")
    assert r.status_code == 200 and "컨설팅 보고서" in r.text

    # (5) 전사본도 종전대로
    tr = (await client.get("/v1/calls/cb1/transcript")).json()
    assert tr["call_id"] == "cb1" and tr["segments"] and tr["speakers"]


async def test_summary_callback_is_one_summary_regardless_of_crops(client, app, stt_mock):
    """작물이 2건이어도 content 는 통화 요약 1건 — 일지를 이어붙이지 않는다."""
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
    assert "---" not in content and "# 영농일지" not in content
    assert content.startswith("- 주제:")
    # 작물별 일지는 응답에 2건 그대로
    body = (await client.get("/v1/calls/cb2")).json()
    assert [d["prdlst_nm"] for d in body["result"]["diaries"]] == ["딸기", "파프리카"]


async def test_summary_falls_back_to_report_when_summarizer_fails(client, app, stt_mock):
    """요약 LLM 실패 → 통화는 COMPLETED 유지, 보고서 요약으로 폴백하고 warning 을 남긴다."""
    import json

    rt = app.state.rt

    class BoomSummarizer:
        async def summarize(self, transcript, ctx):
            raise RuntimeError("summary llm down")

    restore = _enable_summary_callback(rt)
    orig = rt.summarizer
    rt.summarizer = BoomSummarizer()
    try:
        with respx.mock(assert_all_called=True) as router:
            route = router.post(SUMMARY_HOOK).mock(return_value=httpx.Response(200))
            await full_flow(client, "cb6")
            await rt.worker.drain()
    finally:
        rt.summarizer = orig
        restore()

    payload = json.loads(route.calls[0].request.content)
    assert payload["status"] == "COMPLETED"
    assert payload["content"].startswith("- 주제:")          # FakePipeline 보고서 summary="fake"
    body = (await client.get("/v1/calls/cb6")).json()
    assert body["status"] == "COMPLETED"
    assert any("통화 단순요약 실패" in w for w in body["generation"]["warnings"])
    assert body["result"]["summary"]["structured"]["source"] == "report_fallback"


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
    assert payload["empty_reason"] == "NO_AUDIO"


async def test_summary_callback_empty_diaries_are_not_sent(client, app, stt_mock):
    """일지가 전부 빈 템플릿(EMPTY) → 본문을 싣지 않고 EMPTY + NO_DIARY_CONTENT 로 낮춰 보낸다.

    영농일지와 무관한 통화에서 백엔드가 빈 템플릿을 받던 문제(백엔드 요청)의 회귀 테스트.
    """
    import json

    from app.schemas.pipeline import DiaryArtifact, PipelineResult

    rt = app.state.rt

    class EmptyDiaryPipeline:
        """통화 자체는 COMPLETED 인데(보고서 있음) 일지에 남길 내용은 없는 경우."""

        async def run(self, transcript, ctx):
            d = DiaryArtifact(prdlst_code="0804MM", prdlst_nm="딸기", diary_date="2026-08-26",
                              status="EMPTY", markdown="# 영농일지 — 딸기 (2026-08-26)\n\n## 주요 농작업\n- 언급 없음\n")
            return PipelineResult(diaries=[d], report=None, model="fake", prompt_version="0")

    restore = _enable_summary_callback(rt)
    orig = rt.pipeline
    rt.pipeline = EmptyDiaryPipeline()
    try:
        with respx.mock(assert_all_called=True) as router:
            route = router.post(SUMMARY_HOOK).mock(return_value=httpx.Response(200))
            await full_flow(client, "cb5")
            await rt.worker.drain()
    finally:
        rt.pipeline = orig
        restore()

    payload = json.loads(route.calls[0].request.content)
    assert payload["status"] == "EMPTY"
    assert "content" not in payload                       # 빈 템플릿은 실리지 않는다
    assert payload["empty_reason"] == "NO_DIARY_CONTENT"

    body = (await client.get("/v1/calls/cb5")).json()     # 통화 자체는 COMPLETED, 산출물은 남아 있다
    assert body["status"] == "COMPLETED"
    assert body["result"]["diaries"][0]["status"] == "EMPTY"


async def test_summary_callback_no_summary_folds_to_no_content(client, app, stt_mock):
    """일지는 있는데 요약이 폴백까지 실패 → 백엔드 허용값 NO_CONTENT 로 접어 보낸다(구 NO_SUMMARY 제거)."""
    import json

    from app.schemas.pipeline import DiaryArtifact, PipelineResult
    from app.worker.generate_job import ALLOWED_EMPTY_REASONS

    rt = app.state.rt

    class DiaryOnlyPipeline:
        """실질 일지는 있으나 보고서가 없어 요약 폴백도 불가능한 경우."""

        async def run(self, transcript, ctx):
            d = DiaryArtifact(prdlst_code="0804MM", prdlst_nm="딸기", diary_date="2026-08-26",
                              status="OK", markdown="# 영농일지 — 딸기 (2026-08-26)\n\n## 주요 농작업\n- 관수 2시간\n")
            return PipelineResult(diaries=[d], report=None, model="fake", prompt_version="0")

    restore = _enable_summary_callback(rt)
    orig_pipeline, orig_summarizer = rt.pipeline, rt.summarizer
    rt.pipeline = DiaryOnlyPipeline()
    rt.summarizer = None                                  # 요약 패스 자체가 없음 → content 를 만들 수 없다
    try:
        with respx.mock(assert_all_called=True) as router:
            route = router.post(SUMMARY_HOOK).mock(return_value=httpx.Response(200))
            await full_flow(client, "cb6")
            await rt.worker.drain()
    finally:
        rt.pipeline, rt.summarizer = orig_pipeline, orig_summarizer
        restore()

    payload = json.loads(route.calls[0].request.content)
    assert payload["status"] == "EMPTY" and "content" not in payload
    assert payload["empty_reason"] == "NO_CONTENT"
    assert payload["empty_reason"] in ALLOWED_EMPTY_REASONS


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


# --- 화자 역할(농가/컨설턴트) 되먹임 -------------------------------------------


class RolePipeline:
    """speaker_map 을 실제 역할로 돌려주는 파이프라인 (fake 는 전부 unknown)."""

    async def run(self, transcript, ctx):
        from app.schemas.pipeline import DiaryArtifact, PipelineResult

        roles = ["consultant", "farmer"]
        return PipelineResult(
            diaries=[DiaryArtifact(prdlst_code="0804MM", prdlst_nm="딸기", diary_date="2026-08-26",
                                   status="OK", markdown="# 영농일지 — 딸기 (2026-08-26)\n\n본문\n")],
            report=None, model="fake", prompt_version="0",
            speaker_map={k: roles[i % 2] for i, k in enumerate(transcript.speakers)})


async def test_transcript_carries_roles_after_generation(client, app, stt_mock):
    """생성이 끝나면 merged.json 이 역할과 함께 다시 쓰인다 — GET /transcript 가 농가/컨설턴트를 그대로 준다."""
    rt = app.state.rt
    orig = rt.pipeline
    rt.pipeline = RolePipeline()
    try:
        await full_flow(client, "role1")
        await rt.worker.drain()
    finally:
        rt.pipeline = orig

    tr = (await client.get("/v1/calls/role1/transcript")).json()
    assert tr["speaker_map"] == {"f0:A": "consultant", "f0:B": "farmer"}
    by_key = {s["speaker_key"]: s["role"] for s in tr["segments"]}
    assert by_key == {"f0:A": "consultant", "f0:B": "farmer"}
    assert all(s["role"] in ("farmer", "consultant") for s in tr["segments"])
    # 결과 API 의 speaker_map 과 같은 값 (백엔드가 둘 중 무엇을 봐도 일치)
    assert (await client.get("/v1/calls/role1")).json()["result"]["speaker_map"] == tr["speaker_map"]
    # 사람이 읽는 md 도 한글 라벨
    md = (await rt.s3.get_bytes(rt.settings.s3_bucket,
                                "agents/voicecall/role1/transcript/merged.md")).decode("utf-8")
    assert "컨설턴트(f0:A):" in md and "농가(f0:B):" in md


async def test_transcript_roles_unknown_when_pipeline_cannot_tell(client, app, stt_mock):
    """역할 추정이 안 되면(fake=전부 unknown) role 은 unknown 으로 남는다 — 추측하지 않는다."""
    rt = app.state.rt
    await full_flow(client, "role2")
    await rt.worker.drain()
    tr = (await client.get("/v1/calls/role2/transcript")).json()
    assert {s["role"] for s in tr["segments"]} == {"unknown"}
    assert set(tr["speaker_map"].values()) == {"unknown"}


async def test_summary_callback_carries_speaker_map(client, app, stt_mock):
    """통화요약 콜백에 화자 역할표를 동봉 — 끄면 필드 자체가 빠진다."""
    import json

    rt = app.state.rt
    restore = _enable_summary_callback(rt)
    orig = rt.pipeline
    rt.pipeline = RolePipeline()
    try:
        with respx.mock(assert_all_called=True) as router:
            route = router.post(SUMMARY_HOOK).mock(return_value=httpx.Response(200))
            await full_flow(client, "role3")
            await rt.worker.drain()
        assert json.loads(route.calls[0].request.content)["speaker_map"] == {"f0:A": "consultant", "f0:B": "farmer"}

        rt.settings.callback_include_speaker_map = False
        with respx.mock(assert_all_called=True) as router:
            route = router.post(SUMMARY_HOOK).mock(return_value=httpx.Response(200))
            await full_flow(client, "role4")
            await rt.worker.drain()
        assert "speaker_map" not in json.loads(route.calls[0].request.content)
    finally:
        rt.settings.callback_include_speaker_map = True
        rt.pipeline = orig
        restore()
