"""일지 검수 패스 — 실질 내용 없는 초안을 EMPTY 로 강등하고, 실패해도 생성을 막지 않는다."""

import pytest

from app.agents.interface import PipelineEmpty
from app.agents.nodes.crop_diary.verify_diary import strip_lead_quotes
from app.schemas.pipeline import PipelineResult

from .conftest import fake_llm, load_call, make_pipeline
from .test_graph import STRAW_CONTENT, STRAW_REPORT

EMPTY_TEMPLATE_LINE = "통화에서 이 작물의 영농일지에 기록할"
BLANK_REPORT = {"farm_status": [], "issues": [], "advice": [], "farmer_actions": [], "follow_ups": [],
                "summary_line": "", "keywords": [], "action_items": []}


def _verdict(has: bool, confidence: float = 0.9, reason: str = "잡담만 오감"):
    return {"has_diary_content": has, "reason": reason, "confidence": confidence, "evidence": []}


def _run(settings, farmos_fake, **verify):
    """딸기 정상 통화 + 검수 응답만 바꿔 파이프라인을 돌린다."""
    tr, ctx = load_call("strawberry_botrytis")
    llm = fake_llm("strawberry_botrytis",
                   responses={"diary_content": STRAW_CONTENT, "report": STRAW_REPORT, **verify})
    return llm, make_pipeline(settings, llm, farmos_fake).run(tr, ctx)


@pytest.mark.asyncio
async def test_verdict_empty_downgrades_and_rerenders(settings, farmos_fake):
    """실질 내용 없음 판정 → status EMPTY, 빈 템플릿 재렌더, prefill 회수."""
    llm, coro = _run(settings, farmos_fake, verify_diary=_verdict(False))
    res = await coro
    d = res.diaries[0]
    assert d.status == "EMPTY"
    assert EMPTY_TEMPLATE_LINE in d.markdown
    body = strip_lead_quotes(d.markdown)
    assert "사파이어" not in body                              # 원래 초안이 남아 있지 않다(상단 통화 요약 줄은 예외)
    assert d.markdown.startswith("> 📝 **통화 요약** · ") and "다음 통화도 응원할게요" in d.markdown   # 요약 유지, 격려는 중립 문구
    assert "꼼꼼히" not in d.markdown
    assert d.structured["prefill"] is None and d.structured["prefill_ready"] is False
    assert d.structured["verify"]["has_diary_content"] is False
    assert any("검수" in w for w in res.warnings)


@pytest.mark.asyncio
async def test_low_confidence_keeps_status(settings, farmos_fake):
    """확신이 임계값 미만이면 강등하지 않는다 — 판정 기록만 남는다."""
    llm, coro = _run(settings, farmos_fake, verify_diary=_verdict(False, confidence=0.3))
    res = await coro
    d = res.diaries[0]
    assert d.status == "OK"
    assert EMPTY_TEMPLATE_LINE not in d.markdown
    assert d.structured["verify"]["confidence"] == 0.3


@pytest.mark.asyncio
async def test_verdict_ok_is_noop(settings, farmos_fake):
    llm, coro = _run(settings, farmos_fake, verify_diary=_verdict(True, reason="관수·적엽 기록 있음"))
    res = await coro
    d = res.diaries[0]
    assert d.status == "OK" and d.structured["verify"]["has_diary_content"] is True
    assert d.structured["prefill"] is not None


@pytest.mark.asyncio
async def test_verify_failure_is_fail_open(settings, farmos_fake):
    """검수 LLM 이 죽어도 초안은 그대로 나간다 (규칙 판정 유지)."""
    tr, ctx = load_call("strawberry_botrytis")
    llm = fake_llm("strawberry_botrytis", responses={"diary_content": STRAW_CONTENT, "report": STRAW_REPORT},
                   fail_kinds={"verify_diary"})
    res = await make_pipeline(settings, llm, farmos_fake).run(tr, ctx)
    d = res.diaries[0]
    assert d.status == "OK" and d.structured["verify"] is None
    assert any("검수 실패" in w for w in res.warnings)


@pytest.mark.asyncio
async def test_disabled_skips_llm_call(settings, farmos_fake):
    settings.verify_diary_enabled = False
    llm, coro = _run(settings, farmos_fake, verify_diary=_verdict(False))
    res = await coro
    assert res.diaries[0].status == "OK"
    assert not [c for c in llm.calls if c["kind"] == "verify_diary"]
    assert res.usage["calls"] == 4                              # 검수 콜 없음


@pytest.mark.asyncio
async def test_already_empty_diary_skips_llm_call(settings):
    """규칙 판정이 이미 EMPTY 면 검수 LLM 을 부르지 않는다 (그리고 통화는 PipelineEmpty)."""
    tr, ctx = load_call("grape_multi_crop_no_work")
    llm = fake_llm("grape_multi_crop_no_work",
                   responses={"verify_diary": _verdict(True), "report": BLANK_REPORT})
    with pytest.raises(PipelineEmpty):
        await make_pipeline(settings, llm).run(tr, ctx)
    assert not [c for c in llm.calls if c["kind"] == "verify_diary"]


@pytest.mark.asyncio
async def test_all_crops_verified_empty_raises_pipeline_empty(settings, farmos_fake):
    """작물이 전부 검수에서 EMPTY 이고 보고서도 비면 통화 자체가 EMPTY 로 끝난다.

    사실 추출은 성공했으므로(facts 는 비어 있지 않다) 검수 판정이 없으면 잡히지 않던 케이스다.
    """
    tr, ctx = load_call("strawberry_botrytis")
    llm = fake_llm("strawberry_botrytis",
                   responses={"diary_content": STRAW_CONTENT, "report": BLANK_REPORT,
                              "verify_diary": _verdict(False)})
    with pytest.raises(PipelineEmpty):
        await make_pipeline(settings, llm, farmos_fake).run(tr, ctx)


@pytest.mark.asyncio
async def test_report_failure_does_not_report_empty(settings, farmos_fake):
    """보고서 노드가 죽었을 뿐인데 통화를 EMPTY 로 오보하지 않는다 — 전사는 남기고 COMPLETED."""
    tr, ctx = load_call("strawberry_botrytis")
    llm = fake_llm("strawberry_botrytis",
                   responses={"diary_content": STRAW_CONTENT, "verify_diary": _verdict(False)},
                   fail_kinds={"report"})
    res = await make_pipeline(settings, llm, farmos_fake).run(tr, ctx)
    assert isinstance(res, PipelineResult) and res.diaries[0].status == "EMPTY"


def test_strip_lead_quotes_removes_only_top_block():
    """검수 LLM 입력에서는 상단 요약·격려 인용 블록만 떼고, 본문 안의 `>` 안내문은 그대로 둔다."""
    md = "> 📝 **통화 요약** · 요약\n> 💬 격려 🌱\n\n| 항목 | 값 |\n|---|---|\n> 이 날짜에 기존 일지(#7)가 있어요.\n\n## 주요 농작업\n- 언급 없음\n"
    out = strip_lead_quotes(md)
    assert out.startswith("| 항목 | 값 |") and "격려" not in out and "기존 일지(#7)" in out
