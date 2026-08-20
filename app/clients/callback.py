"""선택 콜백 — terminal 상태에서 호출자에게 알림. 실패해도 통화 상태에 영향 없음."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

RETRY_DELAYS = (10.0, 30.0, 90.0)


async def send_callback(settings: Settings, url: str, payload: dict[str, Any], *,
                        delays: tuple[float, ...] = RETRY_DELAYS) -> tuple[bool, int]:
    """(성공여부, 시도횟수). 3회 시도(10/30/90s)."""
    headers = {"Content-Type": "application/json"}
    if settings.callback_api_key:
        headers["X-API-Key"] = settings.callback_api_key
    attempts = 0
    async with httpx.AsyncClient(timeout=settings.callback_timeout) as c:
        for i in range(len(delays)):
            attempts += 1
            try:
                r = await c.post(url, json=payload, headers=headers)
                if r.status_code < 300:
                    return True, attempts
                log.warning("callback %s -> %s (attempt %d)", url, r.status_code, attempts)
            except Exception as e:  # noqa: BLE001
                log.warning("callback %s failed: %s (attempt %d)", url, e, attempts)
            if i < len(delays) - 1:
                await asyncio.sleep(delays[i])
    return False, attempts
