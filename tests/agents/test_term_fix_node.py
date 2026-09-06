"""`correct_terms` 노드 — 그래프 안에서 on/off·실패 강등·raw 불변을 LLM 없이 검증한다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.term_fix.catalog import get_catalog

from .conftest import fake_llm, load_call, make_pipeline

CATALOG = [{"term": "쏘일킹", "category": "pesticide_brand"}, {"term": "탄저병", "category": "disease"},
           {"term": "사파이어", "category": "pesticide_brand"}]


@pytest.fixture
def catalog_path(tmp_path: Path) -> str:
    p = tmp_path / "catalog.jsonl"
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in CATALOG), encoding="utf-8")
    get_catalog.cache_clear()
    return str(p)


def _term_fix_llm(fixture: str, seen: list | None = None, **kw):
    def respond(messages):
        text = messages[-1].content
        # 첫 발화의 첫 어절을 '쏘일킹' 으로 바꾸자고 제안 — 원문에 있는 문자열이어야 적용된다
        first = next(line for line in text.splitlines() if line.startswith("#0 "))
        word = first.split("] ", 1)[1].split()[0]
        return {"corrections": [{"seg_id": 0, "original": word, "replacement": "쏘일킹", "confidence": 0.95,
                                 "reason": "test"},
                                {"seg_id": 0, "original": word, "replacement": "없는약", "confidence": 0.99,
                                 "reason": "카탈로그 밖"}]}
    responses = {"term_fix": respond}
    if seen is not None:
        facts = json.loads((Path(__file__).parent / "fixtures" / "golden" / f"{fixture}.facts.json").read_text(encoding="utf-8"))

        def extract(messages):
            seen.append(messages[-1].content)
            return facts
        responses["extract"] = extract
    return fake_llm(fixture, responses=responses, **kw)


@pytest.mark.asyncio
async def test_off_by_default(settings, farmos_fake):
    transcript, ctx = load_call("strawberry_botrytis")
    llm = _term_fix_llm("strawberry_botrytis")
    result = await make_pipeline(settings, llm, farmos_fake).run(transcript, ctx)
    assert result.term_fix is None
    assert not any(c["kind"] == "term_fix" for c in llm.calls)


@pytest.mark.asyncio
async def test_on_applies_and_keeps_raw(settings, farmos_fake, catalog_path):
    settings.term_fix_enabled = True
    settings.term_fix_catalog_path = catalog_path
    transcript, ctx = load_call("strawberry_botrytis")
    before = transcript.model_dump_json()
    seen: list[str] = []
    llm = _term_fix_llm("strawberry_botrytis", seen)
    result = await make_pipeline(settings, llm, farmos_fake).run(transcript, ctx)
    assert result.term_fix and result.term_fix["status"] == "ok"
    assert result.term_fix["n_applied"] == 1 and result.term_fix["applied"][0]["replacement"] == "쏘일킹"
    assert any(r["why"] == "replacement_not_in_catalog" for r in result.term_fix["rejected"])
    assert result.term_fix["elapsed_s"] >= 0 and result.term_fix["n_calls"] == 1
    assert any(u["name"].startswith("term_fix") for u in result.usage["by_call"])
    assert transcript.model_dump_json() == before                     # raw 전사 불변
    # 교정된 turn 이 추출 프롬프트에 들어갔다
    assert seen and "쏘일킹" in seen[0]


@pytest.mark.asyncio
async def test_failure_degrades(settings, farmos_fake, catalog_path):
    settings.term_fix_enabled = True
    settings.term_fix_catalog_path = catalog_path
    transcript, ctx = load_call("strawberry_botrytis")
    llm = fake_llm("strawberry_botrytis", fail_kinds={"term_fix"})
    result = await make_pipeline(settings, llm, farmos_fake).run(transcript, ctx)
    assert result.term_fix["status"] == "failed"
    assert any("용어 교정 실패" in w for w in result.warnings)
    assert result.diaries                                             # 나머지 파이프라인은 계속 돈다
