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


# --- 작물 고정 모드 (daily `crop` → hints.crop_fixed) --------------------------

from app.agents.nodes.select_crops import choose_fixed_target, route_facts_fixed  # noqa: E402
from app.agents.schemas import ObservationFact, ProductFact  # noqa: E402
from app.schemas.pipeline import CallHints  # noqa: E402


def _fw(name, crop):
    return FarmworkFact(name=name, crop=crop, when="today", date_hint=None, detail=None, evidence=[1])


def _pest(name, crop):
    return PestFact(name=name, kind="병", status="발생", severity="경미", severity_raw=None, location=None, note=None,
                    crop=crop, evidence=[2])


def test_fixed_by_code_in_farm():
    t, others, w = choose_fixed_target(facts(), FARM, "0803MM", None)
    assert (t.prdlst_code, t.prdlst_nm, t.reason, t.registered, t.resolved) == ("0803MM", "토마토", "fixed", True, True)
    assert [o.prdlstNm for o in others] == ["딸기", "포도"] and w == []


def test_fixed_by_name_only_fills_code_from_farm():
    t, _, w = choose_fixed_target(facts(), FARM, None, "토마토")
    assert (t.prdlst_code, t.prdlst_nm, t.registered) == ("0803MM", "토마토", True) and w == []


def test_fixed_code_wins_over_name_when_both_given():
    t, _, _ = choose_fixed_target(facts(), FARM, "0603MM", "딸기")     # 코드가 등록 목록에 있으면 이름은 무시
    assert (t.prdlst_code, t.prdlst_nm) == ("0603MM", "포도")


def test_fixed_unregistered_uses_standard_code_and_warns():
    t, others, w = choose_fixed_target(facts(), REGISTERED, None, "토마토", STANDARD)
    assert (t.prdlst_code, t.prdlst_nm, t.registered, t.resolved) == ("0803MM", "토마토", False, True)
    assert [o.prdlstNm for o in others] == ["딸기", "파프리카"]
    assert w == ["토마토: 농가 등록 작물에 없음 — 고정 작물로 일지 생성(미등록 작물)"]


def test_fixed_unresolved_keeps_name_and_is_not_unresolved_crop():
    t, _, w = choose_fixed_target(facts(), REGISTERED, None, "장인콘", None)
    assert (t.prdlst_code, t.prdlst_nm, t.registered, t.resolved) == (None, "장인콘", False, True)
    assert any("표준 품목코드도 못 찾음" in x for x in w)


def test_fixed_no_farm_crops_is_registered_without_warning():
    t, others, w = choose_fixed_target(facts(), FarmContext(), "0803MM", "토마토")
    assert (t.prdlst_code, t.prdlst_nm, t.registered) == ("0803MM", "토마토", True) and others == [] and w == []


def test_fixed_others_include_mentions_not_matching_fixed_or_farm():
    f = facts(crops_mentioned=[CropMention(name_raw="방울토마토", matched_name=None, evidence=[0]),   # 토마토 substring → 등록 항목
                               CropMention(name_raw="고추", matched_name=None, evidence=[1]),
                               CropMention(name_raw="딸기", matched_name="딸기", evidence=[2])])
    _, others, _ = choose_fixed_target(f, FARM, "0804MM", None)
    assert [o.prdlstNm for o in others] == ["토마토", "포도", "고추"]


def test_route_facts_fixed_excludes_other_crops_and_counts():
    f = facts(crops_mentioned=[CropMention(name_raw="고추", matched_name=None, evidence=[0])],
              farmworks=[_fw("관수", "딸기"), _fw("적심", None), _fw("적과", "포도"), _fw("봉지씌우기", "포도")],
              pests=[_pest("탄저병", "고추")],
              observations=[ObservationFact(topic="생육", text="화방 양호", crop="딸기", evidence=[3])],
              products=[ProductFact(name="칼슘제", category="비료", target=None, dose=None, when="applied", date_hint=None,
                                    note=None, crop="외계작물", evidence=[4])])
    t, others, _ = choose_fixed_target(f, FARM, "0804MM", None)
    routed, w = route_facts_fixed(f, t, others)
    cf = routed["0804MM"]
    assert [x.name for x in cf.farmworks] == ["관수", "적심"]          # 딸기 + 미상 포함, 포도 제외
    assert cf.pests == [] and [x.text for x in cf.observations] == ["화방 양호"]
    assert [x.name for x in cf.products] == ["칼슘제"]                # 어디에도 안 맞는 이름(잡음)은 포함
    assert w == ["고추 관련 항목 1건 제외(작물 고정: 딸기)", "포도 관련 항목 2건 제외(작물 고정: 딸기)"]
    assert not any("대표" in x for x in w)


def test_route_facts_fixed_keeps_follow_ups_and_actions():
    from app.agents.schemas import ActionFact, FollowUpFact
    f = facts(follow_ups=[FollowUpFact(text="다음 주 재방문", when_hint=None, evidence=[1])],
              actions=[ActionFact(text="환기 강화", actor="farmer", status="agreed", due_hint=None, evidence=[2])])
    t, others, _ = choose_fixed_target(f, FARM, "0804MM", None)
    routed, w = route_facts_fixed(f, t, others)
    assert len(routed["0804MM"].follow_ups) == 1 and len(routed["0804MM"].actions) == 1 and w == []


def test_route_facts_fixed_exact_beats_substring():
    farm = FarmContext(crops=[CropRef(prdlstCode="0806MM", prdlstNm="방울토마토"), CropRef(prdlstCode="0803MM", prdlstNm="토마토")],
                       source="farmos", status="ok")
    f = facts(farmworks=[_fw("유인", "토마토")])
    t, others, _ = choose_fixed_target(f, farm, "0806MM", None)
    routed, w = route_facts_fixed(f, t, others)
    assert routed["0806MM"].farmworks == [] and w == ["토마토 관련 항목 1건 제외(작물 고정: 방울토마토)"]
    # 등록 목록에 '토마토' 가 없으면 부분 문자열로 고정 작물에 포함
    farm2 = FarmContext(crops=[CropRef(prdlstCode="0806MM", prdlstNm="방울토마토"), CropRef(prdlstCode="0901MM", prdlstNm="고추")],
                        source="farmos", status="ok")
    t, others, _ = choose_fixed_target(f, farm2, "0806MM", None)
    routed, w = route_facts_fixed(f, t, others)
    assert [x.name for x in routed["0806MM"].farmworks] == ["유인"] and w == []


@pytest.mark.asyncio
async def test_select_crops_fixed_mode_single_target_and_standard_fetch_policy():
    f = facts(crops_mentioned=[CropMention(name_raw="딸기", matched_name="딸기", evidence=[0]),
                               CropMention(name_raw="파프리카", matched_name="파프리카", evidence=[1])],
              farmworks=[_fw("관수", "딸기"), _fw("적심", "파프리카")])
    # 코드 지정 → 표준 목록 조회 없음, 대상 1개, 파프리카 항목 제외
    ap = _Ap()
    ctx = CallContext(call_id="d1", hints=CallHints(prdlst_code="0804MM", prdlst_nm="딸기", crop_fixed=True))
    out = await select_crops({"facts": f, "farm": REGISTERED, "ctx": ctx}, _cfg(ap))
    assert ap.calls == 0 and [(t.prdlst_code, t.reason) for t in out["crop_targets"]] == [("0804MM", "fixed")]
    assert [x.name for x in out["crop_facts"]["0804MM"].farmworks] == ["관수"]
    assert out["warnings"] == ["파프리카 관련 항목 1건 제외(작물 고정: 딸기)"]
    # 이름만 + 등록 목록에 없음 → 표준 목록 1회 조회로 코드 채움
    ctx = CallContext(call_id="d2", hints=CallHints(prdlst_nm="토마토", crop_fixed=True))
    out = await select_crops({"facts": f, "farm": REGISTERED, "ctx": ctx}, _cfg(ap))
    assert ap.calls == 1 and [(t.prdlst_code, t.prdlst_nm, t.registered) for t in out["crop_targets"]] == [("0803MM", "토마토", False)]
    # crop_fixed 가 아니면 기존 경로(힌트는 추가일 뿐 — 언급 작물도 대상)
    ctx = CallContext(call_id="d3", hints=CallHints(prdlst_code="0804MM"))
    out = await select_crops({"facts": f, "farm": REGISTERED, "ctx": ctx}, _cfg(ap))
    assert len(out["crop_targets"]) == 2


def test_fixed_hint_only_farm_gets_code_from_standard():
    """토큰 없이 hints 로만 만든 농가 목록(코드 없음) — 고정 작물 코드는 표준 품목에서 보완."""
    farm = FarmContext(crops=[CropRef(prdlstNm="토마토", reprsntPrdlstCnt=1)], source="hints", status="unavailable")
    t, _, w = choose_fixed_target(facts(), farm, None, "토마토", STANDARD)
    assert (t.prdlst_code, t.prdlst_nm, t.registered) == ("0803MM", "토마토", True) and w == []
    t, _, _ = choose_fixed_target(facts(), farm, None, "토마토", None)
    assert (t.prdlst_code, t.prdlst_nm) == (None, "토마토")


def _turns(*texts):
    from app.agents.schemas import NormalizedTranscript, Turn
    return NormalizedTranscript(turns=[Turn(tid=i, file_index=0, speaker_letter="A", speaker_key="f0:A", start_sec=i, end_sec=i + 1,
                                            abs_start=i, abs_end=i + 1, text=t) for i, t in enumerate(texts)],
                                n_files=1, est_tokens=10, duration_sec=float(len(texts)))


def test_route_facts_fixed_uses_evidence_when_crop_is_null():
    """extract 는 목록 밖 작물의 사실을 crop=None 으로 내므로, 근거 발화에 다른 작물명만 있으면 제외한다(스모크 녹음 재현)."""
    tr = _turns("안녕하세요 아 파프리카 진딧물이 너무", "많이 생겨가지고 고민이 많습니다",
                "그런데 또 딸기는 잿빛곰팡이가 너무 많이 퍼졌고 흰가루병도 오는 것 같아요", "환기는 잘 하고 계세요?")
    f = facts(crops_mentioned=[CropMention(name_raw="파프리카", matched_name=None, evidence=[0]),
                               CropMention(name_raw="딸기", matched_name="딸기", evidence=[2])],
              pests=[PestFact(name="진딧물", kind="해충", status="발생", severity="심함", severity_raw=None, location=None, note=None, crop=None, evidence=[0, 1]),
                     PestFact(name="잿빛곰팡이", kind="병", status="발생", severity="심함", severity_raw=None, location=None, note=None, crop=None, evidence=[2])],
              observations=[ObservationFact(topic="환경", text="환기 양호", crop=None, evidence=[3])])
    farm = FarmContext(crops=[CropRef(prdlstNm="딸기", reprsntPrdlstCnt=1)], source="hints", status="unavailable")
    t, others, _ = choose_fixed_target(f, farm, None, "딸기", None)
    assert [o.prdlstNm for o in others] == ["파프리카"]
    routed, w = route_facts_fixed(f, t, others, tr, aliases=["딸기"])
    assert [p.name for p in routed["딸기"].pests] == ["잿빛곰팡이"]      # 파프리카 진딧물 제외
    assert [o.text for o in routed["딸기"].observations] == ["환기 양호"]   # 작물 언급 없는 근거 → 포함
    assert w == ["파프리카 관련 항목 1건 제외(작물 고정: 딸기)"]
    # 근거에 고정 작물도 같이 나오면 판단 보류(포함); transcript 없으면 crop=None 은 전부 포함
    f2 = facts(pests=[PestFact(name="응애", kind="해충", status="발생", severity="경미", severity_raw=None, location=None, note=None, crop=None,
                               evidence=[0, 2])])
    routed, w = route_facts_fixed(f2, t, others, tr)
    assert [p.name for p in routed["딸기"].pests] == ["응애"] and w == []
    routed, w = route_facts_fixed(f, t, others, None)
    assert len(routed["딸기"].pests) == 2 and w == []


def test_fixed_code_only_resolves_name_from_standard():
    """코드만 받으면 daily_job 이 이름 자리에 코드를 넣어 보낸다 — 이름은 표준 품목에서 보완해야 진짜 작물 사실이 '타작물' 로 안 빠진다."""
    farm = FarmContext(crops=[CropRef(prdlstCode="0804MM", prdlstNm="0804MM", reprsntPrdlstCnt=1)], source="hints", status="unavailable")
    std = [CropRef(prdlstCode="0804MM", prdlstNm="딸기"), *STANDARD]
    t, others, w = choose_fixed_target(facts(), farm, "0804MM", "0804MM", std)
    assert (t.prdlst_code, t.prdlst_nm, t.registered) == ("0804MM", "딸기", True) and w == [] and others == []
    t, _, _ = choose_fixed_target(facts(), farm, "0804MM", "0804MM", None)   # 표준 목록 못 받으면 코드가 이름
    assert (t.prdlst_code, t.prdlst_nm) == ("0804MM", "0804MM")


@pytest.mark.asyncio
async def test_select_crops_fixed_fetches_standard_unless_farm_resolves_both():
    f = facts()
    # 등록 목록에 코드·이름 모두 있음 → 조회 없음
    ap = _Ap()
    ctx = CallContext(call_id="a", hints=CallHints(prdlst_code="0804MM", prdlst_nm="딸기", crop_fixed=True))
    await select_crops({"facts": f, "farm": REGISTERED, "ctx": ctx}, _cfg(ap))
    assert ap.calls == 0
    # hints 로 만든 목록(이름 자리에 코드) → 조회
    farm = FarmContext(crops=[CropRef(prdlstCode="0804MM", prdlstNm="0804MM")], source="hints", status="unavailable")
    ap = _Ap()
    ctx = CallContext(call_id="b", hints=CallHints(prdlst_code="0804MM", prdlst_nm="0804MM", crop_fixed=True))
    await select_crops({"facts": f, "farm": farm, "ctx": ctx}, _cfg(ap))
    assert ap.calls == 1
    # 이름만, 등록 목록 코드 있음 → 조회 없음
    ap = _Ap()
    ctx = CallContext(call_id="c", hints=CallHints(prdlst_nm="파프리카", crop_fixed=True))
    out = await select_crops({"facts": f, "farm": REGISTERED, "ctx": ctx}, _cfg(ap))
    assert ap.calls == 0 and out["crop_targets"][0].prdlst_code == "1326MM"
