import json
from datetime import UTC, datetime

from app.agents.nodes.crop_diary.render_diary import build_prefill
from app.agents.nodes.prepare_transcript import build_turns
from app.agents.render.markdown import render_diary, render_report
from app.agents.schemas import ActionItem, Bullet, CropFacts, DiaryResult, MappedItem, MappingReport, ReportNarrative
from app.clients.farmos import FarmosRefs, expand_dbyhs

from .conftest import FIX, load_call

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
DIARY_HEADINGS = ["## 주요 농작업", "## 기타 기록사항", "## 병해충", "## 방제이력", "## 농작업 사진",
                  "## 투입 제품 (농약·비료·종자·농자재)", "## 향후 작업·확인 계획", "## 근거 발화"]
REPORT_HEADINGS = ["## 대화 요약", "## 상담 개요", "## 농가·농장·작물 현황", "## 주요 문의·문제사항",
                   "## 컨설팅·권고 내용", "## 농가 조치사항", "## 후속 확인·상담 계획", "## 근거 발화"]


def _ordered(md: str, headings: list[str]) -> bool:
    pos = [md.find(h) for h in headings]
    return all(p >= 0 for p in pos) and pos == sorted(pos)


def test_diary_headings_order_and_footer():
    tr, ctx = load_call("strawberry_botrytis")
    nt = build_turns(tr)
    rep = MappingReport(farmworks=[MappedItem(item_id="fw0", family="farmwork", source="관수", status="matched", code="1", name="관수", evidence=[4]),
                                   MappedItem(item_id="fw1", family="farmwork", source="런너정리", status="unmatched", evidence=[4],
                                              candidates=[{"code": "8", "name": "런너제거", "score": 75}])],
                        pests=[MappedItem(item_id="p0", family="pest", source="잿빛곰팡이", status="matched", code="002006001", name="잿빛곰팡이병",
                                          evidence=[2], payload={"occrrncStepNm": "2%미만", "occrrncStepDesc": "주의"})])
    d = DiaryResult(prdlst_code="0804MM", prdlst_nm="딸기", diary_date="2026-08-19", status="OK", gs_nm="수확기",
                    mapping=rep, content="[AI 초안·통화 기반]\n테스트", evidence=[2, 4])
    md = render_diary(d, ctx, nt, CropFacts(), model="m", prompt_version="1", now=NOW)
    assert md.startswith("# 영농일지 — 딸기 (2026-08-19)")
    assert _ordered(md, DIARY_HEADINGS)
    assert "- [x] 관수 (근거: #4)" in md and "신규 후보" in md and "런너제거" in md
    assert "잿빛곰팡이병 — 발생단계: 2%미만 (주의)" in md
    assert "AI 초안 — 농가 확인 후 저장" in md and "프롬프트 v1" in md
    assert "`#2`" in md and "`#4`" in md


def test_diary_empty_template():
    tr, ctx = load_call("grape_multi_crop_no_work")
    nt = build_turns(tr)
    d = DiaryResult(prdlst_code="0603MM", prdlst_nm="포도", diary_date="2026-08-19", status="EMPTY")
    md = render_diary(d, ctx, nt, CropFacts(), model=None, prompt_version="1", now=NOW)
    assert "확인되지 않았습니다" in md and "## 주요 농작업\n- 언급 없음" in md


def test_report_headings_and_verification_mark():
    tr, ctx = load_call("strawberry_botrytis")
    nt = build_turns(tr)
    n = ReportNarrative(farm_status=[], issues=[Bullet(text="잿빛곰팡이 발생", evidence=[2], needs_verification=False)],
                        advice=[Bullet(text="[병해충관리] 사파이어 2000배", evidence=[7], needs_verification=True)],
                        farmer_actions=[], follow_ups=[], summary_line="요약", keywords=["a", "b"],
                        action_items=[ActionItem(owner="consultant", text="방문", due_hint="화요일", evidence=[11])])
    md = render_report(n, ctx, nt, speaker_roles=None, crops=["딸기"], warnings=[], model="m", prompt_version="1", now=NOW)
    assert md.startswith("# 컨설팅 보고서 — 2026-08-19 김철수 농가")
    assert _ordered(md, REPORT_HEADINGS)
    assert "※ 확인 필요" in md and "(컨설턴트) 방문 — 화요일" in md and "| 화자 식별 | 미식별 |" in md
    assert "## 농가·농장·작물 현황\n- 언급 없음" in md


def test_build_prefill_merges_existing():
    detail = json.loads((FIX / "farmos/0803MM/detail.json").read_text())
    palette = json.loads((FIX / "farmos/0803MM/palette.json").read_text())
    dbyhs = [expand_dbyhs(r) for r in json.loads((FIX / "farmos/0803MM/dbyhs.json").read_text())]
    refs = FarmosRefs(prdlst_code="0803MM", diary_date="2026-08-19", detail=detail, farmworks=palette, dbyhs=dbyhs)
    rep = MappingReport(farmworks=[MappedItem(item_id="fw0", family="farmwork", source="유인", status="matched", code="22", name="유인",
                                              payload=palette[1])],
                        pests=[MappedItem(item_id="p0", family="pest", source="담배가루이", status="matched", code="002005011",
                                          payload=dbyhs[4].single(4))])
    dto = build_prefill("2026-08-19", "0803MM", rep, "[AI 초안·통화 기반]\n새 내용", refs)
    assert dto.diaryId == 77
    assert [f.userFarmworkNm for f in dto.userFarmworkList] == ["관수", "유인"]      # 기존 체크 유지 + 신규
    assert dto.dbyhsList[0].occrrncStepCode == "4" and dto.dbyhsList[0].dbyhsNm == "담배가루이"
    assert dto.content.startswith("오전 관수 완료")
