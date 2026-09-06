"""용어 오청 복구 실험 하네스 — 카탈로그 적재·가드·채점·CLI 를 LLM 없이 검증한다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.prompts.loader import load_system, render_user
from app.agents.tools.fake_llm import FakeChatModel, detect_kind
from app.agents.voice_eval.term_fix import __main__ as cli
from app.agents.voice_eval.term_fix.apply import apply_corrections, join_text
from app.agents.voice_eval.term_fix.catalog import load_catalog, strip_legal
from app.agents.voice_eval.term_fix.schemas import TermCorrection
from app.agents.voice_eval.term_fix.score import score_arm

CATALOG_LINES = [
    {"term": "쏘일킹", "category": "pesticide_brand"},
    {"term": "디티공칠", "category": "pesticide_brand", "canonical": "디티07"},
    {"term": "탄저병", "category": "disease"},
    {"term": "점박이응애", "category": "pest"},
    {"term": "(주)팜한농", "category": "company"},
    {"term": "팜한농", "category": "company", "canonical": "(주)팜한농"},
    {"term": "사랑", "category": "pesticide_brand"},          # 불용어 → 제외
    {"term": "설향", "category": "crop_variety", "meta": {"crops": ["딸기"]}},  # 품종 → 제외
]


@pytest.fixture
def catalog(tmp_path: Path):
    p = tmp_path / "catalog.jsonl"
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in CATALOG_LINES), encoding="utf-8")
    sw = tmp_path / "stopwords.txt"
    sw.write_text("사랑\n그냥\n하우스\n", encoding="utf-8")
    return load_catalog(p, sw)


# --------------------------------------------------------------------------- catalog
def test_catalog_filters_and_normalizes(catalog):
    names = {t.display for t in catalog.terms}
    assert "설향" not in names and "사랑" not in names
    assert "팜한농" in names and "(주)팜한농" not in names           # 법인 접두 제거, 중복 접힘
    assert catalog.lookup("디티공칠").display == "디티07"
    assert catalog.lookup("디티 07").display == "디티07"
    assert catalog.lookup("(주)팜한농").display == "팜한농"
    assert catalog.lookup("없는약") is None
    assert catalog.is_stopword("하우스")
    block = catalog.prompt_block()
    assert "쏘일킹" in block and "설향" not in block and "###" in block
    assert strip_legal("주식회사 경농") == "경농"


# --------------------------------------------------------------------------- apply guards
SEGS = [{"speaker": "A", "text": "수독 한 병 쳤어요"},
        {"speaker": "B", "text": "탄두병이 왔네요, 사랑 하나 주세요"},
        {"speaker": "A", "text": "디티공칠 삼천 배로요"}]


def _c(seg, orig, repl, conf=0.9):
    return TermCorrection(seg_id=seg, original=orig, replacement=repl, confidence=conf)


def test_apply_happy_path_and_guards(catalog):
    props = [
        _c(0, "수독", "쏘일킹"),                     # applied
        _c(1, "탄두병", "탄저병"),                   # applied
        _c(1, "사랑", "쏘일킹"),                     # 원문이 불용어 → 거부
        _c(2, "디티공칠", "디티07"),                 # 같은 용어의 독음 변형 → 표기 규약, 실험 밖
        _c(0, "한 병", "탄저병"),                    # 가드에 안 걸리는 오탐 — 적용된다(precision 으로 잡는 몫)
        _c(0, "없음", "쏘일킹"),                     # 원문 없음
        _c(1, "탄저", "쏘일킹", conf=0.85),          # 이미 치환된 '탄저병' 구간 안 → 겹침 거부
        _c(2, "삼천", "쏘일킹", conf=0.5),           # 신뢰도 미달
        _c(2, "삼천", "없는약"),                     # 카탈로그 밖
        _c(9, "수독", "쏘일킹"),                     # seg 범위 밖
    ]
    fixed, log = apply_corrections(SEGS, props, catalog, min_confidence=0.7)
    by = {(a.seg_id, a.original): a for a in log}
    assert fixed[0]["text"] == "쏘일킹 탄저병 쳤어요"
    assert fixed[1]["text"] == "탄저병이 왔네요, 사랑 하나 주세요"
    assert fixed[2]["text"] == SEGS[2]["text"]
    assert by[(1, "사랑")].why == "original_is_common_word"
    assert by[(2, "디티공칠")].why == "already_catalog_form"
    assert by[(0, "없음")].why == "original_not_in_segment"
    assert by[(1, "탄저")].why == "overlaps_applied"
    assert by[(2, "삼천")].why.startswith("confidence<") or by[(2, "삼천")].why == "replacement_not_in_catalog"
    assert by[(9, "수독")].why == "seg_id_out_of_range"
    assert SEGS[0]["text"] == "수독 한 병 쳤어요"           # 원본 불변


def test_apply_segment_cap(catalog):
    segs = [{"text": "수독 탄두병 디티 응애"}]
    props = [_c(0, "수독", "쏘일킹"), _c(0, "탄두병", "탄저병"), _c(0, "디티", "디티07"), _c(0, "응애", "점박이응애")]
    _, log = apply_corrections(segs, props, catalog, max_per_segment=2)
    assert sum(1 for a in log if a.status == "applied") == 2
    assert any(a.why == "segment_cap" for a in log)


# --------------------------------------------------------------------------- score
def test_score_precision_and_recall(catalog):
    reference = "쏘일킹 한 병 쳤어요. 탄저병이 왔네요. 디티07 삼천 배로요."
    props = [_c(0, "수독", "쏘일킹"), _c(1, "탄두병", "탄저병"), _c(0, "한 병", "점박이응애")]
    fixed, log = apply_corrections(SEGS, props, catalog)
    base = score_arm("pass1", join_text(SEGS), reference, ["쏘일킹", "탄저병", "디티07", "점박이응애"], {})
    fix = score_arm("pass1+fix", join_text(fixed), reference, ["쏘일킹", "탄저병", "디티07", "점박이응애"], {}, log)
    assert base.keyword_recall < fix.keyword_recall
    assert fix.tp == 2 and fix.fp == 1 and fix.precision == pytest.approx(2 / 3, abs=1e-3)
    fixed_tp, log_tp = apply_corrections(SEGS, props[:2], catalog)
    assert score_arm("x", join_text(fixed_tp), reference, [], {}, log_tp).cer < base.cer
    kw = {k["keyword"]: k["status"] for k in fix.keywords}
    assert kw["점박이응애"] == "n/a"                    # 대본에 없는 핵심어는 분모 제외


# --------------------------------------------------------------------------- prompt / fake kind
def test_prompt_renders_and_fake_detects(catalog):
    sys_text = load_system("term_fix", preamble=False)
    user = render_user("term_fix", crops=["딸기"], note="", segments=[{"id": 0, "speaker": "A", "text": "수독"}],
                       catalog=catalog.prompt_block())
    assert "evidence" not in sys_text                    # 공통 프리앰블 미포함
    assert "#0 [A] 수독" in user and "쏘일킹" in user
    from langchain_core.messages import HumanMessage, SystemMessage
    assert detect_kind([SystemMessage(content=sys_text), HumanMessage(content=user)]) == "term_fix"


# --------------------------------------------------------------------------- CLI end-to-end (fake LLM)
def test_cli_fake_end_to_end(catalog, tmp_path: Path):
    cat_path = tmp_path / "catalog.jsonl"
    cat_path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in CATALOG_LINES), encoding="utf-8")
    fx_dir = tmp_path / "fx"
    fx_dir.mkdir()
    (fx_dir / "demo_case.json").write_text(json.dumps({
        "case": "demo_case", "reference": "쏘일킹 한 병 쳤어요. 탄저병이 왔네요.",
        "expect_keywords": ["쏘일킹", "탄저병", "점박이응애"],
        "pass1": {"segments": SEGS[:2]},
        "pass2": {"segments": [{"speaker": "A", "text": "쏘일킹 한 병 쳤어요"}, SEGS[1]]},
    }, ensure_ascii=False), encoding="utf-8")

    def respond(messages):
        text = messages[-1].content
        out = []
        if "수독" in text:
            out.append({"seg_id": 0, "original": "수독", "replacement": "쏘일킹", "confidence": 0.9, "reason": "발음"})
        out.append({"seg_id": 1, "original": "탄두병", "replacement": "탄저병", "confidence": 0.8, "reason": "문맥"})
        out.append({"seg_id": 1, "original": "사랑", "replacement": "쏘일킹", "confidence": 0.95, "reason": "오탐"})
        return {"corrections": out}

    llm = FakeChatModel(responses={"term_fix": respond})
    out = tmp_path / "out"
    rc = cli.main(["--fixtures", str(fx_dir), "--catalog", str(cat_path), "--stopwords", str(tmp_path / "stopwords.txt"),
                   "--provider", "fake", "--out", str(out)], llm=llm)
    assert rc == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    v = {x["base"]: x for x in summary["verdicts"]}
    assert v["pass1"]["tp"] == 2 and v["pass1"]["fp"] == 0 and v["pass1"]["pass"] is True
    assert v["pass1"]["mean_recall_fix"] == 1.0 and v["pass1"]["mean_recall_base"] < 1.0
    assert v["pass2"]["tp"] == 1
    assert (out / "report.md").exists() and (out / "demo_case" / "pass1.proposals.json").exists()
    assert "사랑 → 쏘일킹" in (out / "report.md").read_text(encoding="utf-8")   # 거부 내역도 리포트에
    # 캐시 재사용: LLM 호출 없이 재채점
    calls = len(llm.calls)
    assert cli.main(["--fixtures", str(fx_dir), "--catalog", str(cat_path), "--provider", "fake", "--out", str(out)],
                    llm=llm) == 0
    assert len(llm.calls) == calls
