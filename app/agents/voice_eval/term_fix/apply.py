"""LLM 치환 제안의 결정적 적용 + 가드.

LLM 은 목록만 내고, 텍스트를 실제로 바꾸는 것은 여기서만 한다. 가드는 게이트웨이 `terms.correct` 의 교훈
(치환은 오탐이 비싸다)을 따른다: 카탈로그 밖 표기 금지, 일상어(불용어) 원문 금지, 숫자·단위 금지, 세그먼트당 상한,
같은 자리 중복 금지, 신뢰도 하한.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from ..stt_score import norm_chars
from .catalog import Catalog
from .schemas import TermCorrection

_HAS_WORD = re.compile(r"[가-힣A-Za-z]")


@dataclass
class Applied:
    seg_id: int
    original: str
    replacement: str
    category: str | None
    confidence: float
    reason: str
    status: str          # applied | rejected
    why: str = ""        # rejected 사유

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _reject(c: TermCorrection, why: str) -> Applied:
    return Applied(c.seg_id, c.original, c.replacement, c.category, c.confidence, c.reason, "rejected", why)


def apply_corrections(segments: list[dict[str, Any]], proposals: list[TermCorrection], catalog: Catalog, *,
                      min_confidence: float = 0.7, max_per_segment: int = 3,
                      max_len_ratio: float = 3.0) -> tuple[list[dict[str, Any]], list[Applied]]:
    """세그먼트 사본에 치환을 적용한다. 세그먼트는 `{"text": ...}` 를 가진 dict, seg_id 는 인덱스."""
    out = [dict(s) for s in segments]
    log: list[Applied] = []
    spans: dict[int, list[tuple[int, int]]] = {}       # seg → 이미 치환된 구간(현재 텍스트 좌표)
    for c in sorted(proposals, key=lambda x: (-x.confidence, x.seg_id)):
        orig = c.original.strip()
        if not (0 <= c.seg_id < len(out)):
            log.append(_reject(c, "seg_id_out_of_range"))
            continue
        if len(orig) < 2 or not _HAS_WORD.search(orig):
            log.append(_reject(c, "original_too_short_or_nonword"))
            continue
        text = out[c.seg_id].get("text") or ""
        if orig not in text:
            log.append(_reject(c, "original_not_in_segment"))
            continue
        term = catalog.lookup(c.replacement)
        if term is None:
            log.append(_reject(c, "replacement_not_in_catalog"))
            continue
        repl = term.display
        if norm_chars(orig) == norm_chars(repl):
            log.append(_reject(c, "noop"))
            continue
        if catalog.lookup(orig) is term:
            # 원문이 이미 같은 용어의 발화형(독음 변형·법인 접두)이다 — 오청이 아니라 표기 규약이므로 이 실험 밖
            log.append(_reject(c, "already_catalog_form"))
            continue
        if catalog.is_stopword(orig):
            log.append(_reject(c, "original_is_common_word"))
            continue
        if len(norm_chars(repl)) > max_len_ratio * max(1, len(norm_chars(orig))):
            log.append(_reject(c, "replacement_too_long"))
            continue
        if c.confidence < min_confidence:
            log.append(_reject(c, f"confidence<{min_confidence}"))
            continue
        done = spans.setdefault(c.seg_id, [])
        if len(done) >= max_per_segment:
            log.append(_reject(c, "segment_cap"))
            continue
        # 이미 치환된 구간과 겹치지 않는 출현만 바꾼다(겹치는 것뿐이면 거부)
        hits = [i for i in _find_all(text, orig) if not any(i < e and i + len(orig) > s for s, e in done)]
        if not hits:
            log.append(_reject(c, "overlaps_applied"))
            continue
        hits = [i for i in hits if not _inside_longer_term(text, i, len(orig), catalog)]
        if not hits:
            log.append(_reject(c, "inside_longer_catalog_term"))
            continue
        new_text, new_spans, shift = [], [], 0
        pos = 0
        for i in hits:
            new_text.append(text[pos:i])
            start = i + shift
            new_text.append(repl)
            new_spans.append((start, start + len(repl)))
            shift += len(repl) - len(orig)
            pos = i + len(orig)
        new_text.append(text[pos:])
        done[:] = [(_shift(s, hits, len(repl) - len(orig)), _shift(e, hits, len(repl) - len(orig))) for s, e in done] + new_spans
        out[c.seg_id]["text"] = "".join(new_text)
        log.append(Applied(c.seg_id, orig, repl, term.category, c.confidence, c.reason, "applied"))
    return out, log


_TOKEN_BREAK = re.compile(r"[\s,.!?;:()\[\]\"']")


def _inside_longer_term(text: str, i: int, n: int, catalog: Catalog) -> bool:
    """출현이 더 긴 카탈로그 용어 안에 있으면(예: '응애' ⊂ '응애특급') 그 출현은 건드리지 않는다."""
    lo = i
    while lo > 0 and not _TOKEN_BREAK.match(text[lo - 1]):
        lo -= 1
    hi = i + n
    while hi < len(text) and not _TOKEN_BREAK.match(text[hi]):
        hi += 1
    token = text[lo:hi]
    rel = i - lo
    for length in range(len(token), n, -1):
        for start in range(0, len(token) - length + 1):
            if start <= rel and rel + n <= start + length and catalog.lookup(token[start:start + length]) is not None:
                return True
    return False


def _find_all(text: str, sub: str) -> list[int]:
    out, i = [], text.find(sub)
    while i != -1:
        out.append(i)
        i = text.find(sub, i + len(sub))
    return out


def _shift(pos: int, hits: list[int], delta: int) -> int:
    """앞쪽에서 일어난 치환 수만큼 좌표를 민다."""
    return pos + delta * sum(1 for h in hits if h < pos)


def join_text(segments: list[dict[str, Any]]) -> str:
    return " ".join((s.get("text") or "").strip() for s in segments if (s.get("text") or "").strip())
