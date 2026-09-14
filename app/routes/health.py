"""헬스 — /healthz(무인증, 업스트림 호출 없음) · /v1/upstream/health(인증, STT/LLM/S3/farmos 프로브 + 비밀 아닌 유효 설정).

`commit`/`config` 는 tests/smoke 가 배포본 일치·환경 드리프트(dev/prod .env)를 판정하는 근거다 — 비밀값은 절대 넣지 않는다."""

from __future__ import annotations

import asyncio
import os

import httpx
from fastapi import APIRouter, Depends, Request

from .. import __version__
from ..auth import require_api_key
from ..clients.ap_backend import ApBackendClient
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
            "commit": os.environ.get("GIT_SHA", "unknown"),
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

    async def ap_backend() -> dict | None:
        if not (st.ap_backend_base_url and st.callback_api_key):
            return None
        async with ApBackendClient(st.ap_backend_base_url, st.callback_api_key,
                                   timeout=st.ap_backend_timeout) as c:
            return await c.probe()

    stt_r, llm_r, s3_r, fo_r, ap_r = await asyncio.gather(
        rt.stt.probe(), probe_llm(st), s3(), farmos(), ap_backend())
    out = {"stt": {**stt_r, "url": st.stt_base_url},
           "llm": {**llm_r, "model": st.llm_model, "provider": st.llm_provider},
           "s3": s3_r, "farmos": {**fo_r, "url": st.farmos_base_url}, "pipeline": st.pipeline_impl,
           "config": effective_config(st)}
    if ap_r is not None:
        out["ap_backend"] = ap_r
    return out


def effective_config(st) -> dict:
    """비밀 아닌 유효 설정 — 값이 아니라 '설정됐는지' 만 내는 항목(summary_callback_set)에 주의. 키·토큰·자격증명 경로는 금지."""
    return {
        "api_markdown_view": st.api_markdown_view,
        "s3_prefix": st.s3_prefix,
        "storage_impl": st.storage_impl,
        "public_base_url": st.public_base_url,
        "callback_enabled": st.callback_enabled,
        "summary_callback_set": bool(st.summary_callback_url),
        "call_agent_callback_enabled": st.call_agent_callback_enabled,
        "callback_include_artifact_keys": st.callback_include_artifact_keys,
        "term_fix_enabled": st.term_fix_enabled,
        "verify_diary_enabled": st.verify_diary_enabled,
    }
