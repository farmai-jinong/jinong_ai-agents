"""워커 ↔ 파이프라인 계약."""

from __future__ import annotations

from typing import Protocol

from ..schemas.pipeline import CallContext, PipelineResult
from ..schemas.transcript import MergedTranscript


class PipelineEmpty(Exception):
    """통화에 영농일지/보고서로 남길 내용이 전혀 없음 → 통화 status EMPTY."""


class DiaryReportPipeline(Protocol):
    async def run(self, transcript: MergedTranscript, ctx: CallContext) -> PipelineResult: ...
