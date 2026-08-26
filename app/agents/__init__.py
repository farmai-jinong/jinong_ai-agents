"""LangGraph 파이프라인 (⑧ 에이전트 본체).

워커는 `build_pipeline()` 만 사용한다. 내부 그래프는 graph.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .interface import CallSummarizer, DiaryReportPipeline, PipelineEmpty

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

__all__ = ["CallSummarizer", "DiaryReportPipeline", "PipelineEmpty", "build_pipeline",
           "build_summarizer"]


def build_pipeline(settings: "Settings") -> DiaryReportPipeline:
    """PIPELINE_IMPL 에 따라 구현을 고른다 (langgraph | fake)."""
    impl = (settings.pipeline_impl or "langgraph").lower()
    if impl == "fake":
        from .fake import FakePipeline

        return FakePipeline()
    from .graph import LangGraphPipeline

    return LangGraphPipeline(settings)


def build_summarizer(settings: "Settings") -> CallSummarizer:
    """통화 단순요약 구현 선택 — 파이프라인과 같은 PIPELINE_IMPL 스위치를 쓴다."""
    from .summarize import FakeSummarizer, LlmSummarizer

    if (settings.pipeline_impl or "langgraph").lower() == "fake":
        return FakeSummarizer()
    return LlmSummarizer(settings)
