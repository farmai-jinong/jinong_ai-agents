import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm import StructuredOutputError, extract_json_text, structured_call
from app.agents.schemas import SummaryOut
from app.agents.tools.fake_llm import FakeChatModel

MSGS = [SystemMessage(content="요약을 하나로 통합"), HumanMessage(content="go")]


def test_extract_json_text():
    assert extract_json_text("```json\n{\"a\": 1}\n```") == '{"a": 1}'
    assert extract_json_text("설명 {\"a\": [1,2]} 끝") == '{"a": [1,2]}'


@pytest.mark.asyncio
async def test_ladder_falls_back_to_json_mode():
    llm = FakeChatModel(responses={"extract_merge": {"one_line_summary": "요약", "keywords": ["k"]}})
    out, trace = await structured_call(llm, SummaryOut, MSGS, name="t")
    assert out.one_line_summary == "요약" and trace.mode == "json_mode" and trace.attempts == 2
    assert trace.total_tokens == 150


@pytest.mark.asyncio
async def test_repair_round_on_bad_json():
    llm = FakeChatModel(responses={"extract_merge": {"one_line_summary": "요약", "keywords": []}}, bad_json_first=True)
    out, trace = await structured_call(llm, SummaryOut, MSGS, name="t")
    assert out.keywords == [] and trace.attempts == 3


@pytest.mark.asyncio
async def test_all_modes_fail_raises():
    llm = FakeChatModel(responses={"extract_merge": "이건 JSON 아님"})
    with pytest.raises(StructuredOutputError):
        await structured_call(llm, SummaryOut, MSGS, name="t", repair_rounds=0)


@pytest.mark.asyncio
async def test_mode_cache_skips_unsupported():
    llm = FakeChatModel(responses={"extract_merge": {"one_line_summary": "a", "keywords": []}})
    await structured_call(llm, SummaryOut, MSGS, name="t")
    _, trace = await structured_call(llm, SummaryOut, MSGS, name="t")
    assert trace.attempts == 1 and trace.mode == "json_mode"

