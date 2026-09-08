"""좌표 기록·대조 — 오라클 팔과 서빙 팔을 나란히 놓으려 하면 알아채야 한다."""

from __future__ import annotations

import json
from pathlib import Path

from app.agents.voice_eval.term_fix import coordinate as C

ORACLE = C.Coordinate(source="deploy-base-k10-jinong", kind="eval-dump",
                      model="Qwen/Qwen3-ASR-1.7B", engine="in-process greedy",
                      context="oracle-bias", gold_rule="bias")
SERVING = C.Coordinate(source="serving :8105 → :8102", kind="serving",
                       model=".../asr-soup-20260824-g55-w75/checkpoint-4000",
                       engine="pyannote turns + :8100 vLLM 위임", context="retriever",
                       gold_rule="catalog")


def test_same_coordinate_compares_clean():
    assert C.compare(SERVING, SERVING) == []
    assert SERVING.digest() == C.Coordinate(**{**SERVING.__dict__}).digest()


def test_oracle_and_serving_are_flagged_as_different():
    diffs = C.compare(ORACLE, SERVING)
    assert any(d.startswith("context:") for d in diffs)      # 오라클 vs 리트리버 — 가장 비싼 착각
    assert any(d.startswith("model:") for d in diffs)
    assert any(d.startswith("gold_rule:") for d in diffs)
    assert ORACLE.digest() != SERVING.digest()


def test_roundtrip(tmp_path: Path):
    p = tmp_path / "coordinate.json"
    C.save(p, SERVING)
    back = C.load(p)
    assert back is not None and C.compare(back, SERVING) == []
    assert json.loads(p.read_text(encoding="utf-8"))["digest"] == SERVING.digest()
    assert C.load(tmp_path / "nope.json") is None


def test_serving_coordinate_detects_the_delegated_backend():
    """`--asr-url` 이 붙어 있으면 승격 체크포인트를, 아니면 base 고정을 기록한다."""
    delegated = C.serving_coordinate({
        "served_model": "/x/runs/asr-soup/checkpoint-4000", "catalog_md5": "abc",
        "processes": ["python qwen_diar_server.py --asr-url http://127.0.0.1:8100/v1/audio/transcriptions --port 8102"]})
    assert "checkpoint-4000" in delegated.model and ":8100" in delegated.engine

    legacy = C.serving_coordinate({
        "served_model": "/x/runs/asr-soup/checkpoint-4000", "catalog_md5": "abc",
        "processes": ["python qwen_diar_server.py --repo Qwen/Qwen3-ASR-1.7B --port 8102"]})
    assert "base" in legacy.model and "FunASR" in legacy.engine
    assert C.compare(delegated, legacy)          # 교체 전후는 다른 좌표다
