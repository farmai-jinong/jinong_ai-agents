"""채점 — 정답 용어 recall(`stt_score.match_keyword` 재사용) + 치환 precision + CER.

집계 단위가 둘이다. **케이스 평균(macro)** 은 대본 5건처럼 케이스마다 핵심어가 고르게 있는 세트용이고,
**발생 단위(micro)** 는 실통화 세트처럼 골드가 소수 통화에 몰린 세트용이다 — 92통화 중 골드가 붙은 통화는
일부라 케이스 평균은 골드 없는 통화(recall 1.0)에 희석된다. micro 는 픽스처가 발화마다 실어 준
`segment["gold"]` 를 그 발화 텍스트에 대고 재므로 '같은 용어를 5번 말했는데 1번 놓침' 도 보인다
(통화 단위 매칭은 그걸 못 본다).

CER 은 참고치다: 표기 규약(띄어쓰기·법인 접두)이 회복분을 상쇄하는 것이 :8105 측정의 교훈이라 판정은 용어 단위로 한다.
치환 precision 은 대본(reference)에 **바꾼 표기가 있고 원문 표기는 없으면 TP**, 바꾼 표기가 대본에 없으면 FP,
둘 다 있으면(이형 표기) neutral 로 센다. 바꾼 표기가 대본에 exact 로는 없지만 recall 과 같은 자모 fuzzy 규칙
(`match_keyword`, partial_ratio ≥ 85)으로는 있으면 `tp_variant`(잿빛곰팡이병 ↔ 대본 '잿빛곰팡이') — 용어는 맞고
표기 규약만 다른 경우다. precision 은 strict(tp/(tp+fp))와 lenient((tp+tp_variant)/(tp+tp_variant+fp)) 둘 다 낸다.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Any

from rapidfuzz.distance import Levenshtein

from ...term_fix.apply import Applied
from ..stt_score import cer, keyword_family, match_keyword, norm_chars


@dataclass
class ArmScore:
    arm: str
    cer: float
    keyword_recall: float
    exact_recall: float                 # exact 만 인정(fuzzy 제외) — 매핑 단계가 실제로 덕 보는 지표
    keywords: list[dict[str, Any]]
    n_applied: int = 0
    n_rejected: int = 0
    tp: int = 0
    fp: int = 0
    neutral: int = 0
    tp_variant: int = 0
    corrections: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_s: float = 0.0
    ref_chars: int = 0                  # CER 미시집계용(정규화 후 정답 글자 수) — 공백·구두점은 이미 빠져 있다
    edits: int = 0
    occ_total: int = 0                  # 발생 단위 골드(발화별 `gold`)
    occ_exact: int = 0
    occ_hit: int = 0                    # exact + fuzzy
    occurrences: list[dict[str, Any]] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        judged = self.tp + self.fp
        return round(self.tp / judged, 4) if judged else None

    @property
    def precision_lenient(self) -> float | None:
        judged = self.tp + self.tp_variant + self.fp
        return round((self.tp + self.tp_variant) / judged, 4) if judged else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["precision"] = self.precision
        d["precision_lenient"] = self.precision_lenient
        return d


def keyword_hits(hypothesis: str, reference: str, keywords: list[str], expect: dict[str, Any]) -> list[dict[str, Any]]:
    """대본에 실제로 발화된 핵심어만 채점(`stt_score.score` 와 같은 n/a 규칙)."""
    hits = []
    for k in keywords:
        fam = keyword_family(k, expect)
        if match_keyword(k, reference, fam).status == "miss":
            hits.append({"keyword": k, "status": "n/a", "score": 0.0})
            continue
        h = match_keyword(k, hypothesis, fam)
        hits.append({"keyword": k, "status": h.status, "score": h.score})
    return hits


def recall(hits: list[dict[str, Any]], statuses: tuple[str, ...] = ("exact", "fuzzy")) -> float:
    scored = [h for h in hits if h["status"] != "n/a"]
    if not scored:
        return 1.0
    return round(sum(1 for h in scored if h["status"] in statuses) / len(scored), 4)


def _variant_in_reference(a: Applied, ref_norm: str, reference: str) -> bool:
    """카탈로그 표기 '잿빛곰팡이병'·'탄저병' 을 대본은 '잿빛곰팡이'·'탄저' 로 적는다(구어). 병명의 '병' 접미사를 뗀 형태가
    대본에 있거나, recall 과 같은 자모 fuzzy(≥85)로 있으면 이형(용어는 맞음)으로 본다."""
    r = norm_chars(a.replacement)
    if a.category == "disease" and r.endswith("병") and len(r) > 2 and r[:-1] in ref_norm:
        return True
    return match_keyword(a.replacement, reference).status in ("exact", "fuzzy")


def judge_corrections(applied: list[Applied], reference: str) -> tuple[dict[str, int], list[dict[str, Any]]]:
    ref = norm_chars(reference)
    n = {"tp": 0, "fp": 0, "neutral": 0, "tp_variant": 0}
    rows = []
    for a in applied:
        row = a.to_dict()
        if a.status == "applied":
            in_repl = norm_chars(a.replacement) in ref
            in_orig = norm_chars(a.original) in ref
            if in_repl and not in_orig:
                verdict = "tp"
            elif in_repl:
                verdict = "neutral"
            elif _variant_in_reference(a, ref, reference):
                verdict = "tp_variant"      # 표기 규약 차이(구어는 '병' 을 떼고 말한다)·자모 fuzzy 로는 대본에 있음
            else:
                verdict = "fp"
            n[verdict] += 1
            row["verdict"] = verdict
        rows.append(row)
    return n, rows


# --------------------------------------------------------------------------- 발생 단위 · 미시집계
def occurrence_stats(segments: list[dict[str, Any]]) -> tuple[int, int, int, list[dict[str, Any]]]:
    """발화에 실린 골드 용어(`segment["gold"]`)가 그 발화 텍스트에 남아 있는가 → (발생, exact, exact+fuzzy, 내역)."""
    rows: list[dict[str, Any]] = []
    total = exact = hit = 0
    for i, s in enumerate(segments):
        for t in (s.get("gold") or []):
            h = match_keyword(t, s.get("text") or "")
            total += 1
            exact += h.status == "exact"
            hit += h.status in ("exact", "fuzzy")
            rows.append({"seg": i, "row_id": s.get("row_id"), "term": t, "status": h.status, "score": h.score})
    return total, exact, hit, rows


def cer_parts(reference: str, hypothesis: str) -> tuple[int, int]:
    """(편집거리, 정답 글자 수) — `stt_score.cer` 과 같은 정규화(공백·구두점 제거)라 공백제거 CER 이다."""
    ref = norm_chars(reference)
    return Levenshtein.distance(ref, norm_chars(hypothesis)), len(ref)


def micro_cer(edits: list[int], chars: list[int]) -> float | None:
    n = sum(chars)
    return round(sum(edits) / n, 4) if n else None


def paired_boot(edits_a: list[int], edits_b: list[int], chars: list[int], *, iters: int = 10000,
                seed: int = 0) -> dict[str, float] | None:
    """Δ = CER(b) − CER(a), 케이스 단위 페어드 부트스트랩(`jinong_gpu` stt041_analysis.paired_boot 이식)."""
    n = sum(chars)
    if not chars or not n:
        return None
    rng = random.Random(seed)
    delta = (sum(edits_b) - sum(edits_a)) / n
    ds = []
    for _ in range(iters):
        ea = eb = c = 0
        for _ in range(len(chars)):
            k = rng.randrange(len(chars))
            ea += edits_a[k]
            eb += edits_b[k]
            c += chars[k]
        ds.append((eb - ea) / c if c else 0.0)
    ds.sort()
    return {"delta": round(delta, 5), "lo": round(ds[int(0.025 * iters)], 5),
            "hi": round(ds[int(0.975 * iters) - 1], 5),
            "p_better": round(sum(d < 0 for d in ds) / iters, 4)}


def score_arm(arm: str, hypothesis: str, reference: str, keywords: list[str], expect: dict[str, Any],
              applied: list[Applied] | None = None, *, prompt_tokens: int = 0, completion_tokens: int = 0,
              elapsed_s: float = 0.0, segments: list[dict[str, Any]] | None = None) -> ArmScore:
    hits = keyword_hits(hypothesis, reference, keywords, expect)
    n, rows = judge_corrections(applied or [], reference)
    edits, ref_chars = cer_parts(reference, hypothesis)
    occ_total, occ_exact, occ_hit, occ_rows = occurrence_stats(segments or [])
    return ArmScore(arm=arm, cer=round(cer(reference, hypothesis), 4), keyword_recall=recall(hits),
                    exact_recall=recall(hits, ("exact",)), keywords=hits,
                    n_applied=sum(1 for a in (applied or []) if a.status == "applied"),
                    n_rejected=sum(1 for a in (applied or []) if a.status == "rejected"),
                    tp=n["tp"], fp=n["fp"], neutral=n["neutral"], tp_variant=n["tp_variant"], corrections=rows,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, elapsed_s=round(elapsed_s, 1),
                    ref_chars=ref_chars, edits=edits, occ_total=occ_total, occ_exact=occ_exact, occ_hit=occ_hit,
                    occurrences=occ_rows)
