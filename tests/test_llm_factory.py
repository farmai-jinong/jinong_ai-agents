"""LLM provider 설정/팩토리 — openai|jinong|gemini 분기 (네트워크 없음)."""

import pytest

from app.clients.llm import is_gemini, make_chat_model
from app.config import Settings


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, agent_api_key="k", **kw)


def test_provider_validator():
    assert _settings(llm_provider="GEMINI").llm_provider == "gemini"
    with pytest.raises(ValueError):
        _settings(llm_provider="anthropic")


def test_extra_body_per_provider():
    assert _settings(llm_provider="jinong").llm_extra_body_dict == {"chat_template_kwargs": {"enable_thinking": False}}
    assert _settings(llm_provider="openai").llm_extra_body_dict is None
    # gemini 는 OpenAI 전용 extra_body 를 무시한다(명시해도 None)
    assert _settings(llm_provider="gemini", llm_extra_body='{"x": 1}').llm_extra_body_dict is None


def test_openai_factory_is_chat_openai():
    llm = make_chat_model(_settings(llm_provider="openai", llm_api_key="sk-test", llm_model="gpt-4.1"))
    assert type(llm).__name__ == "ChatOpenAI" and not is_gemini(llm)
    assert llm.model_name == "gpt-4.1"


def test_gemini_factory_is_vertex_backed(monkeypatch):
    pytest.importorskip("langchain_google_genai")
    # 자격증명 없이도 객체는 만들어져야 한다(호출 시점에만 인증). API 키가 있어도 Vertex 백엔드여야 한다.
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    s = _settings(llm_provider="gemini", llm_model="gemini-3.5-flash", gcp_project_id="proj-x", gcp_location="global")
    llm = make_chat_model(s, max_tokens=1234)
    assert is_gemini(llm) and type(llm).__name__ == "ChatGoogleGenerativeAI"
    assert llm.model.endswith("gemini-3.5-flash") and llm.vertexai is True
    assert llm.project == "proj-x" and llm.location == "global"
    assert llm.max_output_tokens == 1234 and llm.temperature == 0.0 and llm.seed == 42
