"""Bearer API-key 인증 — jinong_ai-gateway `app/auth.py` 이식.

`AGENT_API_KEY` 콤마 구분 다중 키(클라이언트별 발급/회수). fail-closed: 키가 비어 있으면
`ALLOW_NO_AUTH=1` 이 아닌 한 기동 거부(app.main).
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request, status

from .config import Settings, get_settings


def _settings_of(request: Request | None) -> Settings:
    # 앱에 주입된 settings 우선(테스트에서 앱마다 다른 키), 없으면 전역
    rt = getattr(getattr(request, "app", None), "state", None)
    rt = getattr(rt, "rt", None) if rt is not None else None
    return rt.settings if rt is not None else get_settings()


def _valid(token: str, settings: Settings) -> bool:
    return any(secrets.compare_digest(token, k) for k in settings.api_keys)


def _extract(authorization: str | None, x_api_key: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return x_api_key


async def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    settings = _settings_of(request)
    if not settings.auth_enabled:
        return
    token = _extract(authorization, x_api_key)
    if not token or not _valid(token, settings):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
