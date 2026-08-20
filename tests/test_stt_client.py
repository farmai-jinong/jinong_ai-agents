import httpx
import pytest
import respx

from app.clients.stt import SttClient, SttError, backoff_delay, parse_diarized
from app.config import Settings

URL = "https://stt.test"


def _client() -> SttClient:
    return SttClient(Settings(stt_base_url=URL, stt_api_key="k", stt_timeout=5, allow_no_auth=True))


def test_parse_sample(stt_sample):
    r = parse_diarized(stt_sample)
    assert r.seconds == 77 and len(r.segments) == 4
    assert r.segments[0]["text"].startswith("안녕하세요")     # 선행 공백 제거
    assert r.segments[1]["speaker"] == "B"


def test_parse_multi_chunk_permanent(stt_sample):
    with pytest.raises(SttError) as e:
        parse_diarized(stt_sample + stt_sample)
    assert e.value.permanent and "STT_MULTI_CHUNK" in str(e.value)


@pytest.mark.asyncio
async def test_diarize_ok(stt_sample):
    with respx.mock() as router:
        route = router.post(f"{URL}/v1/audio/transcriptions").mock(return_value=httpx.Response(200, json=stt_sample))
        r = await _client().diarize(b"abc", "a.wav", 2)
        assert r.seconds == 77
        req = route.calls[0].request
        assert req.headers["authorization"] == "Bearer k"
        body = req.content
        assert b'name="diarize"' in body and b"true" in body and b'name="num_speakers"' in body


@pytest.mark.asyncio
@pytest.mark.parametrize("status,permanent", [(413, True), (415, True), (400, True), (401, True),
                                              (502, False), (503, False), (504, False), (500, False)])
async def test_status_classification(status, permanent):
    with respx.mock() as router:
        router.post(f"{URL}/v1/audio/transcriptions").mock(return_value=httpx.Response(status, text="x"))
        with pytest.raises(SttError) as e:
            await _client().diarize(b"abc", "a.wav")
        assert e.value.permanent is permanent


@pytest.mark.asyncio
async def test_429_retry_after():
    with respx.mock() as router:
        router.post(f"{URL}/v1/audio/transcriptions").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "45"}))
        with pytest.raises(SttError) as e:
            await _client().diarize(b"abc", "a.wav")
        assert not e.value.permanent and e.value.retry_after == 45.0


@pytest.mark.asyncio
async def test_timeout_transient():
    with respx.mock() as router:
        router.post(f"{URL}/v1/audio/transcriptions").mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(SttError) as e:
            await _client().diarize(b"abc", "a.wav")
        assert not e.value.permanent


def test_backoff():
    assert 15 <= backoff_delay(0) <= 18.1
    assert 300 <= backoff_delay(10) <= 360.1
