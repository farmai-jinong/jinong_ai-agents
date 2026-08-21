"""병합 전사(MergedTranscript) — 워커가 파이프라인에 넘기는 입력 형식.

게이트웨이 화자분리 응답의 speaker 글자(A/B…)는 **요청(파일)마다 새로 배정**되므로 파일 간에
뒤바뀔 수 있다. 그래서 `speaker_key = f"f{file_index}:{speaker}"` 로 파일 단위 식별자를 유지한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TranscriptSegment(BaseModel):
    file_index: int
    audio_id: int
    seg_id: str                     # 게이트웨이 id ("seg_3"), 실행마다 재번호 — 조인 키로 쓰지 말 것
    speaker: str                    # 파일 내 글자 ("A")
    speaker_key: str                # "f0:A"
    start: float                    # 파일 내 초
    end: float
    abs_start: float                # 통화 전체 기준 초 (파일 오프셋 합산)
    abs_end: float
    text: str                       # 선행 공백 제거


class TranscriptFile(BaseModel):
    file_index: int
    audio_id: int
    call_id: str | None = None      # 날짜별(멀티콜) 병합 시 원본 통화 (단일 통화 병합에서는 None)
    bucket: str
    key: str
    seq: int | None = None
    recorded_at: datetime | None = None
    offset_sec: float = 0.0
    duration_sec: float = 0.0
    speaker_hint: str | None = None
    stt_raw_key: str | None = None
    status: Literal["TRANSCRIBED", "FAILED"] = "TRANSCRIBED"
    error: str | None = None


class MergedTranscript(BaseModel):
    call_id: str
    files: list[TranscriptFile] = Field(default_factory=list)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    speakers: list[str] = Field(default_factory=list)   # speaker_key 최초 등장 순
    total_duration_sec: float = 0.0
    text: str = ""                                       # "[f0:A] …" 한 줄씩 (프롬프트/사람용)

    @property
    def is_empty(self) -> bool:
        return not any(s.text.strip() for s in self.segments)
