"""LLM 출력 계약 — 치환 **목록** 만 받는다(전사문 재작성 금지: 잡담 줄이 흔들리면 CER 이 새로 깨진다)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TermCorrection(BaseModel):
    seg_id: int = Field(description="발화 번호(#n)")
    original: str = Field(description="발화에 적힌 그대로의 오청 어절(조사 제외, 글자 단위로 정확히)")
    replacement: str = Field(description="카탈로그에 있는 표기 그대로")
    category: str | None = Field(default=None, description="pesticide_brand | pest | disease | company")
    confidence: float = Field(ge=0.0, le=1.0, description="0~1")
    reason: str = Field(default="", description="발음 유사·문맥 근거 한 줄")


class TermFixOut(BaseModel):
    corrections: list[TermCorrection] = Field(default_factory=list)
