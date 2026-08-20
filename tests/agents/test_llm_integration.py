"""실제 LLM 통합 테스트 — `pytest -m llm`.
openai/jinong: LLM_API_KEY 또는 OPENAI_API_KEY. gemini: LLM_PROVIDER=gemini + GOOGLE_APPLICATION_CREDENTIALS(SA 키, .env 가능)."""

import os

import pytest

from app.agents.deps import Deps
from app.agents.graph import LangGraphPipeline
from app.agents.tools.fake_farmos import FakeFarmosClient
from app.clients.llm import make_chat_model
from app.config import Settings

from .conftest import FIX, load_call

_ENV = Settings(_env_file=".env" if os.path.exists(".env") else None)
KEY = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
GEMINI_OK = _ENV.llm_provider == "gemini" and bool(_ENV.google_application_credentials or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
pytestmark = [pytest.mark.llm, pytest.mark.skipif(not (KEY or GEMINI_OK), reason="LLM 키/자격증명 없음")]


@pytest.mark.asyncio
async def test_real_llm_strawberry():
    s = Settings(_env_file=".env" if os.path.exists(".env") else None)
    if s.llm_provider != "gemini":
        s.llm_api_key = s.llm_api_key or KEY
    llm = make_chat_model(s)
    tr, ctx = load_call("strawberry_botrytis")
    pipe = LangGraphPipeline(s, Deps(settings=s, llm=llm, farmos_factory=lambda tok: FakeFarmosClient(FIX / "farmos")))
    res = await pipe.run(tr, ctx)
    d = res.diaries[0]
    assert d.prdlst_code == "0804MM" and d.status == "OK"
    names = [f["userFarmworkNm"] for f in d.structured["prefill"]["userFarmworkList"]]
    assert "관수" in names and "적엽" in names
    assert d.structured["prefill"]["dbyhsList"] and d.structured["prefill"]["dbyhsList"][0]["dbyhsNm"] == "잿빛곰팡이병"
    assert res.speaker_map == {"f0:A": "farmer", "f0:B": "consultant"}
    assert res.report and "사파이어" in res.report.markdown
    facts = res.facts
    n_turns = len(tr.segments)
    for k in ("farmworks", "pests", "products", "advice"):
        for it in facts[k]:
            assert it["evidence"] and all(0 <= e < n_turns for e in it["evidence"])
