"""/v1/daily-diaries 요청·응답 모델 — 날짜별(멀티콜) 영농일지 (docs/api-reference.md 와 정합)."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .calls import CALL_ID_RE, DiaryView, ErrorView, GenerationView

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CROP_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")   # diary_id 에 들어가야 하므로 같은 문자 집합


class DailyCropSpec(BaseModel):
    """작물 고정 모드의 대상 작물 — 코드·이름 중 하나 이상. 파이프라인은 이 작물 **하나만** 생성한다."""
    prdlst_code: str | None = None     # farmos/AP 표준 품목코드 (예: "0804MM")
    prdlst_nm: str | None = None       # 작물명 (예: "딸기")

    @field_validator("prdlst_code")
    @classmethod
    def _code(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        if not v:
            return None
        if not CROP_CODE_RE.match(v):
            raise ValueError("crop.prdlst_code must match [A-Za-z0-9_.:-]{1,64}")
        return v

    @field_validator("prdlst_nm")
    @classmethod
    def _nm(cls, v: str | None) -> str | None:
        v = unicodedata.normalize("NFKC", v or "").strip()
        if not v:
            return None
        if len(v) > 64:
            raise ValueError("crop.prdlst_nm must be 1..64 chars")
        return v

    @model_validator(mode="after")
    def _one_of(self) -> DailyCropSpec:
        if not (self.prdlst_code or self.prdlst_nm):
            raise ValueError("crop requires prdlst_code or prdlst_nm")
        return self


def crop_equal(a: DailyCropSpec | dict[str, Any] | None, b: DailyCropSpec | dict[str, Any] | None) -> bool:
    """재-POST 불변 검사 — 둘 다 코드가 있으면 코드로, 아니면 정규화한 이름(NFKC·공백 제거·소문자)으로 비교."""
    if a is None or b is None:
        return a is b
    da = a.model_dump() if isinstance(a, DailyCropSpec) else dict(a)
    db = b.model_dump() if isinstance(b, DailyCropSpec) else dict(b)
    if da.get("prdlst_code") and db.get("prdlst_code"):
        return str(da["prdlst_code"]) == str(db["prdlst_code"])

    def norm(s: Any) -> str:
        return "".join(unicodedata.normalize("NFKC", str(s or "")).split()).lower()
    return norm(da.get("prdlst_nm")) == norm(db.get("prdlst_nm")) and bool(norm(da.get("prdlst_nm")))


class DailyDiaryCreateRequest(BaseModel):
    diary_id: str                      # 멱등성 키 — 백엔드가 결정적으로 생성 (예: daily_{farmer}_{yyyyMMdd})
    diary_date: str                    # yyyy-MM-dd (산출물 일지 날짜로 고정)
    call_ids: list[str] = Field(min_length=1, max_length=50)
    farm_access_token: str | None = None   # 없으면 farmos 조회 없이 생성 (기존 call 토큰은 이미 purge됨)
    callback_url: str | None = None
    language: str = "ko"
    metadata: dict[str, Any] | None = None  # hints 포함 가능 (CallHints 형식)
    crop: DailyCropSpec | None = None      # 작물 고정 모드 — 이 작물 일지 1건만 생성(자동 판정 없음). None = 기존 자동 판정

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
    crop: DailyCropSpec | None = None     # 생성 시 고정한 작물(요청값 정규화) — 자동 판정이면 null. 판정 결과는 result.diaries[]
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
