"""파이프라인 계약 — 워커 ↔ LangGraph 파이프라인 사이의 입출력 모델.

워커(app/worker/generate_job.py)는 `MergedTranscript` + `CallContext` 를 넣고 `PipelineResult` 를 받아
S3/DB 에 저장한다. 파이프라인 내부 구조(CallFacts 등)는 app/agents/schemas.py 에 있고, 여기서는
`structured` 딕셔너리로만 노출한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Participant(BaseModel):
    role: Literal["farmer", "consultant"]
    user_id: str | None = None
    engn_id: str | None = None   # 영농체 ID — 농가 구분은 (engn_id, user_id) 복합 키 (user_id 단독 식별 금지)
    name: str | None = None


class CallHints(BaseModel):
    """호출자가 줄 수 있는 선택 힌트 (farmos 조회 실패 시 대체)."""
    prdlst_code: str | None = None
    prdlst_nm: str | None = None
    farmer_crops: list[dict[str, Any]] | None = None     # [{prdlstCode, prdlstNm, reprsntPrdlstCnt}]
    diary_date: str | None = None                        # yyyy-MM-dd
    topic: str | None = None
    # 농가 복합 키 — participants 의 farmer 항목이 비었을 때의 대체 경로. AP 백엔드 research API
    # (`/voicetalk/public/research/farm-context`) 조회 키로 쓴다. 계약: 백엔드 "AI 영농일지 연동 변경사항" §1·§3.
    farmer_engn_id: str | None = None
    farmer_user_id: str | None = None


class CallContext(BaseModel):
    call_id: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    participants: list[Participant] = Field(default_factory=list)
    farm: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    farm_access_token: str | None = None      # None → farmos 조회 없이 전사만으로 생성
    language: str = "ko"
    generation_run: int = 1
    hints: CallHints = Field(default_factory=CallHints)

    def name_of(self, role: str) -> str | None:
        for p in self.participants:
            if p.role == role and p.name:
                return p.name
        return None


DiaryStatus = Literal["OK", "PARTIAL", "EMPTY", "UNRESOLVED_CROP"]


class DiaryArtifact(BaseModel):
    prdlst_code: str | None            # 작물 id (farmos prdlstCode); 확정 불가 시 None
    prdlst_nm: str
    diary_date: str                    # yyyy-MM-dd
    status: DiaryStatus = "OK"
    markdown: str
    structured: dict[str, Any] = Field(default_factory=dict)
    # structured = {schema_version, prefill: PutDiaryDTO|None, prefill_ready, mapping, gsNm,
    #               growingSeasonStartDe, existing_diary_id, warnings, evidence}


class ReportArtifact(BaseModel):
    markdown: str
    structured: dict[str, Any] = Field(default_factory=dict)
    # structured = {summary, keywords, action_items, sections, speaker_map, needs_verification}


class CallSummaryResult(BaseModel):
    """통화 단순요약 — 영농일지와 별개로 전사에서 직접 뽑는다(백엔드 통화요약 콜백 content)."""
    topic: str = ""
    actions: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    markdown: str = ""                                # 렌더된 불릿 = 콜백 content = summary.md 본문
    model: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    source: str = "llm"                               # llm | report_fallback | fake


class PipelineResult(BaseModel):
    diaries: list[DiaryArtifact] = Field(default_factory=list)
    report: ReportArtifact | None = None
    speaker_map: dict[str, str] = Field(default_factory=dict)   # speaker_key → farmer|consultant|unknown
    facts: dict[str, Any] | None = None                          # CallFacts dump (디버그/재사용)
    warnings: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)          # {prompt_tokens, completion_tokens, calls}
    model: str | None = None
    prompt_version: str | None = None
    farmos_status: str = "disabled"                              # ok | partial | unavailable | disabled
