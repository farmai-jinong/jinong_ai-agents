"""헬스 — /healthz(무인증, 업스트림 호출 없음) · /v1/upstream/health(인증, STT/LLM/S3/farmos 프로브)."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Depends, Request

from .. import __version__
from ..auth import require_api_key
from ..clients.llm import probe_llm
from ..db import repo
from ..runtime import Runtime

router = APIRouter(tags=["health"])


def _rt(request: Request) -> Runtime:
    return request.app.state.rt


@router.get("/healthz")
async def healthz(request: Request) -> dict:
    rt = _rt(request)
    pend = {"pending_stt": None, "pending_gen": None, "pending_daily": None}
    db_ok = await rt.db.ping()
    if db_ok:
        async with rt.db.session() as s:
            pend = await repo.count_pending(s)
    return {"status": "ok" if db_ok else "degraded", "version": __version__,
            "worker": {"running": bool(rt.worker and rt.worker.running), **pend}}


@router.get("/v1/upstream/health", dependencies=[Depends(require_api_key)])
async def upstream_health(request: Request) -> dict:
    rt = _rt(request)
    st = rt.settings

    async def farmos() -> dict:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(st.farmos_base_url.rstrip("/") + "/m/diary/user/prdlsts/list")
            return {"ok": r.status_code < 500, "status": r.status_code}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    async def s3() -> dict:
        return {"ok": await rt.s3.head_bucket(), "bucket": rt.s3.bucket}

    stt_r, llm_r, s3_r, fo_r = await asyncio.gather(rt.stt.probe(), probe_llm(st), s3(), farmos())
    return {"stt": {**stt_r, "url": st.stt_base_url}, "llm": {**llm_r, "model": st.llm_model, "provider": st.llm_provider},
            "s3": s3_r, "farmos": {**fo_r, "url": st.farmos_base_url}, "pipeline": st.pipeline_impl}
