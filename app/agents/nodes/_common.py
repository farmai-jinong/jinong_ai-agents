"""노드 공용 유틸."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from datetime import datetime
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from ...schemas.pipeline import CallContext
from ..deps import Deps
from ..schemas import PipelineError
from ..tools.transcript import ROLE_KO

log = logging.getLogger(__name__)
T = TypeVar("T")
KST = ZoneInfo("Asia/Seoul")


def participants_view(ctx: CallContext) -> list[dict[str, Any]]:
    return [{"role": p.role, "role_ko": ROLE_KO.get(p.role, p.role), "name": p.name, "user_id": p.user_id}
            for p in ctx.participants]


def call_when_text(ctx: CallContext) -> str:
    def f(dt: datetime | None) -> str:
        if not dt:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    a, b = f(ctx.started_at), f(ctx.ended_at)
    return f"{a} ~ {b[-5:]}" if a and b else (a or b or "불명")


def diary_date_for(ctx: CallContext) -> str:
    if ctx.hints.diary_date:
        return ctx.hints.diary_date
    dt = ctx.started_at or ctx.ended_at
    if not dt:
        return datetime.now(KST).date().isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST).date().isoformat()


async def with_timeout(coro: Awaitable[T], deps: Deps, name: str) -> T:
    return await asyncio.wait_for(coro, deps.settings.node_timeout_s)


def err(node: str, e: Exception | str, fatal: bool = False) -> PipelineError:
    msg = str(e) if isinstance(e, Exception) else e
    return PipelineError(node=node, message=f"{type(e).__name__}: {msg}"[:500] if isinstance(e, Exception) else msg[:500], fatal=fatal)
