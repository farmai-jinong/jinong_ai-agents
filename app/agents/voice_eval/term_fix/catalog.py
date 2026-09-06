"""도메인 용어 카탈로그 적재 — `jinong_gpu/stt-serve/catalog/catalog.jsonl` 을 경로로 참조한다(복사하지 않음).

스키마 SSOT: `jinong_gpu/stt-serve/catalog/README.md` (`term` 발화형 / `canonical` 표기 / `category` / `meta.crops`).
- 품종(`crop_variety`, 52,746건)은 뺀다 — 대부분 일상어라 오탐원이고 프롬프트에도 안 들어간다.
- 법인 접두(`(주)`·`㈜`·`주식회사`…)는 term/canonical 양쪽에서 뗀다(stt-041 처방 ① 과 같은 표기 규약 정규화).
- 불용어(코퍼스 상위 5k, `stopwords_v3top5k.txt`)에 든 용어는 리트리버와 같은 이유로 제외한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..stt_score import norm_chars

DOMAIN_CATEGORIES = ("pesticide_brand", "pest", "disease", "company")
CATEGORY_LABEL = {"pesticide_brand": "농약·자재 상표", "pest": "해충", "disease": "병", "company": "회사"}
_LEGAL_PREFIX = re.compile(r"^(?:\(주\)|㈜|주식회사|\(유\)|\(합\)|농업회사법인)\s*")
_LEGAL_SUFFIX = re.compile(r"\s*(?:\(주\)|㈜|주식회사)$")


def strip_legal(name: str) -> str:
    return _LEGAL_SUFFIX.sub("", _LEGAL_PREFIX.sub("", name.strip())).strip()


@dataclass(frozen=True)
class Term:
    term: str
    canonical: str
    category: str

    @property
    def display(self) -> str:
        """프롬프트에 보여 주고 치환에 쓰는 표기 = canonical(있으면) — 리트리버의 주입 규칙과 같다."""
        return self.canonical or self.term


@dataclass
class Catalog:
    terms: list[Term] = field(default_factory=list)
    stopwords: set[str] = field(default_factory=set)
    _by_norm: dict[str, Term] = field(default_factory=dict, repr=False)

    def __len__(self) -> int:
        return len(self.terms)

    # ------------------------------------------------------------------ 조회
    def lookup(self, text: str) -> Term | None:
        """표기 정규화(NFKC·소문자·공백/구두점 제거·법인 접두 제거) 후 일치하는 용어."""
        return self._by_norm.get(norm_chars(strip_legal(text)))

    def is_stopword(self, text: str) -> bool:
        return norm_chars(text) in self.stopwords

    def displays(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for t in self.terms:
            if t.display not in seen:
                seen.add(t.display)
                out.append(t.display)
        return out

    def prompt_block(self) -> str:
        """카테고리별로 묶은 용어 목록(프롬프트용). 독음 변형은 canonical 로 접혀 한 번만 나온다."""
        groups: dict[str, list[str]] = {c: [] for c in DOMAIN_CATEGORIES}
        seen: set[str] = set()
        for t in self.terms:
            if t.display in seen:
                continue
            seen.add(t.display)
            groups.setdefault(t.category, []).append(t.display)
        lines = []
        for cat, names in groups.items():
            if names:
                lines.append(f"### {CATEGORY_LABEL.get(cat, cat)} ({len(names)})\n" + ", ".join(sorted(names)))
        return "\n\n".join(lines)


def load_stopwords(path: Path | None) -> set[str]:
    if not path or not Path(path).exists():
        return set()
    return {norm_chars(w) for w in Path(path).read_text(encoding="utf-8").split() if w.strip()}


def load_catalog(path: Path, stopwords: Path | None = None,
                 categories: tuple[str, ...] = DOMAIN_CATEGORIES) -> Catalog:
    cat = Catalog(stopwords=load_stopwords(stopwords))
    seen: set[tuple[str, str]] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("category") not in categories:
            continue
        term = strip_legal(str(d.get("term") or ""))
        canonical = strip_legal(str(d.get("canonical") or "")) if d.get("canonical") else ""
        if canonical == term:
            canonical = ""
        if not term or (term, d["category"]) in seen:
            continue
        if norm_chars(term) in cat.stopwords or (canonical and norm_chars(canonical) in cat.stopwords):
            continue
        seen.add((term, d["category"]))
        t = Term(term=term, canonical=canonical, category=d["category"])
        cat.terms.append(t)
        for key in (norm_chars(term), norm_chars(canonical) if canonical else ""):
            if key and key not in cat._by_norm:
                cat._by_norm[key] = t
    return cat
