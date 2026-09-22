"""재생성(ctx.prefer_backend_db) 시 백엔드 DB 를 작물 출처 1순위로 — 첫 생성은 무변경, 빈 결과/예외는 기존 순서로 폴백."""

import pytest

from app.agents.deps import Deps
from app.agents.nodes.crop_diary.fetch_refs import fetch_refs
from app.agents.nodes.farm_context import load_farm_context
from app.agents.nodes.select_crops import _standard_prdlsts
from app.agents.schemas import CropTarget, FarmContext
from app.config import Settings
from app.schemas.pipeline import CallContext, CallHints, Participant
from tests.agents.test_farm_context import CROPS, FakeApBackend

DB_CROPS = [{"prdlstCode": "1326MM", "prdlstNm": "파프리카", "reprsntPrdlstCnt": 1, "use": True}]
FARMER = Participant(role="farmer", user_id="test7", engn_id="1")


class FakeBackendDb:
    def __init__(self, crops=DB_CROPS, exc=None, prdlst_exc=None):
        self.crops, self.exc, self.prdlst_exc, self.calls = crops, exc, prdlst_exc, []

    async def farm_crops(self, engn_id, user_id):
        self.calls.append((engn_id, user_id))
        if self.exc:
            raise self.exc
        return self.crops

    async def prdlsts(self):
        if self.prdlst_exc:
            raise self.prdlst_exc
        return [{"prdlstCode": "0901MM", "prdlstNm": "오이"}]


class FakeFarmos:
    def __init__(self):
        self.list_calls, self.ref_calls = 0, []

    async def list_crops(self):
        self.list_calls += 1
        return CROPS

    async def fetch_refs(self, date, code):
        self.ref_calls.append((date, code))

        class R:
            status = {"diary": "ok"}
        return R()

    async def pesti_list(self, code, prvnbe_code=None):
        return []


def _cfg(db=None, ap=None, farmos=None):
    deps = Deps(settings=Settings(_env_file=None, agent_api_key="k"), llm=None,
                farmos_factory=(lambda tok: farmos) if farmos else None, ap_backend=ap, backend_db=db)
    return {"configurable": {"deps": deps}}, deps


def _ctx(*, prefer=False, token=None, participants=(FARMER,), hints=None):
    return CallContext(call_id="c1", participants=list(participants), farm_access_token=token,
                       hints=hints or CallHints(), prefer_backend_db=prefer)


@pytest.mark.asyncio
async def test_regen_prefers_backend_db_over_token_and_api():
    db, ap, fo = FakeBackendDb(), FakeApBackend(), FakeFarmos()
    cfg, _ = _cfg(db, ap, fo)
    out = await load_farm_context({"ctx": _ctx(prefer=True, token="tok")}, cfg)
    farm = out["farm"]
    assert farm.source == "backend_db" and farm.status == "ok"
    assert [c.prdlstCode for c in farm.crops] == ["1326MM"] and farm.crops[0].reprsntPrdlstCnt == 1
    assert db.calls == [("1", "test7")] and fo.list_calls == 0 and ap.calls == []
    # 토큰 없으면 partial + prefill 불가 경고
    out = await load_farm_context({"ctx": _ctx(prefer=True)}, cfg)
    assert out["farm"].source == "backend_db" and out["farm"].status == "partial"
    assert any("prefill 불가" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_first_generation_ignores_backend_db():
    db, ap, fo = FakeBackendDb(), FakeApBackend(), FakeFarmos()
    cfg, _ = _cfg(db, ap, fo)
    out = await load_farm_context({"ctx": _ctx(prefer=False, token="tok")}, cfg)
    assert out["farm"].source == "farmos" and fo.list_calls == 1 and db.calls == []
    out = await load_farm_context({"ctx": _ctx(prefer=False)}, cfg)
    assert out["farm"].source == "ap_backend" and db.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("db", [FakeBackendDb(crops=[]), FakeBackendDb(exc=RuntimeError("down"))])
async def test_regen_falls_back_to_default_chain(db):
    ap, fo = FakeApBackend(), FakeFarmos()
    cfg, _ = _cfg(db, ap, fo)
    out = await load_farm_context({"ctx": _ctx(prefer=True, token="tok")}, cfg)
    assert out["farm"].source == "farmos" and fo.list_calls == 1
    assert any("기존 순서로 조회" in w for w in out["warnings"]), out["warnings"]
    out = await load_farm_context({"ctx": _ctx(prefer=True)}, cfg)
    assert out["farm"].source == "ap_backend" and ap.calls[-1] == ("1", "test7")


@pytest.mark.asyncio
async def test_regen_without_farmer_key_uses_default_chain():
    db = FakeBackendDb()
    cfg, _ = _cfg(db, None, FakeFarmos())
    out = await load_farm_context({"ctx": _ctx(prefer=True, token="tok", participants=())}, cfg)
    assert out["farm"].source == "farmos" and db.calls == []
    assert any("복합 키 없음" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_fetch_refs_runs_for_backend_db_source_when_token_present():
    fo = FakeFarmos()
    cfg, _ = _cfg(FakeBackendDb(), None, fo)
    target = CropTarget(key="k", prdlst_code="1326MM", prdlst_nm="파프리카", registered=True, resolved=True, reason="test")
    farm = FarmContext(crops=[], source="backend_db", status="ok")
    state = {"ctx": _ctx(prefer=True, token="tok"), "target": target, "farm": farm, "diary_date": "2026-09-01"}
    out = await fetch_refs(state, cfg)
    assert out["refs_status"] == "ok" and fo.ref_calls == [("2026-09-01", "1326MM")]
    state["farm"] = FarmContext(crops=[], source="backend_db", status="partial")
    state["ctx"] = _ctx(prefer=True)          # 토큰 없음 → disabled
    assert (await fetch_refs(state, cfg))["refs_status"] == "disabled"


@pytest.mark.asyncio
async def test_standard_prdlsts_prefers_backend_db_then_ap():
    ap = FakeApBackend()

    async def prdlsts():
        return [{"prdlstCode": "0803MM", "prdlstNm": "토마토"}]
    ap.prdlsts = prdlsts
    _, deps = _cfg(FakeBackendDb(), ap, None)
    assert [c.prdlstCode for c in await _standard_prdlsts(deps)] == ["0901MM"]
    _, deps = _cfg(FakeBackendDb(prdlst_exc=RuntimeError("down")), ap, None)
    assert [c.prdlstCode for c in await _standard_prdlsts(deps)] == ["0803MM"]
    _, deps = _cfg(None, None, None)
    assert await _standard_prdlsts(deps) == []
