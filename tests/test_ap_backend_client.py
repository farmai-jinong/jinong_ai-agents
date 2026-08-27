"""AP 백엔드 research API 클라이언트 (백엔드 문서 §3·§4)."""

import httpx
import pytest
import respx

from app.clients.ap_backend import ApBackendAuthError, ApBackendClient, ApBackendError

BASE = "https://ap.test"


def _client() -> ApBackendClient:
    return ApBackendClient(BASE, "cb-key", retries=0)


@pytest.mark.asyncio
async def test_farm_context_maps_to_farmos_shape():
    with respx.mock as router:
        route = router.get(f"{BASE}/voicetalk/public/research/farm-context").mock(
            return_value=httpx.Response(200, json={
                "farmer_engn_id": "1", "farmer_user_id": "test7",
                "crops": [{"prdlst_code": "0804MM", "prdlst_nm": "딸기", "representative": True},
                          {"prdlst_code": "0805TT", "prdlst_nm": "토마토", "representative": False},
                          {"prdlst_code": "0806XX", "prdlst_nm": None}]}))
        async with _client() as c:
            rows = await c.farm_context("1", "test7")
    req = route.calls[0].request
    assert req.headers["X-API-Key"] == "cb-key"
    assert dict(httpx.URL(str(req.url)).params) == {"farmer_engn_id": "1", "farmer_user_id": "test7"}
    # 이름 없는 행은 버리고, representative 는 farmos 의 reprsntPrdlstCnt 눈금으로 옮긴다
    assert rows == [{"prdlstCode": "0804MM", "prdlstNm": "딸기", "reprsntPrdlstCnt": 1},
                    {"prdlstCode": "0805TT", "prdlstNm": "토마토", "reprsntPrdlstCnt": None}]


@pytest.mark.asyncio
async def test_missing_crops_key_is_empty_not_error():
    with respx.mock as router:
        router.get(f"{BASE}/voicetalk/public/research/farm-context").mock(
            return_value=httpx.Response(200, json={"farmer_engn_id": "1"}))
        async with _client() as c:
            assert await c.farm_context("1", "test7") == []


@pytest.mark.asyncio
async def test_401_is_auth_error():
    with respx.mock as router:
        router.get(f"{BASE}/voicetalk/public/research/farm-context").mock(return_value=httpx.Response(401))
        async with _client() as c:
            with pytest.raises(ApBackendAuthError):
                await c.farm_context("1", "test7")


@pytest.mark.asyncio
async def test_404_prdlst_is_none():
    with respx.mock as router:
        router.get(f"{BASE}/voicetalk/public/research/prdlsts/9999").mock(return_value=httpx.Response(404))
        async with _client() as c:
            assert await c.prdlst("9999") is None


@pytest.mark.asyncio
async def test_prdlst_returns_body():
    body = {"engn_id": 1, "prdlst_code": "0804MM", "mlsfc_code_nm": "딸기", "use_yn": "Y"}
    with respx.mock as router:
        router.get(f"{BASE}/voicetalk/public/research/prdlsts/0804MM").mock(return_value=httpx.Response(200, json=body))
        async with _client() as c:
            assert await c.prdlst("0804MM") == body


@pytest.mark.asyncio
async def test_5xx_raises_after_retries():
    with respx.mock as router:
        router.get(f"{BASE}/voicetalk/public/research/farm-context").mock(return_value=httpx.Response(503, text="down"))
        async with _client() as c:
            with pytest.raises(ApBackendError):
                await c.farm_context("1", "test7")
