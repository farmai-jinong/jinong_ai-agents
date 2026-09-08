"""카탈로그·가드 처방 비교 — 무엇을 고치면 후보정이 사전 등록 기준을 넘는가.

    python -m app.agents.voice_eval.term_fix.tune --run out/term-fix-calls-base \
        --fixtures out/term-fix-calls/fixtures/base [--recipes R0,R1,R2,R3,ALL] [--min-confidence 0.9]
    python -m app.agents.voice_eval.term_fix.tune ... --confirm R123      # 이긴 처방을 LLM 재호출로 확정

실통화 84통화 측정(`docs/stt-term-fix-calls-2026-09-08.md`)에서 후보정은 용어를 복구하지만(exact .8071→.9143)
치환 precision 이 .8298 로 사전 등록 .90 에 못 미쳐 종결됐고, 그 오탐 8건 중 6건이 **카탈로그 문제**였다.
이 하네스는 그 카탈로그·가드 처방을 `jinong_gpu` stt-041 의 처방 비교표와 같은 방식으로 잰다.

**2단 구조 — 이게 이 하네스의 전부다.**
1. **스크리닝(기본, LLM 0콜, 초 단위)**: 캐시된 LLM 제안은 그대로 두고 카탈로그·가드만 바꿔 재적용·재채점한다.
   `팜한농포리캡탄`의 canonical 을 `포리캡탄`으로 바꾸면 그 치환이 FP 에서 TP 로 도는지 같은 질문에 즉답한다.
2. **확정(`--confirm`)**: 카탈로그가 바뀌면 **프롬프트도 바뀐다**(카탈로그를 통째로 넣으므로). 스크리닝은
   'LLM 이 같은 제안을 했다면' 의 반사실이므로, 채택 후보는 반드시 LLM 재호출로 다시 재야 한다.

처방(조합은 이름을 붙여 쓴다 — `R12`, `ALL`):

| id | 무엇을 | 왜 |
|---|---|---|
| `R0` | 그대로 | 기준 |
| `R1` | 농약상표의 회사명 접두를 떼어 canonical 로(+ 맨 표기 ≥3자는 표제 추가) | 전사자는 `포리캡탄`이라 쓰는데 표제가 `팜한농포리캡탄` — stt-041 처방 ①(법인 접두)이 손대지 않은 87건 |
| `R1a` | R1 중 canonical 재작성만(표제 추가 없음) | 새 매칭 표면을 하나도 만들지 않는 보수 변형 |
| `R2` | 가드: 원문이 카탈로그의 다른 용어면 거부 | 멀쩡히 알아들은 상표를 닮은 표제로 바꾸는 것(`마세트`→`마세트300`)을 막는다 |
| `R3` | 카탈로그 구멍 보강(`catalog_gaps.tsv`) | 정답 상표가 표제에 없으면 모델은 가장 닮은 표제를 고를 수밖에 없다 |
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from ...term_fix.apply import apply_corrections
from ...term_fix.catalog import Catalog, load_catalog, norm_chars
from ...term_fix.schemas import TermCorrection
from . import __main__ as cli
from .score import micro_cer, paired_boot, score_arm

log = logging.getLogger("voice_eval.term_fix.tune")

GAPS = Path(__file__).with_name("catalog_gaps.tsv")
MIN_BARE_LEN = 3            # 회사명을 뗀 맨 표기가 이보다 짧으면 새 표제로 만들지 않는다(오탐원)
RECIPES = {
    "R0": (),
    "R1": ("company_prefix",),
    "R1a": ("company_prefix_canonical_only",),
    "R2": ("guard_catalog_originals",),
    "R3": ("add_gaps",),
    "R12": ("company_prefix", "guard_catalog_originals"),
    "R13": ("company_prefix", "add_gaps"),
    "R23": ("guard_catalog_originals", "add_gaps"),
    "ALL": ("company_prefix", "guard_catalog_originals", "add_gaps"),
}


# --------------------------------------------------------------------------- 카탈로그 변형
def read_catalog_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def company_prefixes(rows: list[dict[str, Any]]) -> list[str]:
    """회사 카테고리 표기 중 접두로 쓸 만한 것(2자 이상), 긴 것부터."""
    return sorted({str(r["term"]) for r in rows if r.get("category") == "company" and len(str(r["term"])) >= 2},
                  key=len, reverse=True)


def strip_company_prefix(rows: list[dict[str, Any]], *, add_bare: bool) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """농약상표의 회사명 접두를 떼어 canonical(=치환·주입 표기)로 만든다.

    `term`(매칭 표면)은 건드리지 않는다 — 모델이 `팜한농포리캡탄`을 내도 `포리캡탄`으로 적힌다.
    `add_bare` 면 맨 표기(≥`MIN_BARE_LEN`, 아직 표제에 없는 것)를 표제로도 추가해 모델이 그 표기를 낼 수 있게 한다.
    """
    comps = company_prefixes(rows)
    have = {(str(r.get("term")), r.get("category")) for r in rows}
    out = copy.deepcopy(rows)
    changes: list[tuple[str, str, str]] = []
    extra: list[dict[str, Any]] = []
    for r in out:
        if r.get("category") != "pesticide_brand":
            continue
        term = str(r.get("term") or "")
        for c in comps:
            if term.startswith(c) and len(term) > len(c) + 1:
                bare = term[len(c):]
                r["canonical"] = bare
                changes.append((term, c, bare))
                if add_bare and len(bare) >= MIN_BARE_LEN and (bare, "pesticide_brand") not in have:
                    extra.append({"term": bare, "category": "pesticide_brand"})
                    have.add((bare, "pesticide_brand"))
                break
    return out + extra, changes


def load_gaps(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip():
            rows.append({"term": parts[0].strip(), "category": parts[1].strip()})
    return rows


def build_variant(base_rows: list[dict[str, Any]], steps: tuple[str, ...], gaps: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows, meta = base_rows, {"steps": list(steps)}
    if "company_prefix" in steps or "company_prefix_canonical_only" in steps:
        rows, changes = strip_company_prefix(rows, add_bare="company_prefix" in steps)
        meta["company_prefix_changed"] = len(changes)
        meta["company_prefix_added"] = len(rows) - len(base_rows)
        meta["company_prefix_sample"] = [f"{a}→{c}" for a, _, c in changes[:6]]
    if "add_gaps" in steps:
        g = load_gaps(gaps)
        have = {(str(r.get("term")), r.get("category")) for r in rows}
        add = [x for x in g if (x["term"], x["category"]) not in have]
        rows = rows + add
        meta["gaps_added"] = len(add)
        meta["gaps_skipped"] = len(g) - len(add)
    return rows, meta


# --------------------------------------------------------------------------- 채점
def load_case(fx_dir: Path, run_dir: Path, arm: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(픽스처, 캐시된 LLM 제안) — 제안이 없는 케이스는 건너뛴다."""
    out = []
    for p in sorted(fx_dir.glob("*.json")):
        fx = json.loads(p.read_text(encoding="utf-8"))
        fx.setdefault("case", p.stem)
        cache = run_dir / fx["case"] / f"{arm}.proposals.json"
        if cache.exists():
            out.append((fx, json.loads(cache.read_text(encoding="utf-8"))))
    return out


def score_recipe(cases: list[tuple[dict[str, Any], dict[str, Any]]], catalog: Catalog, arm: str, *,
                 min_confidence: float, max_per_segment: int, guard_originals: bool,
                 iters: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for fx, prop in cases:
        expect, _ = cli.case_meta(fx["case"], fx)
        reference, keywords = fx["reference"], list(fx.get("expect_keywords") or [])
        segments = list(fx[arm]["segments"])
        b = score_arm(arm, cli.join_text(segments), reference, keywords, expect, segments=segments)
        proposals = [TermCorrection(**c) for c in prop["corrections"]]
        fixed, applied = apply_corrections(segments, proposals, catalog, min_confidence=min_confidence,
                                           max_per_segment=max_per_segment,
                                           reject_catalog_originals=guard_originals)
        f = score_arm(f"{arm}+fix", cli.join_text(fixed), reference, keywords, expect, applied, segments=fixed)
        for row in f.corrections:
            seg = segments[row["seg_id"]] if 0 <= row["seg_id"] < len(segments) else {}
            if seg.get("ref"):
                row["seg_ref"] = seg["ref"]
        rows.append(cli.row_of(fx["case"], arm, b, f))
    v = cli.verdict(rows, arm, min_confidence, iters=iters)
    v["rows"] = rows
    return v


# --------------------------------------------------------------------------- 리포트
def write_report(out: Path, results: list[dict[str, Any]], args: argparse.Namespace) -> None:
    lines = ["# 카탈로그·가드 처방 비교 (LLM 재호출 없이 가드만 재적용)", "",
             f"- 입력 `{args.run}` 의 캐시된 제안 · 픽스처 `{args.fixtures}` · 팔 `{args.arm}` · min_confidence={args.min_confidence}",
             f"- 기준 카탈로그 `{args.catalog}`", "",
             "**이 표는 반사실이다** — 카탈로그를 바꾸면 프롬프트도 바뀌므로 LLM 이 같은 제안을 낸다는 보장이 없다. "
             "채택 후보는 `--confirm <id>` 로 LLM 재호출 확정 측정을 해야 한다.", "",
             "| 처방 | 카탈로그 | 적용 | TP/이형/FP | precision strict/lenient | 용어 recall | exact | CER(공백제거) | ΔCER CI95 | 판정 |",
             "|---|---:|---:|---|---|---|---|---|---|---|"]
    for r in results:
        m, b, meta = r["micro"], (r["micro"].get("boot") or {}), r["meta"]
        note = []
        if meta.get("company_prefix_changed"):
            note.append(f"접두 {meta['company_prefix_changed']}건 +표제 {meta.get('company_prefix_added', 0)}")
        if meta.get("gaps_added"):
            note.append(f"구멍 +{meta['gaps_added']}")
        if "guard_catalog_originals" in meta["steps"]:
            note.append("원문가드")
        lines.append(
            f"| **{r['id']}** {r['title']} | {r['catalog_terms']} ({', '.join(note) or '그대로'}) | {r['n_applied']} | "
            f"{r['tp']}/{r['tp_variant']}/{r['fp']} | {cli._fmt(r['precision'])}/{cli._fmt(r['precision_lenient'])} | "
            f"{cli._fmt(m['recall_base'])}→{cli._fmt(m['recall_fix'])} | {cli._fmt(m['exact_base'])}→{cli._fmt(m['exact_fix'])} | "
            f"{cli._fmt(m['cer_base'])}→{cli._fmt(m['cer_fix'])} | {b.get('delta')} [{b.get('lo')}, {b.get('hi')}] | "
            f"{'**PASS**' if r['pass'] else 'FAIL'} |")
    lines += ["", f"판정 기준(사전 등록): 용어 recall ≥ {cli.MICRO_RECALL_MIN} ∧ precision(lenient) ≥ "
                  f"{cli.PRECISION_LENIENT_MIN} ∧ ΔCER CI 상한 < +{cli.CER_CI_MAX} ∧ recall 비하락.", ""]
    base = next((r for r in results if r["id"] == "R0"), None)
    if base:
        lines += ["## R0 대비 오탐 변화", ""]
        b_fp = {(c["case"], x["seg_id"], x["original"], x["replacement"])
                for c in [{"case": r["case"], "rows": r} for r in base["rows"]] for x in c["rows"]["corrections"]
                if x.get("verdict") == "fp"}
        for r in results:
            if r["id"] == "R0":
                continue
            fps = {(row["case"], x["seg_id"], x["original"], x["replacement"])
                   for row in r["rows"] for x in row["corrections"] if x.get("verdict") == "fp"}
            gone = sorted(b_fp - fps)
            new = sorted(fps - b_fp)
            lines.append(f"- **{r['id']}**: 사라진 오탐 {len(gone)}"
                         + (" (" + ", ".join(f"`{o}→{p}`" for _, _, o, p in gone[:8]) + ")" if gone else "")
                         + f" · 새 오탐 {len(new)}"
                         + (" (" + ", ".join(f"`{o}→{p}`" for _, _, o, p in new[:8]) + ")" if new else ""))
        lines.append("")
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.agents.voice_eval.term_fix.tune", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", required=True, help="LLM 제안 캐시가 있는 실행 디렉터리(out/term-fix-calls-base)")
    p.add_argument("--fixtures", required=True)
    p.add_argument("--arm", default="pass1")
    p.add_argument("--catalog", default=str(cli.DEFAULT_CATALOG))
    p.add_argument("--stopwords", default="")
    p.add_argument("--gaps", default=str(GAPS))
    p.add_argument("--recipes", default="R0,R1,R1a,R2,R3,R12,R13,R23,ALL")
    p.add_argument("--min-confidence", type=float, default=0.9)
    p.add_argument("--max-per-segment", type=int, default=3)
    p.add_argument("--iters", type=int, default=cli.BOOT_ITERS)
    p.add_argument("--out", default="out/term-fix-tune")
    p.add_argument("--confirm", default="", help="처방 id — 그 카탈로그로 LLM 을 다시 불러 확정 측정한다")
    p.add_argument("--keep-cache", action="store_true",
                   help="확정 측정에서 LLM 을 다시 부르지 않고 기존 제안 캐시로 재채점(가드만 바꿔 다시 잴 때)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None, llm: Any | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cat_path = Path(args.catalog)
    sw = Path(args.stopwords) if args.stopwords else cat_path.with_name("stopwords_v3top5k.txt")
    sw = sw if sw.exists() else None
    base_rows = read_catalog_rows(cat_path)
    cases = load_case(Path(args.fixtures), Path(args.run), args.arm)
    if not cases:
        print(f"제안 캐시가 없다: {args.run}", file=sys.stderr)
        return 2
    log.info("케이스 %d · 카탈로그 원본 %d행", len(cases), len(base_rows))

    ids = [x.strip() for x in args.recipes.split(",") if x.strip()]
    unknown = [i for i in ids if i not in RECIPES]
    if unknown:
        print(f"모르는 처방: {unknown} (가능: {list(RECIPES)})", file=sys.stderr)
        return 2
    if args.confirm and args.confirm not in RECIPES:
        print(f"모르는 처방: {args.confirm}", file=sys.stderr)
        return 2

    results = []
    for rid in ids:
        steps = RECIPES[rid]
        rows, meta = build_variant(base_rows, steps, Path(args.gaps))
        vpath = out / f"catalog-{rid}.jsonl"
        vpath.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        catalog = load_catalog(vpath, sw)
        v = score_recipe(cases, catalog, args.arm, min_confidence=args.min_confidence,
                         max_per_segment=args.max_per_segment,
                         guard_originals="guard_catalog_originals" in steps, iters=args.iters)
        v.update({"id": rid, "title": "", "meta": meta, "catalog_terms": len(catalog), "catalog_path": str(vpath)})
        results.append(v)
        m = v["micro"]
        log.info("%s: 적용 %d · TP/이형/FP %d/%d/%d · lenient %s · recall %s · %s", rid, v["n_applied"],
                 v["tp"], v["tp_variant"], v["fp"], cli._fmt(v["precision_lenient"]), cli._fmt(m["recall_fix"]),
                 "PASS" if v["pass"] else "FAIL")

    (out / "summary.json").write_text(json.dumps(
        {"results": [{k: x[k] for k in x if k != "rows"} for x in results],
         "args": {k: str(v) for k, v in vars(args).items()}}, ensure_ascii=False, indent=1), encoding="utf-8")
    write_report(out, results, args)
    print(f"리포트: {out / 'report.md'}")

    if args.confirm:
        vpath = out / f"catalog-{args.confirm}.jsonl"
        conf_out = out / f"confirm-{args.confirm}"
        print(f"\n확정 측정(LLM 재호출): {args.confirm} · 카탈로그 {vpath}")
        argv = ["--fixtures", args.fixtures, "--arms", args.arm, "--catalog", str(vpath),
                "--stopwords", str(sw) if sw else "", "--min-confidence", str(args.min_confidence),
                "--out", str(conf_out)]
        if "guard_catalog_originals" in RECIPES[args.confirm]:
            argv.append("--reject-catalog-originals")      # 가드도 처방의 일부다 — 빠뜨리면 확정치가 처방을 과소평가한다
        if not args.keep_cache:
            argv.append("--force")
        rc = cli.main(argv, llm=llm)
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
