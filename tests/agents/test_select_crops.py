import pytest

from app.agents.deps import Deps
from app.agents.nodes.select_crops import choose_targets, route_facts, select_crops
from app.agents.schemas import CallFacts, CropMention, CropRef, FarmContext, FarmworkFact, PestFact
from app.config import Settings
from app.schemas.pipeline import CallContext

CROPS = [CropRef(prdlstCode="0804MM", prdlstNm="딸기", reprsntPrdlstCnt=1),
         CropRef(prdlstCode="0803MM", prdlstNm="토마토", reprsntPrdlstCnt=0),
         CropRef(prdlstCode="0603MM", prdlstNm="포도", reprsntPrdlstCnt=0)]
FARM = FarmContext(crops=CROPS, source="farmos", status="ok")


def facts(**kw):
    f = CallFacts.empty()
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def test_mentioned_crop_partial_name():
    f = facts(crops_mentioned=[CropMention(name_raw="방울토마토", matched_name=None, evidence=[1])])
    targets, n2k, w = choose_targets(f, FARM, None, None)
    assert [t.prdlst_code for t in targets] == ["0803MM"] and n2k["방울토마토"] == "0803MM"


def test_default_to_representative_crop():
    targets, _, w = choose_targets(facts(), FARM, None, None)
    assert targets[0].prdlst_code == "0804MM" and "가정" in w[0]


def test_hint_overrides():
    targets, _, _ = choose_targets(facts(), FARM, "0603MM", None)
    assert targets[0].prdlst_code == "0603MM" and targets[0].reason == "hint"


def test_unresolved_when_no_farm_and_no_mention():
    targets, _, w = choose_targets(facts(), FarmContext(), None, None)
    assert targets[0].resolved is False and targets[0].prdlst_code is None


def test_unknown_farm_but_crop_mentioned():
    f = facts(crops_mentioned=[CropMention(name_raw="딸기", matched_name=None, evidence=[0])])
    targets, _, _ = choose_targets(f, FarmContext(), None, None)
    assert targets[0].prdlst_code is None and targets[0].prdlst_nm == "딸기" and targets[0].resolved


def test_route_facts_multi_target_warning():
    f = facts(crops_mentioned=[CropMention(name_raw="딸기", matched_name="딸기", evidence=[0]),
                               CropMention(name_raw="포도", matched_name="포도", evidence=[1])],
              farmworks=[FarmworkFact(name="관수", crop="딸기", when="today", date_hint=None, detail=None, evidence=[2]),
                         FarmworkFact(name="적심", crop=None, when="today", date_hint=None, detail=None, evidence=[3])],
              pests=[PestFact(name="노균병", kind="병", status="발생", severity="경미", severity_raw=None, location=None, note=None, crop="포도", evidence=[4])])
    targets, n2k, _ = choose_targets(f, FARM, None, None)
    routed, w = route_facts(f, targets, n2k)
    assert [x.name for x in routed["0804MM"].farmworks] == ["관수", "적심"]
    assert [x.name for x in routed["0603MM"].pests] == ["노균병"]
    assert w and "대표" in w[0]


STANDARD = [CropRef(prdlstCode="0803MM", prdlstNm="토마토"), CropRef(prdlstCode="0806MM", prdlstNm="방울토마토"),
            CropRef(prdlstCode="1326MM", prdlstNm="파프리카")]
REGISTERED = FarmContext(crops=[CropRef(prdlstCode="0804MM", prdlstNm="딸기"),
                                CropRef(prdlstCode="1326MM", prdlstNm="파프리카", reprsntPrdlstCnt=1)],
                         source="ap_backend", status="partial")


def test_mentioned_unregistered_crop_gets_own_diary_with_standard_code():
    """등록 작물(딸기·파프리카)에 없는 토마토를 말하면 대표작물로 가정하지 않고 토마토 일지 — 코드는 표준 품목에서."""
    f = facts(crops_mentioned=[CropMention(name_raw="토마토", matched_name="토마토", evidence=[1])])
    targets, n2k, w = choose_targets(f, REGISTERED, None, None, STANDARD)
    assert [(t.prdlst_code, t.prdlst_nm, t.registered) for t in targets] == [("0803MM", "토마토", False)]
    assert n2k["토마토"] == "0803MM" and any("미등록 작물" in x for x in w) and not any("가정" in x for x in w)


def test_mentioned_unregistered_crop_without_standard_list_keeps_name_only():
    f = facts(crops_mentioned=[CropMention(name_raw="토마토", matched_name=None, evidence=[1])])
    targets, _, w = choose_targets(f, REGISTERED, None, None, None)
    assert [(t.prdlst_code, t.prdlst_nm, t.registered) for t in targets] == [(None, "토마토", False)]
    assert any("표준 품목코드도 못 찾음" in x for x in w)


def test_registered_and_unregistered_mentions_both_become_targets():
    f = facts(crops_mentioned=[CropMention(name_raw="딸기", matched_name="딸기", evidence=[0]),
                               CropMention(name_raw="토마토", matched_name="토마토", evidence=[1])],
              farmworks=[FarmworkFact(name="유인", crop="토마토", when="today", date_hint=None, detail=None, evidence=[2])])
    targets, n2k, _ = choose_targets(f, REGISTERED, None, None, STANDARD)
    assert [(t.prdlst_code, t.registered) for t in targets] == [("0804MM", True), ("0803MM", False)]
    routed, _ = route_facts(f, targets, n2k)
    assert [x.name for x in routed["0803MM"].farmworks] == ["유인"] and not routed["0804MM"].farmworks


def test_standard_match_is_exact_only():
    """표준 품목 2천여 건에서는 유사도 매칭이 오탐이라 정확 일치(정규화·부분문자열)만 — 엉뚱한 이름은 코드 없이."""
    f = facts(crops_mentioned=[CropMention(name_raw="토마 토", matched_name=None, evidence=[1])])
    targets, _, _ = choose_targets(f, REGISTERED, None, None, STANDARD)
    assert targets[0].prdlst_code == "0803MM"                       # 공백 정규화 후 정확 일치
    f = facts(crops_mentioned=[CropMention(name_raw="방울토마토", matched_name=None, evidence=[1])])
    targets, _, _ = choose_targets(f, REGISTERED, None, None, STANDARD)
    assert targets[0].prdlst_code == "0806MM"                       # 더 긴 정확 일치가 '토마토'보다 우선
    f = facts(crops_mentioned=[CropMention(name_raw="장인콘", matched_name=None, evidence=[1])])
    targets, _, _ = choose_targets(f, REGISTERED, None, None, STANDARD)
    assert targets[0].prdlst_code is None and targets[0].prdlst_nm == "장인콘"


class _Ap:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    async def farm_context(self, engn_id, user_id):
        return []

    async def prdlsts(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return [{"prdlstCode": "0803MM", "prdlstNm": "토마토"}]


def _cfg(ap):
    return {"configurable": {"deps": Deps(settings=Settings(_env_file=None, agent_api_key="k"), llm=None, farmos_factory=None, ap_backend=ap)}}


@pytest.mark.asyncio
async def test_select_crops_node_fetches_standard_list_only_when_needed():
    ctx = CallContext(call_id="c1", participants=[])
    ap = _Ap()
    out = await select_crops({"facts": facts(crops_mentioned=[CropMention(name_raw="딸기", matched_name="딸기", evidence=[0])]),
                              "farm": REGISTERED, "ctx": ctx}, _cfg(ap))
    assert ap.calls == 0 and out["crop_targets"][0].prdlst_code == "0804MM"
    out = await select_crops({"facts": facts(crops_mentioned=[CropMention(name_raw="토마토", matched_name="토마토", evidence=[0])]),
                              "farm": REGISTERED, "ctx": ctx}, _cfg(ap))
    assert ap.calls == 1 and [(t.prdlst_code, t.registered) for t in out["crop_targets"]] == [("0803MM", False)]


@pytest.mark.asyncio
async def test_select_crops_node_survives_standard_list_failure():
    ctx = CallContext(call_id="c1", participants=[])
    out = await select_crops({"facts": facts(crops_mentioned=[CropMention(name_raw="토마토", matched_name="토마토", evidence=[0])]),
                              "farm": REGISTERED, "ctx": ctx}, _cfg(_Ap(fail=True)))
    assert [(t.prdlst_code, t.prdlst_nm, t.registered) for t in out["crop_targets"]] == [(None, "토마토", False)]
