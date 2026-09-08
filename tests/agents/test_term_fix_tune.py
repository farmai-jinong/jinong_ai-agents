"""카탈로그·가드 처방 비교 하네스 — 변형이 무엇을 바꾸는지, 스크리닝이 도는지 LLM 없이 검증한다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.term_fix.apply import apply_corrections
from app.agents.term_fix.catalog import load_catalog
from app.agents.term_fix.schemas import TermCorrection
from app.agents.voice_eval.term_fix import tune

ROWS = [
    {"term": "팜한농포리캡탄", "category": "pesticide_brand"},
    {"term": "경농디치", "category": "pesticide_brand"},          # 맨 표기 2자 → 표제 추가 안 함
    {"term": "경농벤타존", "category": "pesticide_brand"},
    {"term": "벤타존", "category": "pesticide_brand"},             # 이미 표제로 있음 → 중복 추가 안 함
    {"term": "마세트", "category": "pesticide_brand"},
    {"term": "마세트300", "category": "pesticide_brand"},
    {"term": "팜한농", "category": "company"},
    {"term": "경농", "category": "company"},
]


@pytest.fixture
def base(tmp_path: Path) -> Path:
    p = tmp_path / "catalog.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in ROWS), encoding="utf-8")
    return p


def test_company_prefix_rewrites_canonical_and_adds_bare_terms(base: Path):
    rows, changes = tune.strip_company_prefix(tune.read_catalog_rows(base), add_bare=True)
    by = {(r["term"], r["category"]): r for r in rows}
    assert by[("팜한농포리캡탄", "pesticide_brand")]["canonical"] == "포리캡탄"   # 치환은 전사자 표기로 나간다
    assert by[("경농디치", "pesticide_brand")]["canonical"] == "디치"
    assert ("포리캡탄", "pesticide_brand") in by                                  # ≥3자 → 표제로도 추가
    assert ("디치", "pesticide_brand") not in by                                  # 2자 → 새 매칭 표면을 만들지 않는다
    assert len([r for r in rows if r["term"] == "벤타존"]) == 1                    # 이미 있으면 중복 추가 안 함
    assert {a for a, _, _ in changes} == {"팜한농포리캡탄", "경농디치", "경농벤타존"}


def test_company_prefix_canonical_only_adds_nothing(base: Path):
    src = tune.read_catalog_rows(base)
    rows, _ = tune.strip_company_prefix(src, add_bare=False)
    assert len(rows) == len(src)
    assert {r["term"]: r.get("canonical") for r in rows}["팜한농포리캡탄"] == "포리캡탄"


def test_company_prefix_fixes_the_substitution(base: Path, tmp_path: Path):
    """`포리캡탄이요` 가 정답인 자리에서 R0 는 `팜한농포리캡탄` 을 쓰고 R1 은 `포리캡탄` 을 쓴다."""
    segs = [{"text": "포리캡틴이요"}]
    prop = [TermCorrection(seg_id=0, original="포리캡틴", replacement="팜한농포리캡탄", confidence=0.9, reason="")]
    r0 = load_catalog(base)
    fixed0, _ = apply_corrections([dict(s) for s in segs], prop, r0, min_confidence=0.9)
    assert fixed0[0]["text"] == "팜한농포리캡탄이요"

    rows, _ = tune.strip_company_prefix(tune.read_catalog_rows(base), add_bare=True)
    v = tmp_path / "v.jsonl"
    v.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    fixed1, _ = apply_corrections([dict(s) for s in segs], prop, load_catalog(v), min_confidence=0.9)
    assert fixed1[0]["text"] == "포리캡탄이요"


def test_guard_rejects_original_that_is_a_catalog_term(base: Path):
    cat = load_catalog(base)
    segs = [{"text": "마세트 유제를요"}]
    prop = [TermCorrection(seg_id=0, original="마세트", replacement="마세트300", confidence=0.9, reason="")]
    _, log_off = apply_corrections(segs, prop, cat, min_confidence=0.9)
    assert log_off[0].status == "applied"
    _, log_on = apply_corrections(segs, prop, cat, min_confidence=0.9, reject_catalog_originals=True)
    assert log_on[0].status == "rejected" and log_on[0].why == "original_is_catalog_term"


def test_load_gaps_skips_comments_and_blank(tmp_path: Path):
    p = tmp_path / "gaps.tsv"
    p.write_text("# 주석\n\n레일단\tpesticide_brand\t근거\n가루이\tpest\n", encoding="utf-8")
    assert tune.load_gaps(p) == [{"term": "레일단", "category": "pesticide_brand"},
                                 {"term": "가루이", "category": "pest"}]


def test_shipped_gap_list_is_wellformed():
    gaps = tune.load_gaps(tune.GAPS)
    assert gaps and all(g["category"] in ("pesticide_brand", "pest", "disease", "company") for g in gaps)


def test_screening_runs_and_recipe_removes_the_false_positive(base: Path, tmp_path: Path):
    fx_dir, run_dir = tmp_path / "fx", tmp_path / "run"
    (run_dir / "call_a").mkdir(parents=True)
    fx_dir.mkdir()
    (fx_dir / "call_a.json").write_text(json.dumps({
        "case": "call_a", "reference": "포리캡탄이요 마세트 유제를요",
        "expect_keywords": [],
        "pass1": {"segments": [{"text": "포리캡틴이요", "gold": ["팜한농포리캡탄"], "ref": "포리캡탄이요"},
                               {"text": "마세트 유제를요", "gold": [], "ref": "마세트 유제를요"}]}},
        ensure_ascii=False), encoding="utf-8")
    (run_dir / "call_a" / "pass1.proposals.json").write_text(json.dumps({"corrections": [
        {"seg_id": 0, "original": "포리캡틴", "replacement": "팜한농포리캡탄", "confidence": 0.9, "reason": ""},
        {"seg_id": 1, "original": "마세트", "replacement": "마세트300", "confidence": 0.9, "reason": ""},
    ]}, ensure_ascii=False), encoding="utf-8")

    out = tmp_path / "tune"
    rc = tune.main(["--run", str(run_dir), "--fixtures", str(fx_dir), "--catalog", str(base),
                    "--recipes", "R0,R12", "--iters", "50", "--out", str(out)])
    assert rc == 0
    res = {r["id"]: r for r in json.loads((out / "summary.json").read_text(encoding="utf-8"))["results"]}
    assert res["R0"]["fp"] == 2 and res["R12"]["fp"] == 0     # 표제 규약 1건 + 원문가드 1건
    assert (out / "catalog-R12.jsonl").exists()
    assert "R0 대비 오탐 변화" in (out / "report.md").read_text(encoding="utf-8")
