"""단일 프로세스 워커 — DB 폴링 + asyncio.Event wake, 세마포어(STT ≤ N, GEN ≤ M).

라우트가 작업을 만들면 `worker.wake()` 를 호출한다. 테스트는 `run_once()` 로 결정적으로 돌린다.
"""

from __future__ import annotations

import asyncio
import logging

from ..db import repo
from ..runtime import Runtime
from .generate_job import run_generate
from .recovery import recover, sweep
from .stt_job import run_stt

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()
        self._stt_sem = asyncio.Semaphore(max(1, rt.settings.stt_max_concurrency))
        self._gen_sem = asyncio.Semaphore(max(1, rt.settings.gen_max_concurrency))
        self._sweep_counter = 0
        self.running = False

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        await recover(self.rt)
        self._stop.clear()
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="agent-worker")

    async def stop(self) -> None:
        self.running = False
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        if self._inflight:
            done, pending = await asyncio.wait(self._inflight, timeout=self.rt.settings.shutdown_grace_sec)
            for t in pending:
                t.cancel()
        self._task = None

    def wake(self) -> None:
        self._wake.set()

    # --- loop -------------------------------------------------------------
    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("worker tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.rt.settings.worker_poll_sec)
            except TimeoutError:
                pass

    async def run_once(self) -> dict[str, int]:
        """대기 중인 STT/생성 작업을 클레임해 태스크로 띄운다 (반환: 이번 틱에 띄운 개수)."""
        self._sweep_counter += 1
        if self._sweep_counter % 12 == 0:   # 폴링 12회마다(≈1분) deadline 스윕
            await sweep(self.rt)
        stt_room = self._stt_sem._value  # noqa: SLF001 — 남은 슬롯만큼만 클레임
        gen_room = self._gen_sem._value  # noqa: SLF001
        launched = {"stt": 0, "gen": 0}
        async with self.rt.db.session() as s:
            audio_ids = await repo.claim_audio(s, stt_room) if stt_room > 0 else []
            call_ids = await repo.claim_generation(s, gen_room) if gen_room > 0 else []
            await s.commit()
        for aid in audio_ids:
            self._spawn(self._stt(aid))
            launched["stt"] += 1
        for cid in call_ids:
            self._spawn(self._gen(cid))
            launched["gen"] += 1
        return launched

    async def drain(self, timeout: float = 60.0) -> None:
        """테스트/스크립트용: 큐가 빌 때까지 run_once 반복 + 태스크 완료 대기."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await self.run_once()
            if self._inflight:
                await asyncio.wait(self._inflight, timeout=max(0.1, deadline - loop.time()))
                continue
            async with self.rt.db.session() as s:
                pend = await repo.count_pending(s)
            if pend["pending_stt"] == 0 and pend["pending_gen"] == 0:
                return
            await asyncio.sleep(0.05)

    def _spawn(self, coro) -> None:  # type: ignore[no-untyped-def]
        t = asyncio.create_task(coro)
        self._inflight.add(t)
        t.add_done_callback(self._inflight.discard)

    async def _stt(self, audio_id: int) -> None:
        async with self._stt_sem:
            try:
                queued = await run_stt(self.rt, audio_id)
            except Exception:  # noqa: BLE001
                log.exception("stt job crashed audio#%d", audio_id)
                queued = False
        if queued:
            self.wake()

    async def _gen(self, call_id: str) -> None:
        async with self._gen_sem:
            try:
                await run_generate(self.rt, call_id)
            except Exception as e:  # noqa: BLE001 — run_generate 밖의 예외(DB 등): 재큐 또는 FAILED
                log.exception("[%s] generate job crashed", call_id)
                await self._requeue_or_fail(call_id, f"{type(e).__name__}: {e}")

    async def _requeue_or_fail(self, call_id: str, msg: str) -> None:
        from datetime import timedelta

        from ..db.models import utcnow

        try:
            async with self.rt.db.session() as s:
                call = await repo.get_call(s, call_id)
                if call is None or call.gen_state != "RUNNING":
                    return
                if call.generation_attempts < self.rt.settings.gen_max_attempts:
                    call.gen_state = "QUEUED"
                    call.gen_next_attempt_at = utcnow() + timedelta(seconds=60)
                else:
                    call.gen_state = "IDLE"
                    call.status = "FAILED"
                    call.error_code, call.error_message = "GENERATION_FAILED", msg[:1000]
                    call.generation_finished_at = utcnow()
                await s.commit()
        except Exception:  # noqa: BLE001
            log.exception("[%s] requeue_or_fail failed", call_id)
