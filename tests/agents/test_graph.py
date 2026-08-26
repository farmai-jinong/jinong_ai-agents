"""그래프 전체 실행 (Fake LLM + Fake farmos) — 정상/멀티파일/EMPTY/강등 경로."""

import pytest

from app.agents.interface import PipelineEmpty
from app.agents.tools.fake_farmos import FakeFarmosClient
from app.agents.tools.fake_llm import FakeChatModel
from app.schemas.pipeline import PipelineResult

from .conftest import FIX, fake_llm, load_call, make_pipeline

STRAW_CONTENT = {"content": "[AI 초안·통화 기반]\n- 2동 잿빛곰팡이 초기 발생, 환기 강화\n- 내일 사파이어 살포 예정 (확인 필요)", "evidence": [2, 3, 8]}
STRAW_REPORT = {"farm_status": [{"text": "딸기 하우스 2동", "evidence": [2], "needs_verification": False}],
                "issues": [{"text": "잿빛곰팡이병 초기 발생", "evidence": [2, 3], "needs_verification": False}],
                "advice": [{"text": "[병해충관리] 사파이어 2000배 살포", "evidence": [7], "needs_verification": False},
                           {"text": "근거 없는 권고", "evidence": [999], "needs_verification": False}],
                "farmer_actions": [{"text": "관수·적엽 실시", "evidence": [4], "needs_verification": False}],
                "follow_ups": [{"text": "화요일 방문", "evidence": [11], "needs_verification": False}],
                "summary_line": "요약", "keywords": ["딸기"],
                "action_items": [{"owner": "consultant", "text": "방문", "due_hint": "화요일", "evidence": [11]}]}


@pytest.mark.asyncio
async def test_strawberry_full_run(settings, farmos_fake):
    tr, ctx = load_call("strawberry_botrytis")
    llm = fake_llm("strawberry_botrytis", responses={"diary_content": STRAW_CONTENT, "report": STRAW_REPORT})
    res = await make_pipeline(settings, llm, farmos_fake).run(tr, ctx)
    assert isinstance(res, PipelineResult)
    assert farmos_fake.calls[0] == "list_crops"                      # 최우선 호출
    d = res.diaries[0]
    assert (d.prdlst_code, d.prdlst_nm, d.status) == ("0804MM", "딸기", "OK")
    pre = d.structured["prefill"]
    assert [f["userFarmworkNm"] for f in pre["userFarmworkList"]] == ["관수", "적엽"]
    assert pre["dbyhsList"][0]["dbyhsCode"] == "002006001" and pre["dbyhsList"][0]["occrrncStepCode"] == "1"
    assert pre["prvnbeNPestiList"] == []                              # 사파이어는 planned → 방제이력 아님
    assert "## 방제이력\n- 언급 없음" in d.markdown
    assert "사파이어 액상수화제 → 잿빛곰팡이 · 2000배 (확인 필요)" in d.markdown
    assert d.structured["prefill_ready"] is True
    assert res.speaker_map == {"f0:A": "farmer", "f0:B": "consultant"}
    assert res.farmos_status == "ok" and res.usage["calls"] == 5 and res.model == "fake-llm"   # +verify_diary
    # 보고서: 근거 없는 bullet 제외 + 농약 bullet 은 needs_verification 강제
    rep = res.report.structured
    assert all(b["text"] != "근거 없는 권고" for b in rep["sections"]["advice"])
    assert rep["sections"]["advice"][0]["needs_verification"] is True
    assert any("근거 없는 항목" in w for w in res.warnings)
    assert "## 컨설팅·권고 내용\n- [병해충관리] 사파이어 2000배 살포 ※ 확인 필요 (근거: #7)" in res.report.markdown


@pytest.mark.asyncio
async def test_tomato_two_files_speaker_flip_and_existing_diary(settings, farmos_fake):
    tr, ctx = load_call("tomato_two_files_speaker_flip")
    res = await make_pipeline(settings, fake_llm("tomato_two_files_speaker_flip"), farmos_fake).run(tr, ctx)
    assert res.speaker_map == {"f0:A": "consultant", "f0:B": "farmer", "f1:A": "consultant", "f1:B": "farmer"}
    d = res.diaries[0]
    assert d.prdlst_code == "0803MM" and d.status == "OK"
    pre = d.structured["prefill"]
    assert pre["diaryId"] == 77
    assert [f["userFarmworkNm"] for f in pre["userFarmworkList"]] == ["관수", "유인", "측지제거"]   # 기존 관수 유지
    assert pre["dbyhsList"][0]["dbyhsNm"] == "담배가루이" and pre["dbyhsList"][0]["occrrncStepCode"] in ("4", "5")
    assert "- [x] 관수 (기존 일지)" in d.markdown
    assert "[비료] 칼슘 비료" in d.markdown and "모벤토" in d.markdown
    # 서술 LLM 실패 → 결정적 대체 (경고 포함, 상태는 유지)
    assert any("사실 목록으로 대체" in w for w in res.warnings)
    assert "## 컨설팅·권고 내용\n- [근권관리]" in res.report.markdown or "[병해충관리]" in res.report.markdown


@pytest.mark.asyncio
async def test_grape_multi_crop_only_mentioned_crop_and_empty_diary(settings, farmos_fake):
    tr, ctx = load_call("grape_multi_crop_no_work")
    res = await make_pipeline(settings, fake_llm("grape_multi_crop_no_work"), farmos_fake).run(tr, ctx)
    assert [(d.prdlst_code, d.status) for d in res.diaries] == [("0603MM", "EMPTY")]
    assert res.diaries[0].structured["prefill"] is None
    assert "확인되지 않았습니다" in res.diaries[0].markdown
    assert "당도 18브릭스" in res.report.markdown          # 보고서는 채워짐


@pytest.mark.asyncio
async def test_no_farmos_hints_only_partial(settings):
    tr, ctx = load_call("strawberry_no_farmos")
    res = await make_pipeline(settings, fake_llm("strawberry_botrytis"), None).run(tr, ctx)
    d = res.diaries[0]
    assert d.prdlst_code == "0804MM" and d.status == "PARTIAL" and d.structured["prefill"] is None
    assert res.farmos_status == "disabled"
    assert "[ ] 관수 (표준 목록 조회 불가" in d.markdown


@pytest.mark.asyncio
async def test_farmos_down_degrades_not_fails(settings):
    tr, ctx = load_call("strawberry_botrytis")
    farmos = FakeFarmosClient(FIX / "farmos", raise_on={"list_crops"})
    res = await make_pipeline(settings, fake_llm("strawberry_botrytis"), farmos).run(tr, ctx)
    assert res.farmos_status == "unavailable"
    assert res.diaries[0].prdlst_nm == "딸기" and res.diaries[0].prdlst_code is None   # 통화 언급으로 대상 유지
    assert any("farmos 조회 실패" in w for w in res.warnings)


@pytest.mark.asyncio
async def test_farmos_partial_refs(settings):
    tr, ctx = load_call("strawberry_botrytis")
    farmos = FakeFarmosClient(FIX / "farmos", raise_on={"dbyhs"})
    res = await make_pipeline(settings, fake_llm("strawberry_botrytis"), farmos).run(tr, ctx)
    d = res.diaries[0]
    assert d.status == "PARTIAL" and d.structured["prefill_ready"] is False
    assert d.structured["prefill"]["userFarmworkList"]                       # 농작업은 매핑됨
    assert d.structured["mapping"]["pests"][0]["status"] == "no_refs"


@pytest.mark.asyncio
async def test_farmos_auth_fail(settings):
    tr, ctx = load_call("strawberry_botrytis")
    farmos = FakeFarmosClient(FIX / "farmos", auth_fail=True)
    res = await make_pipeline(settings, fake_llm("strawberry_botrytis"), farmos).run(tr, ctx)
    assert res.farmos_status == "unavailable" and any("인증 실패" in w for w in res.warnings)


@pytest.mark.asyncio
async def test_extract_failure_gives_empty_diaries_but_no_crash(settings, farmos_fake):
    tr, ctx = load_call("strawberry_botrytis")
    llm = fake_llm("strawberry_botrytis", fail_kinds={"extract"})
    res = await make_pipeline(settings, llm, farmos_fake).run(tr, ctx)
    assert all(d.status == "EMPTY" for d in res.diaries)
    assert any("사실 추출 실패" in w for w in res.warnings)
    assert res.report is not None and "언급 없음" in res.report.markdown


@pytest.mark.asyncio
async def test_speaker_role_llm_failure_falls_back(settings, farmos_fake):
    tr, ctx = load_call("strawberry_botrytis")
    llm = fake_llm("strawberry_botrytis", fail_kinds={"speaker_roles"})
    res = await make_pipeline(settings, llm, farmos_fake).run(tr, ctx)
    assert res.diaries[0].status == "OK"
    assert set(res.speaker_map.values()) <= {"farmer", "consultant", "unknown"}
    assert any("화자 역할" in w for w in res.warnings)


@pytest.mark.asyncio
async def test_pipeline_empty_when_nothing(settings, farmos_fake):
    tr, ctx = load_call("grape_multi_crop_no_work")
    llm = FakeChatModel(responses={})       # 빈 CallFacts, 빈 speaker
    llm.fail_kinds = {"report", "diary_content"}
    with pytest.raises(PipelineEmpty):
        await make_pipeline(settings, llm, farmos_fake).run(tr, ctx)


@pytest.mark.asyncio
async def test_disambiguate_path(settings, farmos_fake):
    tr, ctx = load_call("tomato_two_files_speaker_flip")
    facts = dict(fake_llm("tomato_two_files_speaker_flip").responses["extract"])
    facts["pests"] = [{"name": "가루이", "kind": "해충", "status": "발생", "severity": "보통", "severity_raw": None,
                       "location": None, "crop": "토마토", "evidence": [1]}]     # 담배가루이 / 온실가루이 → ambiguous
    llm = fake_llm("tomato_two_files_speaker_flip", responses={"extract": facts,
                   "disambiguate": lambda msgs: {"picks": [{"item_id": "pest0", "choice": "002005012"}]}})
    res = await make_pipeline(settings, llm, farmos_fake).run(tr, ctx)
    m = res.diaries[0].structured["mapping"]["pests"][0]
    assert m["status"] == "matched" and m["code"] == "002005012" and m["method"] == "llm-pick"
    assert res.diaries[0].structured["prefill"]["dbyhsList"][0]["dbyhsNm"] == "온실가루이"
    assert res.diaries[0].structured["prefill"]["dbyhsList"][0]["occrrncStepCode"] == "2"
    assert any(c["kind"] == "disambiguate" for c in llm.calls)


@pytest.mark.asyncio
async def test_disambiguate_rejects_out_of_candidates(settings, farmos_fake):
    tr, ctx = load_call("tomato_two_files_speaker_flip")
    facts = dict(fake_llm("tomato_two_files_speaker_flip").responses["extract"])
    facts["pests"] = [{"name": "가루이", "kind": "해충", "status": "발생", "severity": "보통", "severity_raw": None,
                       "location": None, "crop": "토마토", "evidence": [1]}]
    llm = fake_llm("tomato_two_files_speaker_flip", responses={"extract": facts,
                   "disambiguate": {"picks": [{"item_id": "pest0", "choice": "999999"}]}})
    res = await make_pipeline(settings, llm, farmos_fake).run(tr, ctx)
    m = res.diaries[0].structured["mapping"]["pests"][0]
    assert m["status"] == "unmatched" and "[표준 목록 미매핑]" in res.diaries[0].markdown


def test_speaker_roles_validate_accepts_one_based_file_index():
    """LLM 이 '[파일 1]' 을 file_index=1 로 돌려준 경우(Gemini 에서 관찰) 0-based 로 보정한다."""
    from app.agents.nodes.speaker_roles import validate
    from app.agents.schemas import FileSpeakerMap, LetterRole, SpeakerRoleResult

    sr = SpeakerRoleResult(files=[FileSpeakerMap(file_index=1, confidence=0.9, rationale="r",
                                                 roles=[LetterRole(letter="A", role="farmer"), LetterRole(letter="B", role="consultant")])])
    out = validate(sr, {0: ["A", "B"]})
    assert out.files[0].file_index == 0 and out.files[0].confidence == 0.9 and out.files[0].mapping == {"A": "farmer", "B": "consultant"}
    # 두 파일 모두 1-based 로 온 경우
    sr2 = SpeakerRoleResult(files=[FileSpeakerMap(file_index=1, confidence=0.8, rationale="r", roles=[LetterRole(letter="A", role="farmer")]),
                                   FileSpeakerMap(file_index=2, confidence=0.7, rationale="r", roles=[LetterRole(letter="A", role="consultant")])])
    out2 = validate(sr2, {0: ["A"], 1: ["A"]})
    assert [f.file_index for f in out2.files] == [0, 1] and out2.files[1].mapping == {"A": "consultant"}
    # 정상(0-based) 응답은 그대로
    sr3 = SpeakerRoleResult(files=[FileSpeakerMap(file_index=0, confidence=0.9, rationale="r", roles=[LetterRole(letter="A", role="farmer")])])
    assert validate(sr3, {0: ["A"], 1: ["A"]}).files[0].file_index == 0
