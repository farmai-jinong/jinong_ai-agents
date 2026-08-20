"""⑥ 게이트웨이 STT 클라이언트 — `POST /v1/audio/transcriptions` (diarize=true, 동기).

계약: jinong_ai-gateway/docs/api-reference.md. 응답은 **최상위 배열**(chunk) — 현재 항상 1개.
재시도 분류(audio_labeler qwen-gateway.adapter.ts 교훈):
  429 → Retry-After(기본 30s) · 502/503/504/timeout/transport → 지수 백오프 · 413/415/기타 4xx → 영구 실패.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings


class SttError(Exception):
    def __init__(self, message: str, *, permanent: bool, retry_after: float | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after
        self.status = status


@dataclass
class SttResult:
    text: str
    segments: list[dict[str, Any]]           # {speaker, start, end, text, id}
    seconds: float | None                    # usage.seconds
    source_end_sec: float | None
    raw: Any = field(repr=False, default=None)


def backoff_delay(attempt: int, *, base: float = 15.0, cap: float = 300.0) -> float:
    """attempt 는 0부터. min(cap, base·2^attempt) + jitter(0~20%)."""
    d = min(cap, base * (2 ** max(0, attempt)))
    return d + random.uniform(0, d * 0.2)


def parse_diarized(body: Any) -> SttResult:
    if not isinstance(body, list):
        raise SttError(f"unexpected STT response type: {type(body).__name__}", permanent=True)
    if len(body) != 1:
        # 미문서화 다중 chunk — 조용한 타임라인 손상 대신 시끄럽게 실패
        raise SttError(f"STT_MULTI_CHUNK: expected 1 chunk, got {len(body)}", permanent=True)
    chunk = body[0] or {}
    resp = chunk.get("response") or {}
    segs: list[dict[str, Any]] = []
    for s in resp.get("segments") or []:
        text = s.get("text") or ""
        if text.startswith(" "):
            text = text[1:]
        segs.append({
            "id": s.get("id"), "speaker": s.get("speaker") or "?",
            "start": float(s.get("start") or 0.0), "end": float(s.get("end") or 0.0),
            "text": text,
        })
    usage = resp.get("usage") or {}
    seconds = usage.get("seconds")
    return SttResult(
        text=(resp.get("text") or "").strip(),
        segments=segs,
        seconds=float(seconds) if seconds is not None else None,
        source_end_sec=float(chunk["source_end_sec"]) if chunk.get("source_end_sec") is not None else None,
        raw=body,
    )


class SttClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = settings.stt_base_url.rstrip("/")
        self.api_key = settings.stt_api_key
        self.timeout = httpx.Timeout(settings.stt_timeout, connect=settings.stt_connect_timeout)
        self._client = client

    async def startup(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def diarize(self, audio: bytes, filename: str, num_speakers: int | None = None) -> SttResult:
        data: dict[str, str] = {"diarize": "true", "stream": "false"}
        if num_speakers:
            data["num_speakers"] = str(num_speakers)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            resp = await self._c().post(
                f"{self.base_url}/v1/audio/transcriptions",
                data=data,
                files={"file": (filename, audio, "application/octet-stream")},
                headers=headers,
            )
        except httpx.TimeoutException as e:
            raise SttError(f"STT timeout: {e}", permanent=False, status=408) from e
        except httpx.TransportError as e:
            raise SttError(f"STT transport error: {e}", permanent=False) from e

        st = resp.status_code
        if st == 429:
            ra = resp.headers.get("Retry-After")
            try:
                retry_after = float(ra) if ra else 30.0
            except ValueError:
                retry_after = 30.0
            raise SttError("STT 429 too many requests", permanent=False, retry_after=retry_after, status=429)
        if st in (502, 503, 504):
            raise SttError(f"STT upstream {st}: {resp.text[:300]}", permanent=False, status=st)
        if st >= 500:
            raise SttError(f"STT {st}: {resp.text[:300]}", permanent=False, status=st)
        if st >= 400:
            raise SttError(f"STT {st}: {resp.text[:300]}", permanent=True, status=st)
        try:
            body = resp.json()
        except ValueError as e:
            raise SttError(f"STT non-JSON response: {resp.text[:200]}", permanent=True, status=st) from e
        return parse_diarized(body)

    async def probe(self, timeout: float = 5.0) -> dict[str, Any]:
        try:
            r = await self._c().get(f"{self.base_url}/healthz", timeout=timeout)
            return {"ok": r.status_code == 200, "status": r.status_code}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
