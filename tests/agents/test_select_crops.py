from app.agents.nodes.select_crops import choose_targets, route_facts
from app.agents.schemas import CallFacts, CropMention, CropRef, FarmContext, FarmworkFact, PestFact

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
