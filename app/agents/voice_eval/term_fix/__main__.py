"""용어 오청 복구(LLM 사후 교정) 오프라인 실험 CLI.

    python -m app.agents.voice_eval.term_fix --fixtures ~/dev/jinong/jinong_gpu/stt-serve/fixtures/ctx_replay \
        [--catalog PATH] [--stopwords PATH] [--cases a,b] [--arms pass1,pass2] [--provider gemini|fake]
        [--min-confidence 0.8] [--sweep 0.7,0.8,0.9] [--out out/term-fix] [--force] [--dump-prompts]

입력은 `jinong_gpu/stt-serve/fixtures/ctx_replay/*.json`(실녹음 5건: `reference`·`expect_keywords`·`pass1.segments`·
`pass2.segments`). 팔(arm)마다 LLM 제안을 `out/<case>/<arm>.proposals.json` 에 캐시하므로 가드·채점만 바꾼 뒤엔
LLM 없이 재채점된다(`--force` 로 재호출). 카탈로그 경로 기본값은 env `TERM_CATALOG_PATH`.

판정(계획 1단계 기준을 데이터를 보고 한 번 손봄 — 문서 참조): 용어 recall·exact 인식률이 기준 팔보다 떨어지지 않고
TP 가 1건 이상, 치환 precision(lenient — 표기 규약만 다른 이형은 정답으로 침, strict 도 병기) ≥ 0.8, CER 악화 케이스 0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from ....clients.llm import make_chat_model
from ....config import Settings
from ...term_fix.apply import apply_corrections, join_text
from ...term_fix.catalog import Catalog, load_catalog, norm_chars
from ...term_fix.run import propose
from ...term_fix.schemas import TermCorrection
from ...tools.fake_llm import FakeChatModel
from ..cases import TESTCASES
from ..stt_score import match_keyword
from .score import ArmScore, micro_cer, paired_boot, score_arm

log = logging.getLogger("voice_eval.term_fix")
DEFAULT_CATALOG = Path.home() / "dev/jinong/jinong_gpu/stt-serve/catalog/catalog.jsonl"
PRECISION_MIN = 0.8                 # 대본 세트(케이스 평균) 판정용

# 실통화 세트(발생 단위) 사전 등록 판정 기준 — 돌리기 전에 못박은 값이다. 근거는 docs/stt-term-fix-calls-*.md
MICRO_RECALL_MIN = 0.90             # 도메인 용어 발생 recall (base 좌표 .8071)
PRECISION_LENIENT_MIN = 0.90        # 치환 precision(lenient)
CER_CI_MAX = 0.0033                 # 공백제거 CER 페어드 CI 상한(회귀 감시 — 이득은 기대하지 않는다)
BOOT_ITERS = 10000


# --------------------------------------------------------------------------- 입력
def load_fixtures(d: Path, names: list[str] | None) -> list[dict[str, Any]]:
    out = []
    for p in sorted(d.glob("*.json")):
        f = json.loads(p.read_text(encoding="utf-8"))
        if names and f.get("case") not in names:
            continue
        f.setdefault("case", p.stem)
        out.append(f)
    return out


def case_meta(name: str, fx: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    """expect.json(핵심어 family 판정용)과 작물 힌트.

    픽스처가 `expect`·`crops` 를 직접 실어 오면 그것을 쓴다 — 실통화 세트(`import_calls`)에는 테스트케이스
    디렉터리가 없다. 핵심어는 호출부가 `fx["expect_keywords"]` 에서 직접 읽는다.
    """
    if fx is not None and (fx.get("expect") or fx.get("crops")):
        return dict(fx.get("expect") or {}), list(fx.get("crops") or [])
    d = TESTCASES / name
    expect: dict[str, Any] = {}
    crops: list[str] = []
    if (d / "expect.json").exists():
        expect = json.loads((d / "expect.json").read_text(encoding="utf-8"))
    if (d / "source.json").exists():
        src = json.loads((d / "source.json").read_text(encoding="utf-8"))
        base = dict(src.get("original") or {}) or dict((src.get("original_merged_from") or [{}])[0])
        base.update({k: v for k, v in (src.get("enriched") or {}).items() if v is not None})
        for k in ("prdlstNm", "prdlst_nm", "crop"):
            if base.get(k):
                crops = [str(base[k])]
                break
    if not crops and "_" in name:
        crops = [c for c in [{"strawberry": "딸기", "tomato": "토마토"}.get(name.split("_")[0], "")] if c]
    return expect, crops


# --------------------------------------------------------------------------- 실행
async def proposals_for(llm: Any, fx: dict[str, Any], base: str, catalog: Catalog, out_dir: Path,
                        args: argparse.Namespace) -> dict[str, Any]:
    """LLM 제안(캐시). `{"corrections": [...], prompt_tokens, completion_tokens, elapsed_s, model, n_calls}`."""
    name = fx["case"]
    cache = out_dir / name / f"{base}.proposals.json"
    if cache.exists() and not args.force:
        return json.loads(cache.read_text(encoding="utf-8"))
    _, crops = case_meta(name, fx)
    t0 = time.perf_counter()
    corrections, traces = await propose(llm, list(fx[base]["segments"]), catalog, crops=crops,
                                        name=f"term_fix_{name}_{base}", mode=args.mode,
                                        dump_dir=str(out_dir / name) if args.dump_prompts else None, timeout=args.timeout)
    prop = {"corrections": [c.model_dump() for c in corrections],
            "prompt_tokens": sum(t.prompt_tokens for t in traces),
            "completion_tokens": sum(t.completion_tokens for t in traces),
            "elapsed_s": round(time.perf_counter() - t0, 1),
            "model": next((t.model for t in traces if t.model), None), "n_calls": len(traces)}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(prop, ensure_ascii=False, indent=1), encoding="utf-8")
    return prop


def score_fixture(fx: dict[str, Any], base: str, prop: dict[str, Any], catalog: Catalog, *,
                  min_confidence: float, max_per_segment: int) -> tuple[ArmScore, ArmScore, list[dict[str, Any]]]:
    expect, _ = case_meta(fx["case"], fx)
    reference = fx["reference"]
    keywords = list(fx.get("expect_keywords") or [])
    segments = list(fx[base]["segments"])
    base_score = score_arm(base, join_text(segments), reference, keywords, expect, segments=segments)
    proposals = [TermCorrection(**c) for c in prop["corrections"]]
    fixed, applied = apply_corrections(segments, proposals, catalog, min_confidence=min_confidence,
                                       max_per_segment=max_per_segment)
    fix_score = score_arm(f"{base}+fix", join_text(fixed), reference, keywords, expect, applied,
                          prompt_tokens=prop.get("prompt_tokens", 0), completion_tokens=prop.get("completion_tokens", 0),
                          elapsed_s=prop.get("elapsed_s", 0.0), segments=fixed)
    for row in fix_score.corrections:            # 오탐이 무엇을 가리키는지 보려면 그 발화의 정답이 있어야 한다
        seg = segments[row["seg_id"]] if 0 <= row["seg_id"] < len(segments) else {}
        if seg.get("ref"):
            row["seg_ref"] = seg["ref"]
    changed = [{"id": i, "before": segments[i].get("text"), "after": s.get("text")}
               for i, s in enumerate(fixed) if s.get("text") != segments[i].get("text")]
    return base_score, fix_score, changed


def row_of(case: str, base: str, b: ArmScore, f: ArmScore) -> dict[str, Any]:
    return {"case": case, "base": base, "base_recall": b.keyword_recall, "fix_recall": f.keyword_recall,
            "base_exact": b.exact_recall, "fix_exact": f.exact_recall,
            "base_cer": b.cer, "fix_cer": f.cer, "n_applied": f.n_applied, "n_rejected": f.n_rejected,
            "tp": f.tp, "fp": f.fp, "neutral": f.neutral, "tp_variant": f.tp_variant, "corrections": f.corrections,
            "prompt_tokens": f.prompt_tokens, "completion_tokens": f.completion_tokens,
            "elapsed_s": f.elapsed_s, "keywords_fix": f.keywords, "keywords_base": b.keywords,
            "ref_chars": b.ref_chars, "base_edits": b.edits, "fix_edits": f.edits,
            "base_occ_total": b.occ_total, "base_occ_hit": b.occ_hit, "base_occ_exact": b.occ_exact,
            "fix_occ_total": f.occ_total, "fix_occ_hit": f.occ_hit, "fix_occ_exact": f.occ_exact,
            "occ_base": b.occurrences, "occ_fix": f.occurrences}


def make_llm(args: argparse.Namespace) -> Any:
    if args.provider == "fake":
        return FakeChatModel(responses={"term_fix": {"corrections": []}})
    settings = Settings()
    if args.provider:
        settings.llm_provider = args.provider
    if args.model:
        settings.llm_model = args.model
    if settings.llm_provider == "gemini":
        settings.gcp_project_id = os.environ.get("GCP_PROJECT_ID") or settings.gcp_project_id or "jinong-lab-llm"
    return make_chat_model(settings)


# --------------------------------------------------------------------------- 판정 · 리포트
def _fmt(x: float | None) -> str:
    return "—" if x is None else f"{x:.4f}"


def verdict(rows: list[dict[str, Any]], base: str, min_confidence: float | None = None,
            iters: int = BOOT_ITERS) -> dict[str, Any]:
    rs = [r for r in rows if r["base"] == base]
    if not rs:
        return {}
    mean_base = sum(r["base_recall"] for r in rs) / len(rs)
    mean_fix = sum(r["fix_recall"] for r in rs) / len(rs)
    exact_base = sum(r["base_exact"] for r in rs) / len(rs)
    exact_fix = sum(r["fix_exact"] for r in rs) / len(rs)
    tp, fp, tv = (sum(r[k] for r in rs) for k in ("tp", "fp", "tp_variant"))
    strict = round(tp / (tp + fp), 4) if tp + fp else None
    lenient = round((tp + tv) / (tp + tv + fp), 4) if tp + tv + fp else None
    worse = [r["case"] for r in rs if r["fix_cer"] > r["base_cer"]]
    occ = {k: sum(r.get(k, 0) for r in rs) for k in
           ("base_occ_total", "base_occ_hit", "base_occ_exact", "fix_occ_total", "fix_occ_hit", "fix_occ_exact")}
    n_occ = occ["base_occ_total"]
    micro = {"occurrences": n_occ,
             "recall_base": round(occ["base_occ_hit"] / n_occ, 4) if n_occ else None,
             "recall_fix": round(occ["fix_occ_hit"] / n_occ, 4) if n_occ else None,
             "exact_base": round(occ["base_occ_exact"] / n_occ, 4) if n_occ else None,
             "exact_fix": round(occ["fix_occ_exact"] / n_occ, 4) if n_occ else None,
             "cer_base": micro_cer([r["base_edits"] for r in rs], [r["ref_chars"] for r in rs]),
             "cer_fix": micro_cer([r["fix_edits"] for r in rs], [r["ref_chars"] for r in rs]),
             "boot": paired_boot([r["base_edits"] for r in rs], [r["fix_edits"] for r in rs],
                                 [r["ref_chars"] for r in rs], iters=iters) if iters else None}
    # 판정 단위는 세트가 정한다. 발생 단위 골드가 있으면(실통화) micro, 없으면(대본 5건) 케이스 평균.
    if n_occ:
        # 사전 등록 기준 — recall ≥ MICRO_RECALL_MIN ∧ precision(lenient) ≥ PRECISION_LENIENT_MIN
        #                 ∧ 공백제거 CER 페어드 CI 상한 < CER_CI_MAX
        hi = (micro["boot"] or {}).get("hi")
        ok = ((micro["recall_fix"] or 0) >= MICRO_RECALL_MIN
              and (lenient is not None and lenient >= PRECISION_LENIENT_MIN)
              and (hi is None or hi < CER_CI_MAX)
              and (micro["recall_fix"] or 0) >= (micro["recall_base"] or 0))
    else:
        # recall 은 fuzzy(≥85) 를 인정해 기준선이 이미 0.97~1.0 이라 '상승' 을 요구하면 천장에 막힌다 → 비하락 + exact 상승 또는 TP>0
        ok = (mean_fix >= mean_base and exact_fix >= exact_base and (lenient is not None and lenient >= PRECISION_MIN)
              and not worse and tp + tv > 0)
    return {"base": base, "min_confidence": min_confidence,
            "mean_recall_base": round(mean_base, 4), "mean_recall_fix": round(mean_fix, 4),
            "mean_exact_base": round(exact_base, 4), "mean_exact_fix": round(exact_fix, 4),
            "mean_cer_base": round(sum(r["base_cer"] for r in rs) / len(rs), 4),
            "mean_cer_fix": round(sum(r["fix_cer"] for r in rs) / len(rs), 4),
            "n_cases": len(rs), "micro": micro,
            "n_applied": sum(r["n_applied"] for r in rs), "tp": tp, "fp": fp, "tp_variant": tv,
            "neutral": sum(r["neutral"] for r in rs), "precision": strict, "precision_lenient": lenient,
            "cer_worse_cases": worse, "pass": ok}


def fp_diagnosis(rows: list[dict[str, Any]], catalog: Catalog) -> list[dict[str, Any]]:
    """FP 를 정답 발화에 대고 판독한다 — 환각인지, 카탈로그 구멍/표제 규약인지 가른다."""
    out: list[dict[str, Any]] = []
    for r in rows:
        for c in r["corrections"]:
            if c.get("verdict") != "fp" or not c.get("seg_ref"):
                continue
            ref = c["seg_ref"]
            if norm_chars(c["original"]) in norm_chars(ref):
                d = "**원문이 정답** — 멀쩡한 어절을 건드렸다"
            elif match_keyword(c["replacement"], ref).status == "fuzzy":
                # 정답 발화에 그 표기와 발음이 닮은 말이 실제로 있다 = 용어는 맞고 표기가 카탈로그 표제와 다르다
                d = "정답에 닮은 말 있음(자모 fuzzy ≥85) — 카탈로그 표제 규약/구멍 후보"
            else:
                d = "닮은 말 없음(<85) — 환각이거나 카탈로그 구멍, 정답 발화를 보고 판단"
            out.append({**c, "diagnosis": d})
    return sorted(out, key=lambda x: -x["confidence"])


def catalog_queue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`replacement_not_in_catalog` 로 거부된 제안 = 카탈로그 구멍 후보. 빈도순으로 모은다."""
    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        for c in r["corrections"]:
            if c.get("status") != "rejected" or c.get("why") != "replacement_not_in_catalog":
                continue
            q = agg.setdefault(c["replacement"], {"replacement": c["replacement"], "n": 0, "category": c.get("category"),
                                                  "originals": [], "reason": c.get("reason", "")})
            q["n"] += 1
            if c["original"] not in q["originals"]:
                q["originals"].append(c["original"])
    return sorted(agg.values(), key=lambda q: (-q["n"], q["replacement"]))


def write_report(out_dir: Path, rows: list[dict[str, Any]], verdicts: list[dict[str, Any]],
                 sweep: list[dict[str, Any]], catalog: Catalog, args: argparse.Namespace) -> None:
    lines = ["# 용어 오청 복구(LLM 사후 교정) 실험 결과", "",
             f"- 카탈로그: `{args.catalog}` ({len(catalog)} 용어, 품종 제외) · provider={args.provider or 'env'} "
             f"· min_confidence={args.min_confidence} · max_per_segment={args.max_per_segment}", ""]
    for v in verdicts:
        lines += [f"## 팔 `{v['base']}` → `{v['base']}+fix` — {'**통과**' if v['pass'] else '**미통과**'}", "",
                  "| 지표 | 기준 | +fix |", "|---|---:|---:|",
                  f"| 용어 recall 평균(exact+fuzzy) | {v['mean_recall_base']:.4f} | {v['mean_recall_fix']:.4f} |",
                  f"| 용어 exact 인식률 평균 | {v['mean_exact_base']:.4f} | {v['mean_exact_fix']:.4f} |",
                  f"| CER 평균 | {v['mean_cer_base']:.4f} | {v['mean_cer_fix']:.4f} |",
                  f"| 치환 TP / TP-이형 / FP / neutral | — | {v['tp']} / {v['tp_variant']} / {v['fp']} / {v['neutral']} |",
                  f"| 치환 precision strict / lenient(≥{PRECISION_MIN}) | — | {_fmt(v['precision'])} / {_fmt(v['precision_lenient'])} |",
                  f"| CER 악화 케이스 | — | {', '.join(v['cer_worse_cases']) or '없음'} |", ""]
        m = v.get("micro") or {}
        if m.get("occurrences"):
            b = m.get("boot") or {}
            lines += [f"### 발생 단위(micro) — 사전 등록 판정 좌표 · 골드 발생 {m['occurrences']}건", "",
                      "| 지표 | 기준 | +fix | 사전 등록 기준 |", "|---|---:|---:|---|",
                      f"| 도메인 용어 recall(exact+fuzzy) | {_fmt(m['recall_base'])} | {_fmt(m['recall_fix'])} | ≥ {MICRO_RECALL_MIN} |",
                      f"| 도메인 용어 exact | {_fmt(m['exact_base'])} | {_fmt(m['exact_fix'])} | 비하락 |",
                      f"| 치환 precision(lenient) | — | {_fmt(v['precision_lenient'])} | ≥ {PRECISION_LENIENT_MIN} |",
                      f"| CER(공백제거, micro) | {_fmt(m['cer_base'])} | {_fmt(m['cer_fix'])} | — |",
                      f"| Δ CER 페어드 CI95 | — | {b.get('delta', '—')} [{b.get('lo', '—')}, {b.get('hi', '—')}] | 상한 < +{CER_CI_MAX} |", ""]
        lines += ["| 케이스 | recall 기준→fix | exact 기준→fix | CER 기준→fix | 적용/거부 | TP/이형/FP | 토큰(in/out) | 초 |",
                  "|---|---|---|---|---:|---|---|---:|"]
        for r in rows:
            if r["base"] != v["base"]:
                continue
            if m.get("occurrences") and not (r.get("base_occ_total") or r["n_applied"] or r["n_rejected"]):
                continue        # 실통화 세트: 골드도 제안도 없는 통화는 표에서 뺀다(84통화 중 다수)
            lines.append(f"| {r['case']} | {r['base_recall']:.3f}→{r['fix_recall']:.3f} | "
                         f"{r['base_exact']:.3f}→{r['fix_exact']:.3f} | "
                         f"{r['base_cer']:.4f}→{r['fix_cer']:.4f} | {r['n_applied']}/{r['n_rejected']} | "
                         f"{r['tp']}/{r['tp_variant']}/{r['fp']} | {r['prompt_tokens']}/{r['completion_tokens']} | {r['elapsed_s']} |")
        lines += ["", "### 치환 내역", "", "| 케이스 | #seg | 원문 → 치환 | 카테고리 | conf | 판정 | 근거 |",
                  "|---|---:|---|---|---:|---|---|"]
        for r in rows:
            if r["base"] != v["base"]:
                continue
            for c in r["corrections"]:
                tag = c.get("verdict") or f"rejected:{c.get('why')}"
                lines.append(f"| {r['case']} | {c['seg_id']} | {c['original']} → {c['replacement']} | {c.get('category') or ''} "
                             f"| {c['confidence']:.2f} | {tag} | {c.get('reason', '')[:80]} |")
        lines.append("")
    if sweep:
        lines += ["## 신뢰도 하한 스윕 (같은 LLM 제안, 가드만 재적용)", "",
                  "| 팔 | min_conf | 적용 | TP/이형/FP | precision strict/lenient | recall | exact | CER | 악화 | 판정 |",
                  "|---|---:|---:|---|---|---|---|---|---|---|"]
        for v in sweep:
            lines.append(f"| {v['base']} | {v['min_confidence']} | {v['n_applied']} | {v['tp']}/{v['tp_variant']}/{v['fp']} | "
                         f"{_fmt(v['precision'])}/{_fmt(v['precision_lenient'])} | {v['mean_recall_base']:.4f}→{v['mean_recall_fix']:.4f} | "
                         f"{v['mean_exact_base']:.4f}→{v['mean_exact_fix']:.4f} | "
                         f"{v['mean_cer_base']:.4f}→{v['mean_cer_fix']:.4f} | {', '.join(v['cer_worse_cases']) or '없음'} | "
                         f"{'PASS' if v['pass'] else 'FAIL'} |")
        lines.append("")
    fps = fp_diagnosis(rows, catalog)
    if fps:
        lines += ["## 오탐이 가리키는 카탈로그 문제", "",
                  "치환이 FP(대본에 없는 표기)로 판정된 자리에서 **정답 발화가 실제로 무엇이었는지** 나란히 놓은 것이다. "
                  "판독은 기계적 힌트일 뿐이니 정답 발화 열을 직접 읽어라 — 정답 쪽 말이 카탈로그에 없으면 환각이 아니라 "
                  "**카탈로그 구멍**이고(모델은 가장 닮은 표제를 고를 수밖에 없다), 있으면 표제 규약(회사 접두·숫자 접미)이다.", "",
                  "| conf | 치환 | 정답 발화 | 판독 |", "|---:|---|---|---|"]
        for f in fps:
            lines.append(f"| {f['confidence']:.2f} | {f['original']} → {f['replacement']} | {f['seg_ref'][:60]} | {f['diagnosis']} |")
        lines.append("")
    queue = catalog_queue(rows)
    if queue:
        lines += ["## 카탈로그 후보 큐", "",
                  "LLM 이 냈지만 `replacement_not_in_catalog` 로 거부된 치환 — 카탈로그에 그 표기가 없어서 못 고친 것들이다. "
                  "빈도순. `jinong_gpu` 카탈로그 보강 입력으로 쓴다(`catalog_queue.tsv` 동일 내용).", "",
                  "| 후보 표기 | 횟수 | 카테고리(LLM) | 원문 예시 | 근거 |", "|---|---:|---|---|---|"]
        for q in queue:
            lines.append(f"| {q['replacement']} | {q['n']} | {q['category'] or ''} | "
                         f"{', '.join(q['originals'][:4])} | {q['reason'][:70]} |")
        lines.append("")
    lines += ["## 읽는 법", "",
              "- recall 은 대본에 실제 발화된 핵심어(expect_keywords)의 exact/fuzzy 인식률(`stt_score.match_keyword`).",
              "- TP = 바꾼 표기가 대본에 있고 원문 표기는 없음 · TP-이형 = 바꾼 표기가 대본에 exact 로는 없지만 자모 fuzzy(≥85)로는 있음"
              "(잿빛곰팡이병 ↔ '잿빛곰팡이') · FP = 대본에 없음 · neutral = 원문·치환 둘 다 대본에 있음.",
              "- exact 인식률 = fuzzy 를 빼고 exact 만 인정한 핵심어 인식률 — 매핑 단계가 실제로 덕 보는 지표.",
              "- precision strict = TP/(TP+FP), lenient = (TP+이형)/(TP+이형+FP). 판정은 lenient 기준(표기 규약은 이 실험의 대상이 아님).",
              "- CER 은 참고치(공백·구두점 제거 = `jinong_gpu` 의 결정 좌표인 공백제거 CER). 표기 규약 상쇄가 남는다.",
              f"- **판정 단위는 세트가 정한다.** 발생 단위 골드가 실린 세트(실통화)는 micro 표가 판정이다"
              f"(recall ≥ {MICRO_RECALL_MIN} ∧ lenient ≥ {PRECISION_LENIENT_MIN} ∧ ΔCER CI 상한 < +{CER_CI_MAX} ∧ recall 비하락). "
              f"골드가 통화 단위뿐인 대본 세트는 케이스 평균으로 판정한다(recall·exact 비하락 ∧ TP+이형>0 ∧ lenient≥{PRECISION_MIN} ∧ CER 악화 0).",
              "- micro recall 은 발화별 골드를 그 발화 텍스트에 대고 잰다 — 같은 용어를 5번 말했는데 1번 놓친 것도 보인다"
              "(통화 단위 매칭은 못 본다).", ""]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


async def amain(args: argparse.Namespace, llm: Any | None = None) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    catalog = load_catalog(Path(args.catalog), Path(args.stopwords) if args.stopwords else None)
    if not len(catalog):
        print(f"카탈로그가 비었다: {args.catalog}", file=sys.stderr)
        return 2
    fixtures = load_fixtures(Path(args.fixtures), args.cases.split(",") if args.cases else None)
    if not fixtures:
        print(f"픽스처 없음: {args.fixtures}", file=sys.stderr)
        return 2
    llm = llm or make_llm(args)
    arms = args.arms.split(",")
    thresholds = [float(x) for x in args.sweep.split(",")] if args.sweep else []
    rows: list[dict[str, Any]] = []
    sweep_rows: dict[float, list[dict[str, Any]]] = {t: [] for t in thresholds}
    for base in arms:
        for fx in fixtures:
            if base not in fx or not fx[base].get("segments"):
                log.warning("%s: 팔 %s 없음 — 건너뜀", fx["case"], base)
                continue
            log.info("%s / %s", fx["case"], base)
            prop = await proposals_for(llm, fx, base, catalog, out_dir, args)
            b, f, changed = score_fixture(fx, base, prop, catalog, min_confidence=args.min_confidence,
                                          max_per_segment=args.max_per_segment)
            (out_dir / fx["case"] / f"{base}.result.json").write_text(
                json.dumps({"base": b.to_dict(), "fix": f.to_dict(), "changed_segments": changed},
                           ensure_ascii=False, indent=1), encoding="utf-8")
            rows.append(row_of(fx["case"], base, b, f))
            for t in thresholds:
                b2, f2, _ = score_fixture(fx, base, prop, catalog, min_confidence=t, max_per_segment=args.max_per_segment)
                sweep_rows[t].append(row_of(fx["case"], base, b2, f2))
    verdicts = [v for v in (verdict(rows, b, args.min_confidence) for b in arms) if v]
    sweep = [v for t in thresholds for v in (verdict(sweep_rows[t], b, t) for b in arms) if v]
    (out_dir / "summary.json").write_text(json.dumps(
        {"rows": rows, "verdicts": verdicts, "sweep": sweep, "catalog_terms": len(catalog),
         "catalog_queue": catalog_queue(rows),
         "args": {k: str(v) for k, v in vars(args).items()}}, ensure_ascii=False, indent=1), encoding="utf-8")
    queue = catalog_queue(rows)
    with (out_dir / "catalog_queue.tsv").open("w", encoding="utf-8") as fh:
        fh.write("replacement\tn\tcategory\toriginals\treason\n")
        for q in queue:
            fh.write(f"{q['replacement']}\t{q['n']}\t{q['category'] or ''}\t{'|'.join(q['originals'])}\t{q['reason']}\n")
    write_report(out_dir, rows, verdicts, sweep, catalog, args)
    for v in verdicts + sweep:
        m = v.get("micro") or {}
        if m.get("occurrences"):
            b = m.get("boot") or {}
            print(f"[{v['base']} conf≥{v['min_confidence']}] micro n={m['occurrences']}  "
                  f"recall {_fmt(m['recall_base'])}→{_fmt(m['recall_fix'])}  exact {_fmt(m['exact_base'])}→{_fmt(m['exact_fix'])}  "
                  f"CER {_fmt(m['cer_base'])}→{_fmt(m['cer_fix'])} Δ{b.get('delta')} [{b.get('lo')},{b.get('hi')}]  "
                  f"TP/이형/FP {v['tp']}/{v['tp_variant']}/{v['fp']}  precision {_fmt(v['precision'])}/{_fmt(v['precision_lenient'])}  "
                  f"→ {'PASS' if v['pass'] else 'FAIL'}")
            continue
        print(f"[{v['base']} conf≥{v['min_confidence']}] recall {v['mean_recall_base']:.4f}→{v['mean_recall_fix']:.4f}  "
              f"exact {v['mean_exact_base']:.4f}→{v['mean_exact_fix']:.4f}  "
              f"CER {v['mean_cer_base']:.4f}→{v['mean_cer_fix']:.4f}  TP/이형/FP {v['tp']}/{v['tp_variant']}/{v['fp']}  "
              f"precision {_fmt(v['precision'])}/{_fmt(v['precision_lenient'])}  악화 {v['cer_worse_cases'] or '없음'}  "
              f"→ {'PASS' if v['pass'] else 'FAIL'}")
    print(f"리포트: {out_dir / 'report.md'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.agents.voice_eval.term_fix", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fixtures", required=True, help="ctx_replay 픽스처 디렉터리(*.json)")
    p.add_argument("--catalog", default=os.environ.get("TERM_CATALOG_PATH") or str(DEFAULT_CATALOG))
    p.add_argument("--stopwords", default=os.environ.get("TERM_STOPWORDS_PATH") or "",
                   help="불용어 파일(기본: 카탈로그 옆 stopwords_v3top5k.txt 가 있으면 그것)")
    p.add_argument("--cases", default="")
    p.add_argument("--arms", default="pass1,pass2", help="기준 팔: pass1(1패스) / pass2(:8105 2패스 raw)")
    p.add_argument("--provider", default="", help="gemini | openai | jinong | fake (비우면 .env 의 LLM_PROVIDER)")
    p.add_argument("--model", default="")
    p.add_argument("--mode", default="auto")
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--min-confidence", type=float, default=0.8)
    p.add_argument("--sweep", default="", help="신뢰도 하한 스윕, 예: 0.7,0.8,0.9 (LLM 재호출 없음)")
    p.add_argument("--max-per-segment", type=int, default=3)
    p.add_argument("--out", default="out/term-fix")
    p.add_argument("--force", action="store_true", help="LLM 제안 캐시 무시하고 재호출")
    p.add_argument("--dump-prompts", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None, llm: Any | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not args.stopwords:
        cand = Path(args.catalog).with_name("stopwords_v3top5k.txt")
        args.stopwords = str(cand) if cand.exists() else ""
    return asyncio.run(amain(args, llm))


if __name__ == "__main__":
    sys.exit(main())
