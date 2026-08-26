"""/v1/calls 요청·응답 모델 (docs/api-reference.md 와 정합)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .pipeline import Participant

CALL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class CallStartRequest(BaseModel):
    call_id: str
    started_at: datetime | None = None
    participants: list[Participant] = Field(default_factory=list, max_length=4)
    farm_access_token: str | None = None
    farm: dict[str, Any] | None = None
    num_speakers: int | None = Field(default=None, ge=1, le=8)
    language: str = "ko"
    callback_url: str | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("call_id")
    @classmethod
    def _cid(cls, v: str) -> str:
        if not CALL_ID_RE.match(v):
            raise ValueError("call_id must match [A-Za-z0-9_.:-]{1,128}")
        return v


class AudioReceivedRequest(BaseModel):
    bucket: str = Field(min_length=3, max_length=128)
    key: str = Field(min_length=1, max_length=1024)
    seq: int | None = None
    recorded_at: datetime | None = None
    duration_sec: float | None = Field(default=None, ge=0)
    speaker_hint: str | None = None
    force: bool = False


class CallEndRequest(BaseModel):
    ended_at: datetime | None = None
    duration_sec: float | None = Field(default=None, ge=0)


class RegenerateRequest(BaseModel):
    retranscribe: bool = False
    reason: str | None = None
    farm_access_token: str | None = None   # terminal 시 purge 된 농가 JWT 를 한 호출로 재공급 (daily 와 동일 계약)


# --- responses ------------------------------------------------------------

class AudioView(BaseModel):
    id: int
    bucket: str
    key: str
    seq: int | None = None
    recorded_at: datetime | None = None
    status: str
    attempts: int
    duration_sec: float | None = None
    stt_seconds: float | None = None
    offset_sec: float | None = None
    stt_raw_key: str | None = None
    segments_count: int | None = None
    last_error: str | None = None


class SttProgress(BaseModel):
    total: int = 0
    transcribed: int = 0
    failed: int = 0
    pending: int = 0


class GenerationView(BaseModel):
    run: int = 0
    attempts: int = 0
    state: str = "IDLE"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    model: str | None = None
    warnings: list[str] = Field(default_factory=list)
    usage: dict[str, Any] | None = None


class ErrorView(BaseModel):
    code: str
    message: str | None = None


class DiaryView(BaseModel):
    prdlst_code: str | None
    prdlst_nm: str | None
    diary_date: str | None
    status: str | None = None
    markdown: str | None = None
    structured: dict[str, Any] | None = None
    s3_key_md: str
    s3_key_json: str


class ReportView(BaseModel):
    markdown: str | None = None
    structured: dict[str, Any] | None = None
    s3_key_md: str
    s3_key_json: str


class SummaryView(BaseModel):
    """통화 단순요약 — 백엔드 통화요약 콜백으로 보낸 것과 같은 본문."""
    markdown: str | None = None
    structured: dict[str, Any] | None = None
    s3_key_md: str
    s3_key_json: str


class ResultView(BaseModel):
    transcript_key: str | None = None
    speaker_map: dict[str, str] = Field(default_factory=dict)
    diaries: list[DiaryView] = Field(default_factory=list)
    report: ReportView | None = None
    summary: SummaryView | None = None
    result_key: str | None = None


class CallDetail(BaseModel):
    call_id: str
    state: str
    status: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_sec: float | None = None
    created_at: datetime
    updated_at: datetime
    participants: list[Participant] = Field(default_factory=list)
    farm: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    stale: bool = False
    note: str | None = None
    stt_progress: SttProgress = Field(default_factory=SttProgress)
    audio: list[AudioView] = Field(default_factory=list)
    generation: GenerationView = Field(default_factory=GenerationView)
    error: ErrorView | None = None
    result: ResultView | None = None
    callback_status: str | None = None


class AudioAck(BaseModel):
    call_id: str
    audio: AudioView
    call_status: str
    call_state: str
    stt_progress: SttProgress
    note: str | None = None


class CallListItem(BaseModel):
    call_id: str
    state: str
    status: str
    updated_at: datetime
    stt_progress: SttProgress


class CallListResponse(BaseModel):
    items: list[CallListItem]
    next_cursor: str | None = None


StatusLiteral = Literal["NONE", "PROCESSING", "COMPLETED", "EMPTY", "FAILED"]
