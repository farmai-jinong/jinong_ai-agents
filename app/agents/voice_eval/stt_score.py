"""STT 채점 — CER/WER(참고) + 핵심어 인식률(게이트) + 화자분리 지표.

대본은 애드리브가 허용된 낭독이라 CER/WER 절대치는 의미가 약하다(추세 비교용). 실질 지표는
**핵심어 인식률** — 상표명·병해충명·수치가 전사에서 살아남았는지다. 이름 정규화는 파이프라인이 쓰는
`mapping.matcher.normalize` 와 같은 것을 써서, "다코닐"↔"다코닐에이스 액상수화제" 가 여기서도 일치한다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Literal

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from ..mapping.matcher import decompose_jamo, normalize

FUZZY_THRESHOLD = 85.0                        # partial_ratio 이 이상이면 "비슷하게 들었다"(fuzzy)
_NONWORD = re.compile(r"[^0-9A-Za-z가-힣]+")

# 한글 수사 → 아라비아 숫자. 키워드 매칭에만 적용한다(CER/WER 에는 미적용 — 원문 훼손 방지).
_DIGITS = {"영": 0, "공": 0, "일": 1, "한": 1, "두": 2, "이": 2, "세": 3, "삼": 3, "네": 4, "사": 4,
           "다섯": 5, "오": 5, "여섯": 6, "육": 6, "일곱": 7, "칠": 7, "여덟": 8, "팔": 8, "아홉": 9, "구": 9}
_UNITS = [("만", 10000), ("천", 1000), ("백", 100), ("십", 10)]
_NUMWORD = re.compile("(" + "|".join(sorted(
    list(_DIGITS) + [u for u, _ in _UNITS], key=len, reverse=True)) + ")+")


@dataclass
class KeywordHit:
    keyword: str
    status: Literal["exact", "fuzzy", "miss", "n/a"]
    score: float


@dataclass
class SttScore:
    cer: float
    wer: float
    keyword_recall: float
    keywords: list[KeywordHit]
    n_speakers: int
    top_speaker_share: float
    n_segments: int
    duration_sec: float
    ref_chars: int
    hyp_chars: int
    similarity: float                         # 대본 ↔ 전사 유사도(케이스 오배치 감지)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["keywords"] = [asdict(k) for k in self.keywords]
        return d

    @property
    def misses(self) -> list[str]:
        return [k.keyword for k in self.keywords if k.status == "miss"]

    @property
    def fuzzies(self) -> list[str]:
        return [k.keyword for k in self.keywords if k.status == "fuzzy"]

    @property
    def not_spoken(self) -> list[str]:
        return [k.keyword for k in self.keywords if k.status == "n/a"]


# --------------------------------------------------------------------------- 정규화
def norm_chars(text: str) -> str:
    """CER 용 — NFKC, 한글/영숫자만 남기고 공백 제거."""
    return _NONWORD.sub("", unicodedata.normalize("NFKC", text)).lower()


def norm_tokens(text: str) -> list[str]:
    """WER 용 — 어절 토큰."""
    return _NONWORD.sub(" ", unicodedata.normalize("NFKC", text)).lower().split()


def _hangul_number(word: str) -> int | None:
    """'여섯'→6, '이천'→2000, '천'→1000, '육만'→60000. 못 읽으면 None."""
    total = 0
    chunk = 0
    i = 0
    seen = False
    while i < len(word):
        for unit, mul in _UNITS:
            if word.startswith(unit, i):
                chunk = (chunk or 1) * mul
                if mul == 10000:
                    total += chunk
                    chunk = 0
                seen = True
                i += len(unit)
                break
        else:
            for lit in sorted(_DIGITS, key=len, reverse=True):
                if word.startswith(lit, i):
                    chunk += _DIGITS[lit]
                    seen = True
                    i += len(lit)
                    break
            else:
                return None
    return total + chunk if seen else None


def expand_numerals(text: str) -> str:
    """한글 수사를 숫자로 바꾼 사본을 만든다(원문은 유지하고 매칭용으로만 쓴다)."""
    def sub(m: re.Match[str]) -> str:
        n = _hangul_number(m.group(0))
        return str(n) if n is not None else m.group(0)
    return _NUMWORD.sub(sub, text)


# --------------------------------------------------------------------------- 지표
def cer(reference: str, hypothesis: str) -> float:
    ref = norm_chars(reference)
    if not ref:
        return 0.0
    return Levenshtein.distance(ref, norm_chars(hypothesis)) / len(ref)


def wer(reference: str, hypothesis: str) -> float:
    ref = norm_tokens(reference)
    if not ref:
        return 0.0
    return Levenshtein.distance(ref, norm_tokens(hypothesis)) / len(ref)


def match_keyword(keyword: str, hypothesis: str, family: str | None = None) -> KeywordHit:
    """정규화 후 부분문자열이면 exact, 자모 분해 partial_ratio ≥ 임계면 fuzzy, 아니면 miss.

    자모로 분해해 비교하는 이유는 한글 음절이 원자 단위라 "다코닐"↔"다코날" 이 음절 기준으로는 66점밖에
    안 나오기 때문이다(매칭기 `mapping/matcher.py` 가 같은 이유로 자모를 쓴다). 자모 기준 86점.
    """
    hyp_variants = [hypothesis, expand_numerals(hypothesis)]
    kw_norm = normalize(keyword, family) or norm_chars(keyword)
    for h in hyp_variants:
        h_norm = normalize(h, family) or norm_chars(h)
        if kw_norm and kw_norm in h_norm:
            return KeywordHit(keyword, "exact", 100.0)
    kw_jamo = decompose_jamo(norm_chars(keyword))
    best = max(fuzz.partial_ratio(kw_jamo, decompose_jamo(norm_chars(h))) for h in hyp_variants)
    return KeywordHit(keyword, "fuzzy" if best >= FUZZY_THRESHOLD else "miss", round(best, 1))


def keyword_family(keyword: str, expect: dict[str, Any]) -> str | None:
    """expect.json 안에서의 출처로 family 를 정한다 — normalize 의 제형어 제거 규칙이 달라진다."""
    if keyword in (expect.get("products") or []):
        return "product"
    if keyword in [p[0] if isinstance(p, list) else p for p in (expect.get("pests") or [])]:
        return "pest"
    return None


def speaker_stats(segments: list[dict[str, Any]]) -> tuple[int, float]:
    """(화자 수, 최다 화자의 문자 비중) — 화자분리가 한 명으로 뭉개졌는지 감지."""
    by: dict[str, int] = {}
    for s in segments:
        by[str(s.get("speaker") or "?")] = by.get(str(s.get("speaker") or "?"), 0) + len(s.get("text") or "")
    total = sum(by.values())
    return len(by), (max(by.values()) / total if total else 1.0)


def score(*, reference: str, hypothesis: str, keywords: list[str], expect: dict[str, Any],
          segments: list[dict[str, Any]], duration_sec: float) -> SttScore:
    """핵심어는 **대본에 실제로 발화된 것만** 채점한다.

    `expect.json` 의 이름은 farmos 표준 명칭(잿빛곰팡이병·탄저병…)인데 대본에서는 구어(잿빛곰팡이·탄저)로
    말한다. 아무도 말하지 않은 표기를 STT 가 만들어낼 수는 없으므로, 대본에 없는 핵심어는 `n/a` 로 빼고
    분모에서 제외한다. 표준 명칭 복원은 STT 가 아니라 매핑 단계의 책임이다(`metrics.py` 에서 별도 채점).
    """
    from .audio import similarity
    n_spk, share = speaker_stats(segments)
    hits: list[KeywordHit] = []
    for k in keywords:
        fam = keyword_family(k, expect)
        if match_keyword(k, reference, fam).status == "miss":
            hits.append(KeywordHit(k, "n/a", 0.0))
            continue
        hits.append(match_keyword(k, hypothesis, fam))
    scored = [h for h in hits if h.status != "n/a"]
    hit_n = sum(1 for h in scored if h.status in ("exact", "fuzzy"))
    return SttScore(
        cer=round(cer(reference, hypothesis), 4),
        wer=round(wer(reference, hypothesis), 4),
        keyword_recall=round(hit_n / len(scored), 4) if scored else 1.0,
        keywords=hits,
        n_speakers=n_spk,
        top_speaker_share=round(share, 4),
        n_segments=len(segments),
        duration_sec=round(duration_sec, 1),
        ref_chars=len(norm_chars(reference)),
        hyp_chars=len(norm_chars(hypothesis)),
        similarity=round(similarity(reference, hypothesis), 1),
    )
