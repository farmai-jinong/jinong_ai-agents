"""농가 JWT 없이 AP 백엔드 research API 로 작물을 확정하는 경로 (백엔드 문서 §3)."""

import pytest

from app.agents.deps import Deps
from app.agents.nodes.farm_context import farmer_key, load_farm_context
from app.clients.ap_backend import ApBackendAuthError
from app.config import Settings
from app.schemas.pipeline import CallContext, CallHints, Participant

CROPS = [{"prdlstCode": "0804MM", "prdlstNm": "딸기", "reprsntPrdlstCnt": 1}]


class FakeApBackend:
    def __init__(self, rows=CROPS, exc=None):
        self.rows, self.exc, self.calls = rows, exc, []

    async def farm_context(self, engn_id, user_id):
        self.calls.append((engn_id, user_id))
        if self.exc:
            raise self.exc
        return self.rows


def _ctx(*, participants=None, hints=None, token=None) -> CallContext:
    return CallContext(call_id="c1", participants=participants or [], farm_access_token=token,
                       hints=hints or CallHints())


def _cfg(ap=None, farmos_factory=None) -> dict:
    deps = Deps(settings=Settings(_env_file=None, agent_api_key="k"), llm=None,
                farmos_factory=farmos_factory, ap_backend=ap)
    return {"configurable": {"deps": deps}}


FARMER = Participant(role="farmer", user_id="test7", engn_id="1", name="농가")


@pytest.mark.asyncio
async def test_ap_backend_resolves_crop_code_without_token():
    ap = FakeApBackend()
    out = await load_farm_context({"ctx": _ctx(participants=[FARMER])}, _cfg(ap))
    farm = out["farm"]
    assert ap.calls == [("1", "test7")]
    assert farm.source == "ap_backend" and farm.status == "partial"
    assert [(c.prdlstCode, c.prdlstNm) for c in farm.crops] == [("0804MM", "딸기")]
    assert any("prefill 불가" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_hints_farmer_key_is_used_when_participants_lack_engn_id():
    """participants 에 engn_id 가 없어도 metadata.hints.farmer_* 로 조회한다(백엔드 문서 §1)."""
    ap = FakeApBackend()
    ctx = _ctx(participants=[Participant(role="farmer", user_id="test7")],
               hints=CallHints(farmer_engn_id="1", farmer_user_id="test7"))
    out = await load_farm_context({"ctx": ctx}, _cfg(ap))
    assert ap.calls == [("1", "test7")]
    assert out["farm"].source == "ap_backend"


@pytest.mark.asyncio
async def test_no_farmer_key_falls_back_to_hints():
    ap = FakeApBackend()
    ctx = _ctx(participants=[Participant(role="farmer", user_id="test7")],
               hints=CallHints(prdlst_code="0804MM", prdlst_nm="딸기"))
    out = await load_farm_context({"ctx": ctx}, _cfg(ap))
    assert ap.calls == []                                  # 복합 키가 없으면 조회하지 않는다
    assert out["farm"].source == "hints"
    assert [c.prdlstNm for c in out["farm"].crops] == ["딸기"]


@pytest.mark.asyncio
async def test_ap_backend_disabled_keeps_old_behaviour():
    out = await load_farm_context({"ctx": _ctx(participants=[FARMER])}, _cfg(None))
    assert out["farm"].source == "none" and out["farm"].status == "disabled"
    assert any("토큰 없음" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_ap_backend_failure_degrades_to_hints():
    ap = FakeApBackend(exc=ApBackendAuthError("bad key", 401))
    ctx = _ctx(participants=[FARMER], hints=CallHints(prdlst_nm="딸기"))
    out = await load_farm_context({"ctx": ctx}, _cfg(ap))
    assert out["farm"].source == "hints" and out["farm"].status == "unavailable"
    assert any("AP 백엔드 인증 실패" in w for w in out["warnings"])
    assert out["errors"]


@pytest.mark.asyncio
async def test_empty_crop_list_degrades_to_hints():
    ap = FakeApBackend(rows=[])
    ctx = _ctx(participants=[FARMER], hints=CallHints(prdlst_nm="딸기"))
    out = await load_farm_context({"ctx": ctx}, _cfg(ap))
    assert out["farm"].source == "hints"
    assert any("작물 목록이 비어 있음" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_token_still_wins_over_ap_backend():
    """농가 JWT 가 있으면 farmos 를 쓴다 — prefill 이 가능한 쪽이라 우선순위가 높다."""
    class FakeFarmos:
        async def list_crops(self):
            return [{"prdlstCode": "0805TT", "prdlstNm": "토마토"}]
    ap = FakeApBackend()
    out = await load_farm_context({"ctx": _ctx(participants=[FARMER], token="jwt")},
                                  _cfg(ap, farmos_factory=lambda _t: FakeFarmos()))
    assert ap.calls == []
    assert out["farm"].source == "farmos"
    assert [c.prdlstNm for c in out["farm"].crops] == ["토마토"]


def test_farmer_key_prefers_participants():
    ctx = _ctx(participants=[FARMER], hints=CallHints(farmer_engn_id="9", farmer_user_id="other"))
    assert farmer_key(ctx) == ("1", "test7")
