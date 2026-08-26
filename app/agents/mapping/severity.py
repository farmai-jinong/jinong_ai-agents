"""병해충 정도(severity) → 발생단계 인덱스.

표준 6단계: 0 미발생 | 1 2%미만 | 2 5%미만 | 3 10%미만 | 4 30%미만 | 5 30%이상 (설명서 §3.3).
%가 원문에 있으면 우선, 없으면 단어 매핑(경미→1, 보통→2, 심함→4, 불명→1+warning). SFK 매핑(0|1 약, 2|3 중,
4|5 강)과 정합.

단어 매핑과 구어 힌트 표는 `severity.yaml` 에 데이터로 있다(자가 개선 루프가 튜닝하는 표면).
%→단계 구간(`pct_to_step`)은 설명서 스펙이라 코드에 고정한다.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|퍼센트|프로|percent)")
_RULES_PATH = Path(__file__).with_name("severity.yaml")


@lru_cache
def _rules() -> tuple[dict[str, int], tuple[tuple[re.Pattern[str], int], ...], int]:
    """(word_step, raw_hints, disagreement_tolerance) — severity.yaml 에서 1회 로드."""
    d = yaml.safe_load(_RULES_PATH.read_text(encoding="utf-8")) or {}
    word_step = {str(k): int(v) for k, v in (d.get("word_step") or {}).items()}
    hints = tuple((re.compile(str(h["pattern"])), int(h["step"])) for h in (d.get("raw_hints") or []))
    return word_step, hints, int(d.get("disagreement_tolerance", 2))


def reset_rules_cache() -> None:
    """severity.yaml 을 고친 뒤 같은 프로세스에서 다시 읽을 때 (테스트·튜닝 루프)."""
    _rules.cache_clear()


def pct_to_step(p: float) -> int:
    if p <= 0:
        return 0
    if p < 2:
        return 1
    if p < 5:
        return 2
    if p < 10:
        return 3
    if p < 30:
        return 4
    return 5


def severity_to_step(severity: str | None, severity_raw: str | None = None, status: str | None = None) -> tuple[int, list[str]]:
    """(단계 인덱스, warnings)."""
    word_step, raw_hints, tolerance = _rules()
    warnings: list[str] = []
    raw = severity_raw or ""
    m = _PCT.search(raw)
    if m:
        try:
            return pct_to_step(float(m.group(1))), warnings
        except ValueError:
            pass
    for pat, step in raw_hints:
        if pat.search(raw):
            if severity in ("경미", "보통", "심함") and abs(word_step[severity] - step) > tolerance:
                # 원문 힌트와 LLM 판정이 크게 다르면 LLM 판정 + 경고
                warnings.append(f"발생단계 판정 불일치(원문 '{raw}' vs {severity}) — 확인 필요")
                return word_step[severity], warnings
            return step, warnings
    if severity in word_step:
        if severity == "불명":
            warnings.append("발생 정도 언급 없음 — 단계 확인 필요")
        if status == "의심":
            warnings.append("의심 단계 — 실제 발생 여부 확인 필요")
        return word_step[severity], warnings
    warnings.append("발생 정도 판정 불가 — 단계 확인 필요")
    return 1, warnings
