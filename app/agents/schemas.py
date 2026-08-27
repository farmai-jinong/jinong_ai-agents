"""파이프라인 내부 스키마 — LLM 구조화 출력, 매핑 결과, 일지/보고서 중간 결과.

LLM 출력 모델 규칙: 필드는 전부 필수(선택은 `X | None`, 기본값 없음), `extra="forbid"` —
OpenAI strict json_schema 와 vLLM guided decoding 양쪽에서 같은 스키마가 통한다.
모든 사실(fact) 은 `evidence: list[int]` (turn id, `#n`) 를 가진다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Ev = list[int]
Role = Literal["farmer", "consultant", "unknown"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- transcript
class Turn(BaseModel):
    tid: int
    file_index: int
    speaker_letter: str
    speaker_key: str
    role: Role = "unknown"
    start_sec: float
    end_sec: float
    abs_start: float
    abs_end: float
    text: str
    seg_ids: list[str] = Field(default_factory=list)


class NormalizedTranscript(BaseModel):
    turns: list[Turn]
    n_files: int
    est_tokens: int
    duration_sec: float

    def by_tid(self, tid: int) -> Turn | None:
        if 0 <= tid < len(self.turns) and self.turns[tid].tid == tid:
            return self.turns[tid]
        for t in self.turns:
            if t.tid == tid:
                return t
        return None


# --------------------------------------------------------------------------- speaker roles (LLM)
class LetterRole(_Strict):
    letter: str
    role: Literal["farmer", "consultant", "other"]


class FileSpeakerMap(_Strict):
    file_index: int
    roles: list[LetterRole]          # 화자 글자 → 역할 (strict json_schema 호환을 위해 dict 대신 배열)
    confidence: float
    rationale: str

    @property
    def mapping(self) -> dict[str, str]:
        return {r.letter: r.role for r in self.roles}


class SpeakerRoleResult(_Strict):
    files: list[FileSpeakerMap]


# --------------------------------------------------------------------------- CallFacts (LLM)
class CropMention(_Strict):
    name_raw: str
    matched_name: str | None
    evidence: Ev


class FarmworkFact(_Strict):
    name: str
    crop: str | None
    when: Literal["today", "ongoing", "past", "planned", "unknown"]
    date_hint: str | None
    detail: str | None
    evidence: Ev


class ObservationFact(_Strict):
    topic: Literal["생육", "환경", "토양·양액", "시설·장비", "기타"]
    text: str
    crop: str | None
    evidence: Ev


class PestFact(_Strict):
    name: str
    kind: Literal["병", "해충", "생리장해", "불명"]
    status: Literal["발생", "의심", "예방언급"]
    severity: Literal["경미", "보통", "심함", "불명"]
    severity_raw: str | None
    location: str | None
    crop: str | None
    evidence: Ev


class ProductFact(_Strict):
    name: str
    category: Literal["농약", "비료", "종자", "농자재", "기타"]
    target: str | None
    dose: str | None
    when: Literal["applied", "planned", "recommended", "unknown"]
    date_hint: str | None
    crop: str | None
    evidence: Ev


class QuestionFact(_Strict):
    text: str
    asked_by: Literal["farmer", "consultant", "unknown"]
    evidence: Ev


class AdviceFact(_Strict):
    text: str
    category: Literal["환경관리", "근권관리", "작물관리", "병해충관리", "경영·기타"]
    evidence: Ev


class ActionFact(_Strict):
    text: str
    actor: Literal["farmer", "consultant"]
    status: Literal["done", "agreed", "planned"]
    due_hint: str | None
    evidence: Ev


class FollowUpFact(_Strict):
    text: str
    when_hint: str | None
    evidence: Ev


class CallFacts(_Strict):
    one_line_summary: str
    keywords: list[str]
    crops_mentioned: list[CropMention]
    farm_status: list[ObservationFact]
    farmworks: list[FarmworkFact]
    observations: list[ObservationFact]
    pests: list[PestFact]
    products: list[ProductFact]
    questions: list[QuestionFact]
    advice: list[AdviceFact]
    actions: list[ActionFact]
    follow_ups: list[FollowUpFact]
    has_farmwork_content: bool
    stt_uncertainties: list[str]

    @classmethod
    def empty(cls, reason: str = "") -> "CallFacts":
        return cls(one_line_summary="", keywords=[], crops_mentioned=[], farm_status=[], farmworks=[],
                   observations=[], pests=[], products=[], questions=[], advice=[], actions=[],
                   follow_ups=[], has_farmwork_content=False,
                   stt_uncertainties=[reason] if reason else [])

    def is_blank(self) -> bool:
        return not any([self.crops_mentioned, self.farm_status, self.farmworks, self.observations,
                        self.pests, self.products, self.questions, self.advice, self.actions,
                        self.follow_ups, self.one_line_summary.strip()])


class SummaryOut(_Strict):
    one_line_summary: str
    keywords: list[str]


# --------------------------------------------------------------------------- narrative outputs (LLM)
class DiaryContentOut(_Strict):
    content: str
    evidence: Ev


class DiaryVerdictOut(_Strict):
    """일지 검수 패스 — 렌더된 초안에 실질적인 영농일지 기록거리가 있는지 재확인."""
    has_diary_content: bool
    reason: str
    confidence: float
    evidence: Ev


class JudgeDimension(_Strict):
    """평가 하네스 전용 — 정답 일지 대비 채점 축 하나."""
    name: Literal["coverage", "faithfulness", "classification", "severity", "chatter", "format"]
    score: int                       # 1~5
    reason: str


class JudgeItem(_Strict):
    """감점 사유 1건 + 원인 귀속 — 이 귀속이 다음에 뭘 고칠지(프롬프트/매핑/STT)를 정한다."""
    kind: Literal["missing", "hallucinated", "misclassified", "chatter_leak"]
    section: str                     # 일지 섹션명 ("방제이력" 등)
    text: str
    cause: Literal["stt", "extraction", "mapping", "rendering", "unknown"]


class DiaryJudgeOut(_Strict):
    """영농일지 초안 채점 결과 (app/agents/voice_eval)."""
    dimensions: list[JudgeDimension]
    items: list[JudgeItem]
    overall: int                     # 1~5
    summary: str


class Bullet(_Strict):
    text: str
    evidence: Ev
    needs_verification: bool


class ActionItem(_Strict):
    owner: Literal["farmer", "consultant"]
    text: str
    due_hint: str | None
    evidence: Ev


class ReportNarrative(_Strict):
    farm_status: list[Bullet]
    issues: list[Bullet]
    advice: list[Bullet]
    farmer_actions: list[Bullet]
    follow_ups: list[Bullet]
    summary_line: str
    keywords: list[str]
    action_items: list[ActionItem]


class PickOut(_Strict):
    item_id: str
    choice: str | None


class DisambiguateOut(_Strict):
    picks: list[PickOut]


# --------------------------------------------------------------------------- farm context / crop targets
class CropRef(BaseModel):
    prdlstCode: str | None = None
    prdlstNm: str
    reprsntPrdlstCnt: int | None = None
    use: bool | None = None


class FarmContext(BaseModel):
    crops: list[CropRef] = Field(default_factory=list)
    source: Literal["farmos", "hints", "none"] = "none"
    status: Literal["ok", "partial", "unavailable", "disabled"] = "disabled"
    error: str | None = None


class CropTarget(BaseModel):
    prdlst_code: str | None
    prdlst_nm: str
    reason: str
    resolved: bool = True          # False → UNRESOLVED_CROP


class CropFacts(BaseModel):
    """대상 작물 하나로 라우팅된 사실 부분집합."""
    farmworks: list[FarmworkFact] = Field(default_factory=list)
    observations: list[ObservationFact] = Field(default_factory=list)
    pests: list[PestFact] = Field(default_factory=list)
    products: list[ProductFact] = Field(default_factory=list)
    follow_ups: list[FollowUpFact] = Field(default_factory=list)
    actions: list[ActionFact] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.farmworks or self.observations or self.pests
                    or [p for p in self.products if p.when in ("applied", "unknown")])


# --------------------------------------------------------------------------- mapping
MapStatus = Literal["matched", "ambiguous", "unmatched", "no_refs"]


class MappedItem(BaseModel):
    item_id: str
    family: Literal["farmwork", "pest", "product", "crop"]
    source: str                      # 원문 이름
    status: MapStatus
    code: str | None = None
    name: str | None = None
    score: float | None = None
    method: str | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)   # [{code, name, score}]
    evidence: Ev = Field(default_factory=list)
    needs_verification: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)            # family 별 부가 정보
    when: str | None = None
    category: str | None = None
    warnings: list[str] = Field(default_factory=list)


class MappingReport(BaseModel):
    farmworks: list[MappedItem] = Field(default_factory=list)
    pests: list[MappedItem] = Field(default_factory=list)
    products: list[MappedItem] = Field(default_factory=list)

    def ambiguous(self) -> list[MappedItem]:
        return [m for fam in (self.farmworks, self.pests, self.products) for m in fam if m.status == "ambiguous"]


# --------------------------------------------------------------------------- PutDiaryDTO (prefill)
class UserFarmworkVO(BaseModel):
    userFarmworkId: int | None = None
    userFarmworkNm: str
    userAdded: bool = False
    use: bool = True
    checked: bool = True


class DbyhsSingle(BaseModel):
    dbyhsCode: str
    dbyhsNm: str
    occrrncStepNm: str
    occrrncStepCode: str
    occrrncStepDesc: str
    occrrncStepDescCode: str


class PrvnbeNPesti(BaseModel):
    prvnbeTypeCode: str = ""
    prvnbeCode: str = ""
    prvnbeNm: str = ""
    pestiCode: str = ""
    pestiNm: str = ""


class PutDiaryDTO(BaseModel):
    diaryId: int | None = None
    diaryDate: str
    prdlstCode: str | None
    userFarmworkList: list[UserFarmworkVO] = Field(default_factory=list)
    content: str = ""
    dbyhsList: list[DbyhsSingle] = Field(default_factory=list)
    prvnbeNPestiList: list[PrvnbeNPesti] = Field(default_factory=list)
    deleteAtchFileSnList: list[int] = Field(default_factory=list)


# --------------------------------------------------------------------------- results
class DiaryResult(BaseModel):
    prdlst_code: str | None
    prdlst_nm: str
    diary_date: str
    status: Literal["OK", "PARTIAL", "EMPTY", "UNRESOLVED_CROP"]
    gs_nm: str | None = None
    growing_season_start: str | None = None
    existing_diary_id: int | None = None
    existing_farmworks: list[str] = Field(default_factory=list)   # 기존 일지에 체크돼 있던 농작업(유지)
    markdown: str = ""
    prefill: PutDiaryDTO | None = None
    prefill_ready: bool = False
    mapping: MappingReport = Field(default_factory=MappingReport)
    content: str = ""
    warnings: list[str] = Field(default_factory=list)
    evidence: Ev = Field(default_factory=list)
    verify: DiaryVerdictOut | None = None      # 검수 패스 판정(없으면 미실행/실패)


class ReportJSON(BaseModel):
    summary: str = ""
    keywords: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    sections: dict[str, list[Bullet]] = Field(default_factory=dict)
    speaker_map: list[FileSpeakerMap] = Field(default_factory=list)
    needs_verification: list[str] = Field(default_factory=list)


class ReportResult(BaseModel):
    markdown: str
    json_: ReportJSON = Field(alias="json")
    model_config = ConfigDict(populate_by_name=True)


class LLMUsage(BaseModel):
    name: str
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    mode: str | None = None
    attempts: int = 1


class PipelineError(BaseModel):
    node: str
    message: str
    fatal: bool = False


class DiaryDateInfo(BaseModel):
    diary_date: date
    call_date_local: datetime | None = None
