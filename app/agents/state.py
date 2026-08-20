"""LangGraph 상태 정의 (메인 그래프 / 작물별 서브그래프)."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from ..schemas.pipeline import CallContext
from ..schemas.transcript import MergedTranscript
from .schemas import (
    CallFacts,
    CropFacts,
    CropTarget,
    DiaryContentOut,
    DiaryResult,
    FarmContext,
    LLMUsage,
    MappingReport,
    NormalizedTranscript,
    PipelineError,
    ReportResult,
    SpeakerRoleResult,
)


def _last(a: Any, b: Any) -> Any:
    return b if b is not None else a


class PipelineState(TypedDict, total=False):
    ctx: CallContext
    raw: MergedTranscript
    diary_date: str
    transcript: NormalizedTranscript
    speaker_roles: SpeakerRoleResult | None
    farm: FarmContext
    facts: CallFacts
    facts_meta: dict[str, Any]
    crop_targets: list[CropTarget]
    crop_facts: dict[str, CropFacts]            # target key → facts
    diaries: Annotated[list[DiaryResult], operator.add]
    report: ReportResult | None
    errors: Annotated[list[PipelineError], operator.add]
    usage: Annotated[list[LLMUsage], operator.add]
    warnings: Annotated[list[str], operator.add]


class CropDiaryState(TypedDict, total=False):
    ctx: CallContext
    diary_date: str
    transcript: NormalizedTranscript
    farm: FarmContext
    target: CropTarget
    crop_facts: CropFacts
    refs: Any                                   # FarmosRefs | None
    refs_status: str
    mapping: MappingReport
    content: DiaryContentOut | None
    diary: DiaryResult
    diaries: Annotated[list[DiaryResult], operator.add]
    errors: Annotated[list[PipelineError], operator.add]
    usage: Annotated[list[LLMUsage], operator.add]
    warnings: Annotated[list[str], operator.add]
