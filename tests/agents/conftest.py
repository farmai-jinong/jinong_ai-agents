"""파이프라인 테스트 공용 픽스처 — LLM 없이 Fake 로 그래프 전체를 돌린다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agents.deps import Deps
from app.agents.graph import LangGraphPipeline
from app.agents.llm import reset_mode_cache
from app.agents.tools.fake_farmos import FakeFarmosClient
from app.agents.tools.fake_llm import FakeChatModel
from app.config import Settings
from app.schemas.pipeline import CallContext
from app.schemas.transcript import MergedTranscript

FIX = Path(__file__).parent / "fixtures"


def pytest_configure(config):  # type: ignore[no-untyped-def]
    config.addinivalue_line("markers", "llm: 실제 LLM 호출 (LLM_API_KEY/OPENAI_API_KEY 필요)")


@pytest.fixture(autouse=True)
def _reset_mode_cache():
    reset_mode_cache()
    yield
    reset_mode_cache()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, pipeline_impl="langgraph", llm_api_key="test", agent_api_key="k")


def load_call(name: str) -> tuple[MergedTranscript, CallContext]:
    d = json.loads((FIX / "calls" / f"{name}.json").read_text(encoding="utf-8"))
    return MergedTranscript(**d["transcript"]), CallContext(**d["ctx"])


def golden(name: str) -> dict[str, Any]:
    return json.loads((FIX / "golden" / name).read_text(encoding="utf-8"))


def fake_llm(fixture: str, **overrides: Any) -> FakeChatModel:
    key = fixture.split("_")[0]
    responses: dict[str, Any] = {"speaker_roles": golden("speaker_roles.json").get(key, {"files": []})}
    facts_file = FIX / "golden" / f"{fixture}.facts.json"
    if facts_file.exists():
        responses["extract"] = json.loads(facts_file.read_text(encoding="utf-8"))
    responses.update(overrides.pop("responses", {}))
    fail = overrides.pop("fail_kinds", set())
    fail = set(fail) | {k for k in ("report", "diary_content") if k not in responses}
    return FakeChatModel(responses=responses, fail_kinds=fail, **overrides)


def make_pipeline(settings: Settings, llm: Any, farmos: Any | None = None) -> LangGraphPipeline:
    factory = (lambda tok: farmos) if farmos is not None else None
    deps = Deps(settings=settings, llm=llm, farmos_factory=factory)
    return LangGraphPipeline(settings, deps)


@pytest.fixture
def farmos_fake() -> FakeFarmosClient:
    return FakeFarmosClient(FIX / "farmos")
