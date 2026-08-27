"""AP 백엔드 research API **읽기 전용** 클라이언트 — 농가 JWT 없이 작물·표준 품목 코드를 조회한다.

계약 SSOT: 백엔드 전달 문서 "AI 영농일지 연동 변경사항" §3·§4.
인증은 콜백과 같은 방향의 키 `X-API-Key: CALLBACK_API_KEY`(= `VOICETALK_EXTERNAL_CALLBACK_API_KEY`).

이 클라이언트로 얻는 것은 **작물 목록과 품목 코드**뿐이다. 앱 저장용 초안(`prefill`)에 필요한
방제대상·약제·농작업 팔레트·기존 일지는 farmos `/m/diary/*` 에만 있고 농가 JWT 를 요구한다 —
따라서 토큰 없이 이 경로로 생성한 일지는 `PARTIAL`(prefill 없음)로 남는다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class ApBackendError(Exception):
    def __init__(self, message: str, status: int | None = None, path: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.path = path


class ApBackendAuthError(ApBackendError):
    """401/403 — API 키 무효. 파이프라인은 warning 으로 강등."""


class ApBackendClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 10.0,
                 client: httpx.AsyncClient | None = None, retries: int = 2) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self._client = client
        self._own = client is None

    async def __aenter__(self) -> "ApBackendClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}   # 값은 로그에 남기지 않는다
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = await self._c().get(url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = e
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code in (401, 403):
                raise ApBackendAuthError(f"ap-backend auth failed ({resp.status_code})", resp.status_code, path)
            if resp.status_code == 404:
                return None
            if resp.status_code >= 500:
                last = ApBackendError(f"ap-backend {resp.status_code}: {resp.text[:200]}", resp.status_code, path)
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise ApBackendError(f"ap-backend {resp.status_code}: {resp.text[:200]}", resp.status_code, path)
            try:
                return resp.json()
            except ValueError as e:
                raise ApBackendError(f"ap-backend non-json response: {resp.text[:200]}", resp.status_code, path) from e
        assert last is not None
        raise ApBackendError(f"ap-backend unreachable: {last}", None, path) from last

    # --- §3 농가 등록 품목 -------------------------------------------------
    async def farm_context(self, engn_id: str, user_id: str) -> list[dict[str, Any]]:
        """`GET /voicetalk/public/research/farm-context` → farmos `list_crops()` 와 같은 모양(camelCase)으로 환산.

        같은 품목이 여러 필지에 있어도 백엔드가 `prdlst_code` 별 1건만 준다.
        `representative` 는 farmos 의 `reprsntPrdlstCnt == 1` 과 같은 뜻이라 그 눈금으로 옮긴다.
        """
        body = await self._get("/voicetalk/public/research/farm-context",
                               {"farmer_engn_id": engn_id, "farmer_user_id": user_id})
        rows = (body or {}).get("crops") or []
        out: list[dict[str, Any]] = []
        for r in rows:
            nm = r.get("prdlst_nm")
            if not nm:
                continue
            out.append({"prdlstCode": r.get("prdlst_code"), "prdlstNm": str(nm),
                        "reprsntPrdlstCnt": 1 if r.get("representative") else None})
        return out

    # --- §4 표준 품목 코드 -------------------------------------------------
    async def prdlst(self, prdlst_code: str) -> dict[str, Any] | None:
        """`GET /voicetalk/public/research/prdlsts/{code}` — 없으면 None."""
        return await self._get(f"/voicetalk/public/research/prdlsts/{prdlst_code}")

    async def probe(self, timeout: float = 5.0) -> dict[str, Any]:
        try:
            r = await self._c().get(f"{self.base_url}/voicetalk/public/research/prdlsts",
                                    params={"group_type": "M", "use_yn": "Y"},
                                    headers={"X-API-Key": self.api_key}, timeout=timeout)
            return {"ok": r.status_code < 400, "status": r.status_code, "url": self.base_url}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), "url": self.base_url}
