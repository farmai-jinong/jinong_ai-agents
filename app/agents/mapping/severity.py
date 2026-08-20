"""병해충 정도(severity) → 발생단계 인덱스.

표준 6단계: 0 미발생 | 1 2%미만 | 2 5%미만 | 3 10%미만 | 4 30%미만 | 5 30%이상 (설명서 §3.3).
%가 원문에 있으면 우선, 없으면 단어 매핑(경미→1, 보통→2, 심함→4, 불명→1+warning). SFK 매핑(0|1 약, 2|3 중,
4|5 강)과 정합.
"""

from __future__ import annotations

import re

_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|퍼센트|프로|percent)")
_WORD_STEP = {"경미": 1, "보통": 2, "심함": 4, "불명": 1}
_RAW_HINTS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"(전멸|다\s*죽|절반|반\s*이상|엄청|심각|아주\s*많|너무\s*많|번졌|퍼졌|온\s*하우스)"), 5),
    (re.compile(r"(많이|많다|많아|심하|심해|꽤|제법|여기저기)"), 4),
    (re.compile(r"(군데군데|몇\s*군데|일부|어느\s*정도|중간)"), 3),
    (re.compile(r"(조금|약간|살짝|몇\s*개|한두|한\s*두|초기|시작|보이기\s*시작)"), 1),
]


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
    warnings: list[str] = []
    raw = severity_raw or ""
    m = _PCT.search(raw)
    if m:
        try:
            return pct_to_step(float(m.group(1))), warnings
        except ValueError:
            pass
    for pat, step in _RAW_HINTS:
        if pat.search(raw):
            if severity in ("경미", "보통", "심함") and abs(_WORD_STEP[severity] - step) > 2:
                # 원문 힌트와 LLM 판정이 크게 다르면 LLM 판정 + 경고
                warnings.append(f"발생단계 판정 불일치(원문 '{raw}' vs {severity}) — 확인 필요")
                return _WORD_STEP[severity], warnings
            return step, warnings
    if severity in _WORD_STEP:
        if severity == "불명":
            warnings.append("발생 정도 언급 없음 — 단계 확인 필요")
        if status == "의심":
            warnings.append("의심 단계 — 실제 발생 여부 확인 필요")
        return _WORD_STEP[severity], warnings
    warnings.append("발생 정도 판정 불가 — 단계 확인 필요")
    return 1, warnings
