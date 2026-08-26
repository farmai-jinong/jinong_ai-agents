"""워커 ↔ 파이프라인 계약."""

from __future__ import annotations

from typing import Protocol

from ..schemas.pipeline import CallContext, CallSummaryResult, PipelineResult
from ..schemas.transcript import MergedTranscript


class PipelineEmpty(Exception):
    """통화에 영농일지/보고서로 남길 내용이 전혀 없음 → 통화 status EMPTY."""


class DiaryReportPipeline(Protocol):
    async def run(self, transcript: MergedTranscript, ctx: CallContext) -> PipelineResult: ...


class CallSummarizer(Protocol):
    """통화 단순요약 — 일지 파이프라인과 독립. 콜백 content 를 만든다(app/agents/summarize.py)."""

    async def summarize(self, transcript: MergedTranscript, ctx: CallContext) -> CallSummaryResult: ...
