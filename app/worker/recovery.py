"""기동 시 복구 — 진행중 상태 되돌리기 + ENDED 통화 생성 재평가."""

from __future__ import annotations

import logging

from ..db import repo
from ..runtime import Runtime

log = logging.getLogger(__name__)


async def recover(rt: Runtime) -> dict[str, int]:
    async with rt.db.session() as s:
        stats = await repo.recover_inflight(s)
        await s.commit()
        requeued = 0
        for cid in await repo.ended_call_ids(s):
            if await repo.schedule_generation_if_ready(s, cid):
                requeued += 1
        await s.commit()
    stats["gen_requeued"] = requeued
    if any(stats.values()):
        log.info("recovery: %s", stats)
    return stats


async def sweep(rt: Runtime) -> list[str]:
    """ENDED 후 deadline 초과 오디오 → FAILED, 해당 통화 생성 스케줄."""
    async with rt.db.session() as s:
        affected = await repo.sweep_stt_deadline(s, rt.settings.end_stt_deadline_sec)
        for cid in affected:
            await repo.add_event(s, cid, "stt_deadline_sweep")
            await repo.schedule_generation_if_ready(s, cid)
        await s.commit()
    if affected:
        log.warning("stt deadline sweep hit calls: %s", affected)
    return affected
