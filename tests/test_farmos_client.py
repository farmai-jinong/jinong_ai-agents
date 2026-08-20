import httpx
import pytest
import respx

from app.clients.farmos import FarmosAuthError, FarmosClient, FarmosError, expand_dbyhs

BASE = "https://farmos.test"


def env(data, status=200, msg="완료되었습니다."):
    return httpx.Response(200, json={"timestamp": "t", "statusCode": status, "message": msg, "data": data})


@pytest.mark.asyncio
async def test_envelope_unwrap_and_first_call():
    with respx.mock() as router:
        router.get(f"{BASE}/m/diary/user/prdlsts/list").mock(
            return_value=env([{"prdlstCode": "0804MM", "prdlstNm": "딸기", "use": True, "reprsntPrdlstCnt": 1}]))
        async with FarmosClient(BASE, "tok") as c:
            crops = await c.list_crops()
        assert crops[0]["prdlstCode"] == "0804MM"
        assert router.calls[0].request.headers["authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_401_auth_error():
    with respx.mock() as router:
        router.get(f"{BASE}/m/diary/user/prdlsts/list").mock(return_value=httpx.Response(401))
        async with FarmosClient(BASE, "bad") as c:
            with pytest.raises(FarmosAuthError):
                await c.list_crops()


@pytest.mark.asyncio
async def test_envelope_error_status():
    with respx.mock() as router:
        router.get(f"{BASE}/m/diary/2026-08-19/0804MM/detail").mock(return_value=env(None, 500, "오류"))
        async with FarmosClient(BASE, "tok", retries=0) as c:
            with pytest.raises(FarmosError):
                await c.diary_detail("2026-08-19", "0804MM")


def test_expand_dbyhs():
    row = expand_dbyhs({"dbyhsCode": "002006001", "dbyhsNm": "눈마름병",
                        "occrrncStepNm": "미발생|2%미만|5%미만|10%미만|30%미만|30%이상",
                        "occrrncStepCode": "0|1|2|3|4|5", "occrrncStepDesc": "안심|주의|주의|경고|경고|경고",
                        "occrrncStepDescCode": "0|1|1|2|2|2"})
    assert len(row.steps) == 6
    single = row.single(1)
    assert single == {"dbyhsCode": "002006001", "dbyhsNm": "눈마름병", "occrrncStepNm": "2%미만",
                      "occrrncStepCode": "1", "occrrncStepDesc": "주의", "occrrncStepDescCode": "1"}


@pytest.mark.asyncio
async def test_fetch_refs_partial_failure():
    with respx.mock() as router:
        router.get(f"{BASE}/m/diary/2026-08-19/0804MM/detail").mock(return_value=httpx.Response(500))
        router.get(f"{BASE}/m/diary/2026-08-19/0804MM/user-farmwork/list").mock(
            return_value=env([{"userFarmworkId": 3, "userFarmworkNm": "관수", "userAdded": False, "use": True, "checked": False}]))
        router.get(f"{BASE}/m/diary/0804MM/dhyhs-stdr/list").mock(return_value=env([]))
        router.get(f"{BASE}/m/diary/0804MM/prvnbe-stdr/list").mock(return_value=env({"prvnbeTypeList": [], "prvnbeList": []}))
        router.get(f"{BASE}/m/diary/0804MM/pesti-stdr/list").mock(return_value=env([]))
        async with FarmosClient(BASE, "tok", retries=0) as c:
            refs = await c.fetch_refs("2026-08-19", "0804MM")
        assert refs.status["detail"].startswith("error") and refs.status["farmworks"] == "ok"
        assert refs.farmworks[0]["userFarmworkNm"] == "관수"
