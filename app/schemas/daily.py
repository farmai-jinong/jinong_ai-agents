"""/v1/daily-diaries 요청·응답 모델 — 날짜별(멀티콜) 영농일지 (docs/api-reference.md 와 정합)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .calls import CALL_ID_RE, DiaryView, ErrorView, GenerationView

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DailyDiaryCreateRequest(BaseModel):
    diary_id: str                      # 멱등성 키 — 백엔드가 결정적으로 생성 (예: daily_{farmer}_{yyyyMMdd})
    diary_date: str                    # yyyy-MM-dd (산출물 일지 날짜로 고정)
    call_ids: list[str] = Field(min_length=1, max_length=50)
    farm_access_token: str | None = None   # 없으면 farmos 조회 없이 생성 (기존 call 토큰은 이미 purge됨)
    callback_url: str | None = None
    language: str = "ko"
    metadata: dict[str, Any] | None = None  # hints 포함 가능 (CallHints 형식)

    @field_validator("diary_id")
    @classmethod
    def _did(cls, v: str) -> str:
        if not CALL_ID_RE.match(v):
            raise ValueError("diary_id must match [A-Za-z0-9_.:-]{1,128}")
        return v

    @field_validator("diary_date")
    @classmethod
    def _date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("diary_date must be yyyy-MM-dd")
        return v

    @field_validator("call_ids")
    @classmethod
    def _cids(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("call_ids must not contain duplicates")
        for cid in v:
            if not CALL_ID_RE.match(cid):
                raise ValueError(f"invalid call_id: {cid!r}")
        return v


class DailyRegenerateRequest(BaseModel):
    farm_access_token: str | None = None   # 재생성 시점에도 이전 토큰은 purge되어 있으므로 새로 전달
    reason: str | None = None


# --- responses ------------------------------------------------------------

class DailyResultView(BaseModel):
    transcript_key: str | None = None
    speaker_map: dict[str, str] = Field(default_factory=dict)
    diaries: list[DiaryView] = Field(default_factory=list)
    result_key: str | None = None


class DailyDiaryDetail(BaseModel):
    diary_id: str
    diary_date: str
    status: str
    call_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] | None = None
    note: str | None = None
    generation: GenerationView = Field(default_factory=GenerationView)
    error: ErrorView | None = None
    result: DailyResultView | None = None
    callback_status: str | None = None


class DailyDiaryListItem(BaseModel):
    diary_id: str
    diary_date: str
    status: str
    updated_at: datetime


class DailyDiaryListResponse(BaseModel):
    items: list[DailyDiaryListItem]
    next_cursor: str | None = None
