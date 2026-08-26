"""콜백 — terminal 상태에서 백엔드에 결과 전송/알림. 실패해도 통화 상태에 영향 없음.

- 통화 단위: 통화요약 콜백(`.../voicetalk/public/call-summary-callback`), 일지 마크다운 본문 동봉.
- 날짜별 일지: agent-callback(마스터 ID 알림), 요청 body 의 `callback_url`.
"""

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
    """(성공여부, 시도횟수). 최대 3회 시도, 실패 시 10s·30s 대기 후 재시도(마지막 delay 는 미사용).

    4xx(429 제외)는 요청/설정을 고치기 전에는 다시 보내도 같은 결과이므로 재시도하지 않는다
    (백엔드 명세 권고: 400/401/404 반복 재시도 금지).
    """
    headers = {"Content-Type": "application/json"}
    if settings.callback_api_key:
        headers["X-API-Key"] = settings.callback_api_key   # 값은 로그에 남기지 않는다
    attempts = 0
    async with httpx.AsyncClient(timeout=settings.callback_timeout) as c:
        for i in range(len(delays)):
            attempts += 1
            try:
                r = await c.post(url, json=payload, headers=headers)
                if r.status_code < 300:
                    return True, attempts
                log.warning("callback %s -> %s: %s (attempt %d)", url, r.status_code, r.text[:200], attempts)
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    return False, attempts
            except Exception as e:  # noqa: BLE001
                log.warning("callback %s failed: %s (attempt %d)", url, e, attempts)
            if i < len(delays) - 1:
                await asyncio.sleep(delays[i])
    return False, attempts
