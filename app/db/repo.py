"""모든 SQL 은 여기에. 서비스/워커는 세션 + 이 함수들만 쓴다."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Artifact, Call, CallAudio, DailyArtifact, DailyDiary, JobEvent, utcnow

ACTIVE_AUDIO = ("PENDING", "TRANSCRIBING")
TERMINAL_STATUSES = ("COMPLETED", "EMPTY", "FAILED")


# --- 목록 keyset 커서 -------------------------------------------------------
# 정렬(created_at DESC, id DESC)과 일치하는 복합 커서. 클라이언트에겐 불투명 토큰.

def make_list_cursor(created_at: datetime, ident: str) -> str:
    return f"{created_at.isoformat()}|{ident}"


def _parse_list_cursor(cursor: str) -> tuple[datetime, str]:
    """실패 시 ValueError — 라우트에서 422 로 변환한다."""
    ts, _, ident = cursor.partition("|")
    if not ident:
        raise ValueError(f"malformed cursor: {cursor!r}")
    return datetime.fromisoformat(ts), ident


# --- calls -----------------------------------------------------------------

async def get_call(s: AsyncSession, call_id: str) -> Call | None:
    return await s.get(Call, call_id)


async def list_calls(s: AsyncSession, *, status: str | None = None, state: str | None = None,
                     limit: int = 50, cursor: str | None = None) -> list[Call]:
    q = select(Call).order_by(Call.created_at.desc(), Call.call_id.desc()).limit(limit)
    if status:
        q = q.where(Call.status == status)
    if state:
        q = q.where(Call.state == state)
    if cursor:
        ts, cid = _parse_list_cursor(cursor)
        q = q.where(or_(Call.created_at < ts, and_(Call.created_at == ts, Call.call_id < cid)))
    return list((await s.execute(q)).scalars().all())


async def add_event(s: AsyncSession, call_id: str, event: str, detail: dict[str, Any] | None = None,
                    audio_id: int | None = None) -> None:
    s.add(JobEvent(call_id=call_id, audio_id=audio_id, event=event, detail_json=detail))


async def schedule_generation_if_ready(s: AsyncSession, call_id: str) -> bool:
    """state==ENDED && 진행중 오디오 없음 && gen_state==IDLE → QUEUED. 원자적 UPDATE(rowcount 가드)."""
    active = select(func.count()).select_from(CallAudio).where(
        CallAudio.call_id == call_id, CallAudio.status.in_(ACTIVE_AUDIO)
    ).scalar_subquery()
    res = await s.execute(
        update(Call)
        .where(Call.call_id == call_id, Call.state == "ENDED", Call.gen_state == "IDLE", active == 0)
        .values(gen_state="QUEUED", gen_next_attempt_at=None, updated_at=utcnow())
    )
    return (res.rowcount or 0) > 0


async def claim_generation(s: AsyncSession, limit: int) -> list[str]:
    now = utcnow()
    q = (
        select(Call.call_id)
        .where(Call.gen_state == "QUEUED",
               or_(Call.gen_next_attempt_at.is_(None), Call.gen_next_attempt_at <= now))
        .order_by(Call.updated_at.asc())
        .limit(limit)
    )
    ids = list((await s.execute(q)).scalars().all())
    claimed: list[str] = []
    for cid in ids:
        res = await s.execute(
            update(Call).where(Call.call_id == cid, Call.gen_state == "QUEUED")
            .values(gen_state="RUNNING", generation_started_at=now, updated_at=now)
        )
        if res.rowcount:
            claimed.append(cid)
    return claimed


async def count_pending(s: AsyncSession) -> dict[str, int]:
    stt = (await s.execute(select(func.count()).select_from(CallAudio)
                           .where(CallAudio.status.in_(ACTIVE_AUDIO)))).scalar_one()
    gen = (await s.execute(select(func.count()).select_from(Call)
                           .where(Call.gen_state.in_(("QUEUED", "RUNNING"))))).scalar_one()
    daily = (await s.execute(select(func.count()).select_from(DailyDiary)
                             .where(DailyDiary.gen_state.in_(("QUEUED", "RUNNING"))))).scalar_one()
    return {"pending_stt": int(stt), "pending_gen": int(gen), "pending_daily": int(daily)}


async def get_calls_by_ids(s: AsyncSession, call_ids: Sequence[str]) -> list[Call]:
    q = select(Call).where(Call.call_id.in_(list(call_ids)))
    return list((await s.execute(q)).scalars().all())


# --- audio -----------------------------------------------------------------

async def get_audio(s: AsyncSession, audio_id: int) -> CallAudio | None:
    return await s.get(CallAudio, audio_id)


async def find_audio(s: AsyncSession, call_id: str, bucket: str, key: str) -> CallAudio | None:
    q = select(CallAudio).where(CallAudio.call_id == call_id, CallAudio.bucket == bucket, CallAudio.key == key)
    return (await s.execute(q)).scalar_one_or_none()


async def list_audio(s: AsyncSession, call_id: str) -> list[CallAudio]:
    q = select(CallAudio).where(CallAudio.call_id == call_id).order_by(CallAudio.id)
    return list((await s.execute(q)).scalars().all())


def order_audio(rows: Sequence[CallAudio]) -> list[CallAudio]:
    """(seq NULLS LAST, recorded_at NULLS LAST, id) — 병합 순서."""
    def k(a: CallAudio):  # type: ignore[no-untyped-def]
        return (
            a.seq is None, a.seq if a.seq is not None else 0,
            a.recorded_at is None, a.recorded_at.timestamp() if a.recorded_at else 0.0,
            a.id,
        )
    return sorted(rows, key=k)


async def claim_audio(s: AsyncSession, limit: int) -> list[int]:
    now = utcnow()
    q = (
        select(CallAudio.id)
        .where(CallAudio.status == "PENDING", CallAudio.next_attempt_at <= now)
        .order_by(CallAudio.call_id, CallAudio.seq.is_(None), CallAudio.seq,
                  CallAudio.recorded_at.is_(None), CallAudio.recorded_at, CallAudio.id)
        .limit(limit)
    )
    ids = list((await s.execute(q)).scalars().all())
    claimed: list[int] = []
    for aid in ids:
        res = await s.execute(
            update(CallAudio).where(CallAudio.id == aid, CallAudio.status == "PENDING")
            .values(status="TRANSCRIBING", claimed_at=now, updated_at=now)
        )
        if res.rowcount:
            claimed.append(aid)
    return claimed


async def audio_progress(s: AsyncSession, call_id: str) -> dict[str, int]:
    q = select(CallAudio.status, func.count()).where(CallAudio.call_id == call_id).group_by(CallAudio.status)
    rows = (await s.execute(q)).all()
    by = {st: int(n) for st, n in rows}
    total = sum(by.values())
    return {
        "total": total,
        "transcribed": by.get("TRANSCRIBED", 0),
        "failed": by.get("FAILED", 0),
        "pending": by.get("PENDING", 0) + by.get("TRANSCRIBING", 0),
    }


# --- artifacts -------------------------------------------------------------

async def replace_artifacts(s: AsyncSession, call_id: str, items: list[Artifact]) -> None:
    await s.execute(delete(Artifact).where(Artifact.call_id == call_id))
    for a in items:
        s.add(a)


async def list_artifacts(s: AsyncSession, call_id: str) -> list[Artifact]:
    q = select(Artifact).where(Artifact.call_id == call_id).order_by(Artifact.id)
    return list((await s.execute(q)).scalars().all())


async def get_artifact(s: AsyncSession, call_id: str, kind: str, prdlst_code: str = "") -> Artifact | None:
    q = select(Artifact).where(Artifact.call_id == call_id, Artifact.kind == kind, Artifact.prdlst_code == prdlst_code)
    return (await s.execute(q)).scalar_one_or_none()


# --- daily diaries ---------------------------------------------------------

async def get_daily(s: AsyncSession, diary_id: str) -> DailyDiary | None:
    return await s.get(DailyDiary, diary_id)


async def list_daily(s: AsyncSession, *, diary_date: str | None = None, status: str | None = None,
                     limit: int = 50, cursor: str | None = None) -> list[DailyDiary]:
    q = select(DailyDiary).order_by(DailyDiary.created_at.desc(), DailyDiary.diary_id.desc()).limit(limit)
    if diary_date:
        q = q.where(DailyDiary.diary_date == diary_date)
    if status:
        q = q.where(DailyDiary.status == status)
    if cursor:
        ts, did = _parse_list_cursor(cursor)
        q = q.where(or_(DailyDiary.created_at < ts, and_(DailyDiary.created_at == ts, DailyDiary.diary_id < did)))
    return list((await s.execute(q)).scalars().all())


async def claim_daily_generation(s: AsyncSession, limit: int) -> list[str]:
    """claim_generation 미러 — daily_diaries.gen_state QUEUED → RUNNING."""
    now = utcnow()
    q = (
        select(DailyDiary.diary_id)
        .where(DailyDiary.gen_state == "QUEUED",
               or_(DailyDiary.gen_next_attempt_at.is_(None), DailyDiary.gen_next_attempt_at <= now))
        .order_by(DailyDiary.updated_at.asc())
        .limit(limit)
    )
    ids = list((await s.execute(q)).scalars().all())
    claimed: list[str] = []
    for did in ids:
        res = await s.execute(
            update(DailyDiary).where(DailyDiary.diary_id == did, DailyDiary.gen_state == "QUEUED")
            .values(gen_state="RUNNING", generation_started_at=now, updated_at=now)
        )
        if res.rowcount:
            claimed.append(did)
    return claimed


async def replace_daily_artifacts(s: AsyncSession, diary_id: str, items: list[DailyArtifact]) -> None:
    await s.execute(delete(DailyArtifact).where(DailyArtifact.diary_id == diary_id))
    for a in items:
        s.add(a)


async def list_daily_artifacts(s: AsyncSession, diary_id: str) -> list[DailyArtifact]:
    q = select(DailyArtifact).where(DailyArtifact.diary_id == diary_id).order_by(DailyArtifact.id)
    return list((await s.execute(q)).scalars().all())


async def get_daily_artifact(s: AsyncSession, diary_id: str, kind: str, prdlst_code: str = "") -> DailyArtifact | None:
    q = select(DailyArtifact).where(DailyArtifact.diary_id == diary_id, DailyArtifact.kind == kind,
                                    DailyArtifact.prdlst_code == prdlst_code)
    return (await s.execute(q)).scalar_one_or_none()


# --- recovery / sweeps -----------------------------------------------------

async def recover_inflight(s: AsyncSession) -> dict[str, int]:
    now = utcnow()
    r1 = await s.execute(update(CallAudio).where(CallAudio.status == "TRANSCRIBING")
                         .values(status="PENDING", next_attempt_at=now, claimed_at=None, updated_at=now))
    r2 = await s.execute(update(Call).where(Call.gen_state == "RUNNING")
                         .values(gen_state="QUEUED", gen_next_attempt_at=None, updated_at=now))
    r3 = await s.execute(update(DailyDiary).where(DailyDiary.gen_state == "RUNNING")
                         .values(gen_state="QUEUED", gen_next_attempt_at=None, updated_at=now))
    return {"audio_reset": r1.rowcount or 0, "gen_reset": r2.rowcount or 0, "daily_reset": r3.rowcount or 0}


async def ended_call_ids(s: AsyncSession) -> list[str]:
    q = select(Call.call_id).where(Call.state == "ENDED", Call.status == "PROCESSING")
    return list((await s.execute(q)).scalars().all())


async def sweep_stt_deadline(s: AsyncSession, deadline_sec: int) -> list[str]:
    """ENDED 후 deadline 초 넘도록 미종료 오디오 → FAILED(STT_TIMEOUT). 영향받은 call_id 반환."""
    cutoff = utcnow() - timedelta(seconds=deadline_sec)
    q = (select(CallAudio.id, CallAudio.call_id)
         .join(Call, Call.call_id == CallAudio.call_id)
         .where(Call.state == "ENDED", Call.ended_at.is_not(None), Call.ended_at <= cutoff,
                CallAudio.status.in_(ACTIVE_AUDIO)))
    rows = (await s.execute(q)).all()
    ids = [r[0] for r in rows]
    if ids:
        await s.execute(update(CallAudio).where(CallAudio.id.in_(ids))
                        .values(status="FAILED", error_permanent=True, last_error="STT_TIMEOUT: deadline exceeded",
                                updated_at=utcnow()))
    return sorted({r[1] for r in rows})
