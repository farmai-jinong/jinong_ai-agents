"""SQLite ORM — 통화(calls) / 녹음(call_audio) / 산출물(artifacts) / 이벤트(job_events).

DB 행이 곧 작업 큐다(계획 D1): call_audio.status/next_attempt_at, calls.gen_state 를 폴러가 클레임한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# 상태 어휘 (앱 sttStatus/aiSummaryStatus 와 동일)
CALL_STATES = ("OPEN", "ENDED")
CALL_STATUSES = ("NONE", "PROCESSING", "COMPLETED", "EMPTY", "FAILED")
GEN_STATES = ("IDLE", "QUEUED", "RUNNING")
AUDIO_STATUSES = ("PENDING", "TRANSCRIBING", "TRANSCRIBED", "FAILED")
ARTIFACT_KINDS = ("transcript", "diary_md", "diary_json", "report_md", "report_json", "result_json")


class Call(Base):
    __tablename__ = "calls"

    call_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="NONE", nullable=False)
    gen_state: Mapped[str] = mapped_column(String(16), default="IDLE", nullable=False)
    gen_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generation_run: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    generation_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    generation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generation_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generation_model: Mapped[str | None] = mapped_column(String(128))
    generation_warnings_json: Mapped[list[Any] | None] = mapped_column(JSON)
    usage_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_sec: Mapped[float | None] = mapped_column(Float)

    participants_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    farm_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    farm_access_token: Mapped[str | None] = mapped_column(Text)   # 응답/로그 제외, terminal 시 purge
    num_speakers: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String(8), default="ko", nullable=False)

    callback_url: Mapped[str | None] = mapped_column(Text)
    callback_status: Mapped[str | None] = mapped_column(String(32))
    callback_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    speaker_map_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    s3_prefix: Mapped[str] = mapped_column(String(256), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    audio: Mapped[list["CallAudio"]] = relationship(back_populates="call", cascade="all, delete-orphan",
                                                    order_by="CallAudio.id", lazy="selectin")
    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="call", cascade="all, delete-orphan",
                                                       lazy="selectin")

    __table_args__ = (
        Index("ix_calls_state_gen", "state", "gen_state"),
        Index("ix_calls_status", "status"),
        Index("ix_calls_updated", "updated_at"),
    )


class CallAudio(Base):
    __tablename__ = "call_audio"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.call_id", ondelete="CASCADE"), nullable=False)
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(1024), nullable=False)
    seq: Mapped[int | None] = mapped_column(Integer)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_sec: Mapped[float | None] = mapped_column(Float)      # 호출자 제공
    stt_seconds: Mapped[float | None] = mapped_column(Float)       # 게이트웨이 usage.seconds
    offset_sec: Mapped[float | None] = mapped_column(Float)        # 병합 시 계산
    speaker_hint: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    etag: Mapped[str | None] = mapped_column(String(128))

    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    error_permanent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    stt_raw_key: Mapped[str | None] = mapped_column(String(1024))
    segments_json: Mapped[list[Any] | None] = mapped_column(JSON)   # 게이트웨이 segments (경량 캐시)
    segments_count: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    call: Mapped[Call] = relationship(back_populates="audio")

    __table_args__ = (
        UniqueConstraint("call_id", "bucket", "key", name="uq_call_audio_ref"),
        Index("ix_call_audio_status_next", "status", "next_attempt_at"),
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.call_id", ondelete="CASCADE"), nullable=False)
    generation_run: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    prdlst_code: Mapped[str] = mapped_column(String(32), default="", nullable=False)   # "" = 작물 무관
    prdlst_nm: Mapped[str | None] = mapped_column(String(128))
    diary_date: Mapped[str | None] = mapped_column(String(10))
    diary_status: Mapped[str | None] = mapped_column(String(32))
    s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)              # inline (RESULT_INLINE_MAX_KB 이하)
    sha256: Mapped[str | None] = mapped_column(String(64))
    bytes: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    call: Mapped[Call] = relationship(back_populates="artifacts")

    __table_args__ = (
        UniqueConstraint("call_id", "kind", "prdlst_code", name="uq_artifact"),
    )


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    audio_id: Mapped[int | None] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
