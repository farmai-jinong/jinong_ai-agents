"""이름 → 표준 코드 매칭 (농작업 / 병해충 / 약제 / 작물).

정책(D7): LLM 은 코드를 만들지 않는다. 여기서 exact → substring → rapidfuzz(문자열 + 자모 분해)
로 후보를 고르고, `auto` 이상이면 확정, `ambiguous`~`auto` 는 후보 top-k 를 남겨 LLM 이 그중에서만
고르게 하며, 미만은 unmatched.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import fuzz

_SYN_PATH = Path(__file__).with_name("synonyms.yaml")

# --------------------------------------------------------------------------- 자모 분해 (외부 의존성 없음)
_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JONG = ["", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ", "ㄾ", "ㄿ", "ㅀ", "ㅁ", "ㅂ",
         "ㅄ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]


def decompose_jamo(s: str) -> str:
    out: list[str] = []
    for ch in s:
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3:
            i = o - 0xAC00
            out.append(_CHO[i // 588])
            out.append(_JUNG[(i % 588) // 28])
            j = _JONG[i % 28]
            if j:
                out.append(j)
        else:
            out.append(ch)
    return "".join(out)


# --------------------------------------------------------------------------- 사전
@lru_cache
def load_synonyms() -> dict[str, Any]:
    with _SYN_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_PAREN = re.compile(r"[\(\[（【][^\)\]）】]*[\)\]）】]")
_NONWORD = re.compile(r"[\s\-\_\.\,\·\/\\\'\"`~!@#$%^&*+=:;?<>|]+")


def normalize(s: str | None, family: str | None = None) -> str:
    """NFKC → 소문자 → 괄호내용/공백/구두점 제거 → (family) 제형어·일반접미사 제거 → 동의어."""
    if not s:
        return ""
    syn = load_synonyms()
    t = unicodedata.normalize("NFKC", s).lower().strip()
    t = _PAREN.sub("", t)
    t = _NONWORD.sub("", t)
    if family == "product":
        for suf in sorted(syn.get("formulation_suffixes", []), key=len, reverse=True):
            if t.endswith(suf) and len(t) > len(suf):
                t = t[: -len(suf)]
                break
    if family in ("farmwork", None):
        for suf in sorted(syn.get("generic_suffixes", []), key=len, reverse=True):
            if t.endswith(suf) and len(t) > len(suf) + 1:
                t = t[: -len(suf)]
                break
    if family:
        table = syn.get(family) or {}
        if t in table:
            t = str(table[t])
        else:
            # 정규화된 키와 비교 (yaml 키에 공백/괄호가 있을 수 있음)
            for k, v in table.items():
                if _NONWORD.sub("", unicodedata.normalize("NFKC", str(k)).lower()) == t:
                    t = str(v)
                    break
    return t


# --------------------------------------------------------------------------- 결과
@dataclass
class Candidate:
    code: str
    name: str
    score: float
    method: str
    item: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "score": round(self.score, 1), "method": self.method}


@dataclass
class MatchResult:
    status: str                       # matched | ambiguous | unmatched
    best: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def method(self) -> str | None:
        return self.best.method if self.best else None


def _score(q: str, c: str) -> float:
    if not q or not c:
        return 0.0
    s1 = fuzz.WRatio(q, c)
    s2 = fuzz.token_set_ratio(q, c)
    s3 = fuzz.ratio(decompose_jamo(q), decompose_jamo(c))
    return max(s1, s2, s3)


def match(query: str, candidates: Iterable[Any], *, key: Callable[[Any], str], code: Callable[[Any], str],
          family: str | None = None, auto: float = 88.0, ambiguous: float = 70.0, top_k: int = 5,
          sole_auto: float = 80.0) -> MatchResult:
    q = normalize(query, family)
    if not q:
        return MatchResult("unmatched")
    items = list(candidates)
    normed = [(it, normalize(key(it), family)) for it in items]

    # 1) exact
    exact = [it for it, n in normed if n and n == q]
    if len(exact) == 1:
        it = exact[0]
        return MatchResult("matched", Candidate(code(it), key(it), 100.0, "exact", it), [Candidate(code(it), key(it), 100.0, "exact", it)])
    if len(exact) > 1:
        cands = [Candidate(code(it), key(it), 100.0, "exact", it) for it in exact[:top_k]]
        return MatchResult("ambiguous", None, cands)

    # 2) substring (양방향, 길이 ≥ 2)
    if len(q) >= 2:
        subs = [(it, n) for it, n in normed if n and len(n) >= 2 and (q in n or n in q)]
        if len(subs) == 1:
            it, n = subs[0]
            sc = max(_score(q, n), 90.0)
            return MatchResult("matched", Candidate(code(it), key(it), sc, "substring", it),
                               [Candidate(code(it), key(it), sc, "substring", it)])
        if len(subs) > 1:
            # 여러 개 포함 → 가장 긴 공통(=가장 짧은 후보 길이 차) 우선, 점수차 크면 확정
            scored = sorted(((it, n, _score(q, n)) for it, n in subs), key=lambda x: -x[2])
            cands = [Candidate(code(it), key(it), sc, "substring", it) for it, n, sc in scored[:top_k]]
            if len(scored) >= 2 and scored[0][2] - scored[1][2] >= 15 and scored[0][2] >= auto:
                return MatchResult("matched", cands[0], cands)
            return MatchResult("ambiguous", None, cands)

    # 3) fuzzy
    scored = sorted(((it, n, _score(q, n)) for it, n in normed if n), key=lambda x: -x[2])
    cands = [Candidate(code(it), key(it), sc, "fuzzy", it) for it, n, sc in scored[:top_k] if sc >= ambiguous]
    if not cands:
        return MatchResult("unmatched", None, [Candidate(code(it), key(it), sc, "fuzzy", it) for it, n, sc in scored[:3]])
    top = cands[0]
    if top.score >= auto and (len(cands) == 1 or top.score - cands[1].score >= 5 or cands[1].score < auto):
        return MatchResult("matched", top, cands)
    if len(cands) == 1 and top.score >= sole_auto:
        top.method = "fuzzy-sole"
        return MatchResult("matched", top, cands)
    return MatchResult("ambiguous", None, cands)


def dedupe_texts(texts: list[str], threshold: float = 90.0) -> list[int]:
    """중복 텍스트 인덱스 제거 — 유지할 인덱스 목록 반환 (첫 등장 우선)."""
    keep: list[int] = []
    for i, t in enumerate(texts):
        n = normalize(t)
        if not n:
            continue
        if any(fuzz.ratio(n, normalize(texts[j])) >= threshold for j in keep):
            continue
        keep.append(i)
    return keep
