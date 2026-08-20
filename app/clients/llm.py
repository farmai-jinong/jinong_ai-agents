"""LLM 클라이언트 팩토리 — provider 별 LangChain 채팅 모델.

- openai | jinong : OpenAI 호환 하나(ChatOpenAI). 전환은 env 3개(LLM_BASE_URL / LLM_API_KEY / LLM_MODEL).
  jinong 은 EXAONE 계열이 CoT 를 내뱉지 않도록 `chat_template_kwargs.enable_thinking=false` 를 extra_body 로 넣는다(config 기본값).
- gemini          : Vertex AI 백엔드(langchain-google-genai `ChatGoogleGenerativeAI(vertexai=True)`). 인증은 서비스 계정
  키(`GOOGLE_APPLICATION_CREDENTIALS` → ADC, google-auth 가 직접 읽음), 프로젝트/리전은 GCP_PROJECT_ID / GCP_LOCATION(기본 global).
  Hatchery_serving `pipeline_llm.py`(ChatVertexAI, 같은 SA 키·project·location=global) 와 같은 인증 방식 — ChatVertexAI 는
  langchain-google-vertexai 3.2 부터 deprecated 라 후속 패키지를 쓴다.

OpenAI/Gemini 는 모두 외부 처리(동의서 §7) — 최종 목표는 jinong(게이트웨이 vLLM) 전환.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from ..config import Settings

VERTEX_MODEL_URL = "https://aiplatform.googleapis.com/v1beta1/publishers/google/models/{model}"


def is_gemini(llm: Any) -> bool:
    """Gemini 챗 모델 인스턴스인지(임포트 없이 클래스명으로)."""
    return any(c.__name__ in ("ChatGoogleGenerativeAI", "ChatVertexAI") for c in type(llm).__mro__)


def make_chat_model(settings: Settings, *, max_tokens: int | None = None, **overrides: Any):
    if settings.llm_provider == "gemini":
        return _make_vertex(settings, max_tokens=max_tokens, **overrides)
    return _make_openai(settings, max_tokens=max_tokens, **overrides)


def _make_openai(settings: Settings, *, max_tokens: int | None, **overrides: Any):
    from langchain_openai import ChatOpenAI  # 지연 임포트 (테스트/헬스에서 불필요)

    kwargs: dict[str, Any] = dict(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "sk-local",
        temperature=settings.llm_temperature,
        seed=settings.llm_seed,
        timeout=settings.llm_timeout,
        max_retries=settings.llm_max_retries,
    )
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    extra = settings.llm_extra_body_dict
    if extra:
        kwargs["extra_body"] = extra
    kwargs.update(overrides)
    return ChatOpenAI(**kwargs)


def _export_google_credentials(settings: Settings) -> None:
    """`.env` 로만 준 SA 키 경로를 ADC 가 찾도록 프로세스 환경에 넣는다(이미 있으면 유지)."""
    if settings.google_application_credentials and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.google_application_credentials


def _make_vertex(settings: Settings, *, max_tokens: int | None, **overrides: Any):
    from langchain_google_genai import ChatGoogleGenerativeAI  # 지연 임포트

    _export_google_credentials(settings)
    kwargs: dict[str, Any] = dict(
        model=settings.llm_model,
        vertexai=True,                                   # Developer API(API 키) 가 아니라 Vertex(ADC/SA 키)
        project=settings.gcp_project_id or None,         # None 이면 GOOGLE_CLOUD_PROJECT → SA 키의 프로젝트
        location=settings.gcp_location or "global",
        temperature=settings.llm_temperature,
        seed=settings.llm_seed,
        timeout=settings.llm_timeout,
        max_retries=settings.llm_max_retries,
    )
    if max_tokens:
        kwargs["max_output_tokens"] = max_tokens
    if settings.gemini_thinking_level:
        kwargs["thinking_level"] = settings.gemini_thinking_level
    kwargs.update(overrides)
    return ChatGoogleGenerativeAI(**kwargs)


async def probe_llm(settings: Settings, timeout: float = 5.0) -> dict[str, Any]:
    """LLM 도달성 (헬스). openai/jinong: `GET {base_url}/models`. gemini: SA 토큰 발급 + publisher model 조회."""
    if settings.llm_provider == "gemini":
        return await asyncio.to_thread(_probe_vertex, settings, timeout)
    import httpx

    url = settings.llm_base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {settings.llm_api_key or 'sk-local'}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url, headers=headers)
        return {"ok": r.status_code < 400, "status": r.status_code, "url": url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "url": url}


def _probe_vertex(settings: Settings, timeout: float) -> dict[str, Any]:
    url = VERTEX_MODEL_URL.format(model=settings.llm_model)
    out: dict[str, Any] = {"url": url, "project": settings.gcp_project_id or None, "location": settings.gcp_location}
    _export_google_credentials(settings)
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path and not os.path.exists(cred_path):
        return {**out, "ok": False, "error": f"GOOGLE_APPLICATION_CREDENTIALS not found: {cred_path}"}
    try:
        import google.auth
        import google.auth.transport.requests
        import httpx

        creds, sa_project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        out["project"] = out["project"] or sa_project
        r = httpx.get(url, headers={"Authorization": f"Bearer {creds.token}"}, timeout=timeout)
        return {**out, "ok": r.status_code < 400, "status": r.status_code}
    except Exception as e:  # noqa: BLE001
        return {**out, "ok": False, "error": str(e)}
