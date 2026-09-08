"""도메인 용어 카탈로그 적재 — `jinong_gpu/stt-serve/catalog/catalog.jsonl` 을 경로로 참조한다(복사하지 않음).

스키마 SSOT: `jinong_gpu/stt-serve/catalog/README.md` (`term` 발화형 / `canonical` 표기 / `category` / `meta.crops`).
- 품종(`crop_variety`, 52,746건)은 뺀다 — 대부분 일상어라 오탐원이고 프롬프트에도 안 들어간다.
- 법인 접두(`(주)`·`㈜`·`주식회사`…)는 term/canonical 양쪽에서 뗀다(stt-041 처방 ① 과 같은 표기 규약 정규화).
- 회사명 접두(`팜한농포리캡탄`·`경농다찌가렌`, 87건)는 canonical 을 맨 표기로 돌린다 — 전사 규약은 `포리캡탄` 이라
  표제 그대로 쓰면 stt-041 이 잰 표기 불일치를 그대로 재현한다. 맨 표기가 3자 이상이면 표제로도 등록해
  LLM 이 그 이름을 낼 수 있게 한다(실통화 84통화 실측: 오탐 2건 소멸, `docs/catalog-prescriptions-2026-09-08.md`).
- 카탈로그 구멍 보강(`catalog_gaps.tsv`)은 실통화에서 발화됐는데 표제에 없어 모델이 닮은 표제로 끌려가던 말이다.
  `jinong_gpu` 카탈로그가 이들을 받으면 이 파일은 지운다.
- 불용어(코퍼스 상위 5k, `stopwords_v3top5k.txt`)에 든 용어는 리트리버와 같은 이유로 제외한다.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_NONWORD = re.compile(r"[^0-9A-Za-z가-힣]+")


def norm_chars(text: str) -> str:
    """NFKC → 한글/영숫자만 → 소문자 (voice_eval.stt_score.norm_chars 와 같은 규칙)."""
    return _NONWORD.sub("", unicodedata.normalize("NFKC", text)).lower()

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


# 맨 표기가 이보다 짧으면 별도 표제 행을 만들지 않는다. 다만 canonical 로는 이미 그 표기가 되므로
# 조회 키·프롬프트 노출은 길이와 무관하다 — 이 상수는 표제 중복을 줄일 뿐 짧은 표기를 감추지 않는다
# (실통화 84통화 확정 측정은 이 동작 그대로 낸 값이다: 오탐 3건).
MIN_BARE_LEN = 3
GAPS_PATH = Path(__file__).parents[1] / "voice_eval/term_fix/catalog_gaps.tsv"


def read_rows(path: Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def read_gaps(path: Path) -> list[dict]:
    """`term<TAB>category<TAB>근거` — 주석·빈 줄은 건너뛴다."""
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip():
            out.append({"term": parts[0].strip(), "category": parts[1].strip()})
    return out


def apply_company_prefix(rows: list[dict]) -> list[dict]:
    """농약상표의 회사명 접두를 떼어 canonical(=치환 표기)로 만들고, 맨 표기가 충분히 길면 표제로도 등록한다."""
    comps = sorted({str(r["term"]) for r in rows if r.get("category") == "company" and len(str(r["term"])) >= 2},
                   key=len, reverse=True)
    have = {(str(r.get("term")), r.get("category")) for r in rows}
    extra: list[dict] = []
    for r in rows:
        if r.get("category") != "pesticide_brand":
            continue
        term = str(r.get("term") or "")
        for c in comps:
            if term.startswith(c) and len(term) > len(c) + 1:
                bare = term[len(c):]
                r["canonical"] = bare
                if len(bare) >= MIN_BARE_LEN and (bare, "pesticide_brand") not in have:
                    extra.append({"term": bare, "category": "pesticide_brand"})
                    have.add((bare, "pesticide_brand"))
                break
    return rows + extra


def load_catalog(path: Path, stopwords: Path | None = None,
                 categories: tuple[str, ...] = DOMAIN_CATEGORIES, *,
                 company_prefix: bool = False, gaps: Path | None = None) -> Catalog:
    cat = Catalog(stopwords=load_stopwords(stopwords))
    rows = read_rows(path)
    if company_prefix:
        rows = apply_company_prefix(rows)
    if gaps and Path(gaps).exists():
        have = {(str(r.get("term")), r.get("category")) for r in rows}
        rows = rows + [g for g in read_gaps(Path(gaps)) if (g["term"], g["category"]) not in have]
    seen: set[tuple[str, str]] = set()
    for d in rows:
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


@lru_cache(maxsize=4)
def get_catalog(path: str, stopwords: str = "", company_prefix: bool = True, gaps: str = "") -> Catalog:
    """프로세스당 1회 적재(런타임 노드용). 불용어 경로가 비면 카탈로그 옆 `stopwords_v3top5k.txt` 를 쓴다."""
    sw = Path(stopwords) if stopwords else Path(path).with_name("stopwords_v3top5k.txt")
    gp = Path(gaps) if gaps else GAPS_PATH
    return load_catalog(Path(path), sw if sw.exists() else None,
                        company_prefix=company_prefix, gaps=gp if gp.exists() else None)
