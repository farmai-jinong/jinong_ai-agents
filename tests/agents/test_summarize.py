"""통화 단순요약(app/agents/summarize.py) — 파이프라인 없이 FakeChatModel 직접 주입."""

from __future__ import annotations

import pytest

from app.agents.summarize import LlmSummarizer, render_summary, summary_from_report
from app.agents.tools.fake_llm import FakeChatModel
from app.schemas.pipeline import CallContext
from app.schemas.transcript import MergedTranscript, TranscriptFile, TranscriptSegment

SUMMARY = {"topic": "딸기 잿빛곰팡이 방제를 상담함", "actions": ["환기 강화 권고", "사파이어 살포 권고"],
           "follow_ups": ["다음 주 화요일 방문 예정"], "evidence": [1, 2]}


def _transcript(n_segments: int, text: str = "딸기 잿빛곰팡이가 보입니다 환기를 어떻게 할까요") -> MergedTranscript:
    segs = []
    for i in range(n_segments):
        segs.append(TranscriptSegment(seg_id=f"s{i}", audio_id=1, file_index=0, speaker="A", speaker_key="f0:A",
                                      start=i * 5.0, end=i * 5.0 + 4.0, abs_start=i * 5.0, abs_end=i * 5.0 + 4.0,
                                      text=f"{text} {i}"))
    return MergedTranscript(call_id="c1", files=[TranscriptFile(audio_id=1, file_index=0, offset_sec=0.0,
                                                  bucket="b", key="k")],
                            segments=segs, speakers=["f0:A"], text="\n".join(s.text for s in segs),
                            total_duration_sec=n_segments * 5.0)


def _ctx() -> CallContext:
    return CallContext(call_id="c1", participants=[{"role": "farmer", "name": "홍길동"},
                                                   {"role": "consultant", "name": "김상담"}])


def test_render_summary_skips_empty_lines():
    assert render_summary("주제만", [], []) == "- 주제: 주제만"
    md = render_summary("주제", ["a", "a", "b", "c", "d"], ["f1", "f2", "f3"])
    assert md.splitlines() == ["- 주제: 주제", "- 조치: a / b / c", "- 후속: f1 / f2"]   # 중복 제거·상한 적용
    assert render_summary("", [], []) == ""


def test_summary_from_report_fallback():
    out = summary_from_report({"summary": "딸기 방제를 안내함", "action_items": [{"text": "환기 강화"}]})
    assert out is not None and out.source == "report_fallback"
    assert out.markdown == "- 주제: 딸기 방제를 안내함\n- 조치: 환기 강화"
    assert summary_from_report({"summary": ""}) is None
    assert summary_from_report(None) is None


@pytest.mark.asyncio
async def test_summarize_single_call(settings):
    llm = FakeChatModel(responses={"call_summary": SUMMARY})
    out = await LlmSummarizer(settings, llm=llm).summarize(_transcript(3), _ctx())
    assert out.topic == SUMMARY["topic"] and out.source == "llm"
    assert out.markdown.splitlines() == ["- 주제: 딸기 잿빛곰팡이 방제를 상담함",
                                         "- 조치: 환기 강화 권고 / 사파이어 살포 권고",
                                         "- 후속: 다음 주 화요일 방문 예정"]
    assert len([c for c in llm.calls if c]) == 1                 # 짧은 전사는 1회 호출
    assert out.usage.get("total_tokens") == 150


@pytest.mark.asyncio
async def test_summarize_chunks_long_call_then_merges(settings):
    """긴 전사는 구간 요약 → 통합 1회(map-reduce)."""
    settings.extract_max_input_tokens = 200
    settings.chunk_tokens = 150
    merged = {**SUMMARY, "topic": "통합 주제"}
    llm = FakeChatModel(responses={"call_summary": SUMMARY, "call_summary_merge": merged})
    out = await LlmSummarizer(settings, llm=llm).summarize(_transcript(40), _ctx())
    assert out.topic == "통합 주제"
    kinds = [c["kind"] for c in llm.calls]
    assert kinds.count("call_summary_merge") == 1 and kinds.count("call_summary") >= 2


@pytest.mark.asyncio
async def test_summarize_empty_transcript_returns_blank(settings):
    llm = FakeChatModel(responses={"call_summary": SUMMARY})
    t = MergedTranscript(call_id="c1", files=[], segments=[], speakers=[], text="", total_duration_sec=0.0)
    out = await LlmSummarizer(settings, llm=llm).summarize(t, _ctx())
    assert out.markdown == "" and not llm.calls
