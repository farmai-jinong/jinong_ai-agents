"""실통화 평가셋(jinong-call, 92통화 2,354발화)을 term_fix 하네스 픽스처로 반입한다.

    python -m app.agents.voice_eval.term_fix.import_calls [--out out/term-fix-calls]

`jinong_gpu` 원격에서 세 조각을 읽어 통화 단위로 되짜맞춘다(복사하지 않고 stdout 으로 끌어온다):

| 소스 | 원격 경로(BASE 기준) | 쓰는 필드 |
|---|---|---|
| 통화 그룹 | `tools/model-train/data/asr_eval/jinong-call/*.arrow` | `audio_path`=`<call_uuid>_<3자리>.wav`, `text`(정답) |
| 가설 덤프 | `tools/model-train/data/asr_eval_hyp/<덤프>/jinong-call.jsonl` | `{i, ref, hyp}` |
| 골드 용어 | `tools/model-train/data/asr_eval_hyp/jinong-call-gold.jsonl` | `{i, bias_positives}` |

덤프 행에는 `audio_path` 가 없다(`jinong_gpu` stt-044 세팅이 "선행: 덤프 audio_path" 라고 적은 이유).
`i` ↔ arrow row index 로 조인하고 `dump.ref == arrow.text` 를 전수 대조해 조인을 검증한다 — 한 행이라도
어긋나면 실패시킨다.

**골드**: 두 자가 있다.

- `--gold catalog`(기본) — 도메인 카테고리(농약상표·병·해충) 표제/canonical 중 **3자 이상**을 정답 대본에
  정규화 후 부분문자열로 찾되, 긴 것부터 겹침을 억제하고 `gold_denylist.tsv`(사람 검수)를 뺀다.
- `--gold bias` — 덤프 옆 `jinong-call-gold.jsonl` 의 `bias_positives` 를 카테고리로 갈라 도메인만 쓴다.
  09-08 오전 측정이 쓴 자다. **권장하지 않는다**: 그 목록은 품종 오탐이 46%(`아시`←"아시죠")인데다
  2자 일상어(`뿌리`×55·`영양`)를 넣고 실제 상표(`테라도플러스`·`담배거세미나방`)는 놓친다. 93통화에서
  두 자의 도메인 발생은 140(bias) vs 161(catalog)이고 catalog 자가 bias 를 완전히 포함한다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ...term_fix.catalog import load_catalog

log = logging.getLogger("voice_eval.term_fix.import")

DEFAULT_HOST = "jinong_gpu"
DEFAULT_BASE = "/NHNHOME/WORKSPACE/26mafra001_A/BASE/jinong"
DEFAULT_ARMS = ["base=deploy-base-k10-jinong", "champ=deploy-g55-w75-k10-jinong"]
DEFAULT_CATALOG = Path.home() / "dev/jinong/jinong_gpu/stt-serve/catalog/catalog.jsonl"

# 한 표기가 여러 카테고리에 걸리면 도메인 쪽을 이긴 것으로 본다(품종은 언제나 마지막).
CATEGORY_PRIORITY = ("pesticide_brand", "pest", "disease", "company", "crop_variety")
DOMAIN = ("pesticide_brand", "pest", "disease")

_WAV = re.compile(r"^(?P<call>.+)_(?P<seg>\d+)\.wav$")
_NONWORD = re.compile(r"[^0-9A-Za-z가-힣]+")

# --------------------------------------------------------------------------- 원격 추출
REMOTE = r'''
import json, os, sys, glob
import pyarrow as pa, pyarrow.ipc as ipc
BASE, ARMS, DS_DIR, NAME = sys.argv[1], sys.argv[2].split(","), sys.argv[3], sys.argv[4]
ds = DS_DIR if os.path.isabs(DS_DIR) else os.path.join(BASE, DS_DIR)
files = sorted(glob.glob(os.path.join(ds, "*.arrow")))
tabs = []
for f in files:
    with pa.memory_map(f) as src:
        tabs.append(ipc.open_stream(src).read_all())
t = pa.concat_tables(tabs) if len(tabs) > 1 else tabs[0]
paths, texts = t.column("audio_path").to_pylist(), t.column("text").to_pylist()
H = os.path.join(BASE, "tools/model-train/data/asr_eval_hyp")
hyps = {}
for spec in ARMS:
    name, dump = spec.split("=", 1)
    hyps[name] = {}
    dp = dump if os.path.isabs(dump) else os.path.join(H, dump)
    dp = dp if dp.endswith(".jsonl") else os.path.join(dp, NAME + ".jsonl")
    for line in open(dp, encoding="utf-8"):
        d = json.loads(line)
        hyps[name][str(d["i"])] = (d["ref"], d["hyp"])
gold = {}
gp = os.path.join(H, NAME + "-gold.jsonl")
if os.path.exists(gp):
    for line in open(gp, encoding="utf-8"):
        d = json.loads(line)
        gold[str(d["i"])] = d.get("bias_positives") or []
out = {"n_rows": len(paths), "arms": list(hyps), "rows": []}
for i, (p, ref) in enumerate(zip(paths, texts)):
    k = str(i)
    row = {"i": i, "audio": os.path.basename(p), "ref": ref, "gold": gold.get(k, []), "hyp": {}}
    for name in hyps:
        pair = hyps[name].get(k)
        row["hyp"][name] = {"ref": pair[0], "hyp": pair[1]} if pair else None
    out["rows"].append(row)
json.dump(out, sys.stdout, ensure_ascii=False)
'''


def fetch(host: str, base: str, arms: list[str], dataset_dir: str, name: str, timeout: float) -> dict[str, Any]:
    """원격에서 한 번에 끌어온다 — 12,596행 정도는 stdout 으로 충분하다(수 MB)."""
    cmd = ["ssh", "-o", "ConnectTimeout=10", host, "python3", "-", base, ",".join(arms), dataset_dir, name]
    log.info("원격 추출: %s %s", host, base)
    r = subprocess.run(cmd, input=REMOTE, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"원격 추출 실패(rc={r.returncode}): {r.stderr.strip()[-800:]}")
    return json.loads(r.stdout)


# --------------------------------------------------------------------------- 카탈로그 카테고리
def norm_chars(text: str) -> str:
    return _NONWORD.sub("", unicodedata.normalize("NFKC", text)).lower()


def term_categories(catalog: Path) -> dict[str, str]:
    """표기 → 카테고리(우선순위 적용). 골드 정제 전용이라 품종을 포함한 전 카테고리를 읽는다."""
    seen: dict[str, set[str]] = defaultdict(set)
    for line in catalog.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("term"):
            seen[d["term"]].add(d.get("category") or "?")
    rank = {c: i for i, c in enumerate(CATEGORY_PRIORITY)}
    return {t: sorted(cs, key=lambda c: rank.get(c, 99))[0] for t, cs in seen.items()}


GOLD_DENYLIST = Path(__file__).with_name("gold_denylist.tsv")
GOLD_MIN_LEN = 3        # 2자 표제는 일상어와 충돌한다(`사드`←"사드릴게요", `뿌리`, `영양`)


def read_denylist(path: Path) -> set[str]:
    out: set[str] = set()
    if not Path(path).exists():
        return out
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            out.add(line.split("\t")[0].strip())
    return out


def gold_keys(catalog: Path, stopwords: Path | None, denylist: set[str]) -> list[tuple[str, str]]:
    """(표기, 정규화형) — 도메인 카테고리 표제/canonical 중 3자 이상, 불용어·거부목록 제외. 긴 것부터."""
    cat = load_catalog(catalog, stopwords, categories=DOMAIN)
    keys: set[str] = set()
    for t in cat.terms:
        for k in (t.term, t.canonical or t.term):
            if len(k) >= GOLD_MIN_LEN and k not in denylist:
                keys.add(k)
    return sorted(((k, norm_chars(k)) for k in keys), key=lambda x: -len(x[0]))


def find_gold(ref: str, keys: list[tuple[str, str]]) -> list[str]:
    """정답 대본에서 도메인 용어를 찾는다 — 긴 것부터, 이미 잡힌 구간과 겹치면 억제."""
    rn = norm_chars(ref)
    used: list[tuple[int, int]] = []
    hits: list[str] = []
    for k, kn in keys:
        if len(kn) < GOLD_MIN_LEN or kn not in rn:
            continue
        i = rn.find(kn)
        if any(i < e and i + len(kn) > s for s, e in used):
            continue
        used.append((i, i + len(kn)))
        hits.append(k)
    return hits


def split_gold(terms: list[str], cats: dict[str, str]) -> dict[str, list[str]]:
    """골드 용어를 도메인 / 회사 / 품종 / 미상 으로 가른다."""
    out: dict[str, list[str]] = {"domain": [], "company": [], "variety": [], "unknown": []}
    for t in terms:
        c = cats.get(t)
        bucket = "domain" if c in DOMAIN else "company" if c == "company" else "variety" if c == "crop_variety" else "unknown"
        out[bucket].append(t)
    return out


# --------------------------------------------------------------------------- 조립
def group_calls(rows: list[dict[str, Any]], arm: str, cats: dict[str, str]) -> list[dict[str, Any]]:
    calls: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for r in rows:
        m = _WAV.match(r["audio"])
        if not m:
            raise RuntimeError(f"오디오 파일명이 <call>_<seg>.wav 형식이 아니다: {r['audio']}")
        calls[m["call"]].append((int(m["seg"]), r))
    out = []
    for call, items in sorted(calls.items()):
        items.sort(key=lambda x: x[0])
        segs, refs = [], []
        buckets: dict[str, list[str]] = {"domain": [], "company": [], "variety": [], "unknown": []}
        for _, r in items:
            hyp = (r["hyp"].get(arm) or {}).get("hyp")
            if hyp is None:
                raise RuntimeError(f"{call}: 팔 {arm} 의 행 {r['i']} 가 덤프에 없다")
            g = split_gold(r["gold"], cats)
            # 발화별 골드를 실어 둔다 — 통화 단위 매칭은 '5번 말한 용어 중 1번 놓침' 을 못 본다.
            segs.append({"speaker": None, "start": None, "end": None, "text": hyp,
                         "row_id": r["i"], "ref": r["ref"], "gold": g["domain"]})
            refs.append(r["ref"])
            for k, v in g.items():
                buckets[k].extend(v)
        # 통화 단위 매칭이라 같은 용어의 반복은 한 번만 센다(발생 수는 gold_occurrences 로 따로 남긴다)
        keywords = sorted(dict.fromkeys(buckets["domain"]))
        out.append({
            "case": call,
            "source": {"dataset": "jinong-call", "arm": arm, "call_id": call, "n_utts": len(items),
                       "row_ids": [r["i"] for _, r in items]},
            "reference": " ".join(refs),
            "expect_keywords": keywords,
            "gold": {k: sorted(dict.fromkeys(v)) for k, v in buckets.items()},
            "gold_occurrences": {k: len(v) for k, v in buckets.items()},
            "pass1": {"segments": segs},
        })
    return out


def write_gold_domain(path: Path, rows: list[dict[str, Any]], cats: dict[str, str]) -> dict[str, int]:
    """정제 골드를 원 스키마(`{i, ref, bias_positives}`)로 다시 낸다 — jinong_gpu stt-043 에 그대로 넘길 수 있게."""
    tally: Counter[str] = Counter()
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            b = split_gold(r["gold"], cats)
            for k, v in b.items():
                tally[k] += len(v)
            fh.write(json.dumps({"i": r["i"], "ref": r["ref"], "bias_positives": b["domain"],
                                 "dropped": {"company": b["company"], "variety": b["variety"],
                                             "unknown": b["unknown"]}}, ensure_ascii=False) + "\n")
    return dict(tally)


def headroom(rows: list[dict[str, Any]], arm: str, cats: dict[str, str]) -> dict[str, Any]:
    """발화 단위로 '정답 도메인 용어가 가설에 살아남았는가' — 이 실험의 헤드룸."""
    total = hit = 0
    misses: Counter[str] = Counter()
    for r in rows:
        h = (r["hyp"].get(arm) or {}).get("hyp") or ""
        hn = norm_chars(h)
        for t in split_gold(r["gold"], cats)["domain"]:
            total += 1
            if norm_chars(t) in hn:
                hit += 1
            else:
                misses[t] += 1
    return {"arm": arm, "occurrences": total, "hit": hit, "miss": total - hit,
            "recall": round(hit / total, 4) if total else None, "top_misses": misses.most_common(15)}


def write_report(out: Path, meta: dict[str, Any]) -> None:
    t, arms = meta["tally"], meta["headroom"]
    lines = [
        "# 실통화 평가셋(jinong-call) 반입 · 골드 정제", "",
        f"- 세트 `{meta['dataset']}` · 골드 자 `{meta['gold_rule']}` · 원격 `{meta['host']}` "
        f"· 행 {meta['n_rows']} · 통화 {meta['n_calls']} "
        f"· 픽스처 {meta['n_fixtures']}(발화 {meta['min_utts']}건 미만 {meta['n_skipped']}건 제외)",
        f"- 카탈로그 `{meta['catalog']}` · 조인 검증 `dump.ref == arrow.text` {meta['ref_match']}/{meta['n_rows']}", "",
        "## 골드 용어 정제", "",
        "| 버킷 | 발생 | 처리 |", "|---|---:|---|",
        f"| 농약상표·병·해충 (domain) | {t.get('domain', 0)} | **`expect_keywords` 로 채택** |",
        f"| 회사 (company) | {t.get('company', 0)} | 제외 — 표기 규약(`(주)팜한농` vs `팜한농`) 문제라 후보정 판정과 섞이면 안 된다 |",
        f"| 품종 (crop_variety) | {t.get('variety', 0)} | 제외 — 부분문자열 오탐(`아시`←\"아시죠\", `토마`, `하우스`) |",
        f"| 카탈로그 밖 (unknown) | {t.get('unknown', 0)} | 제외 |", "",
        f"정제 전 {sum(t.values())} → 정제 후 **{t.get('domain', 0)}**. `{meta['dataset']}-gold-domain.jsonl` 로 냈다."
        + ("  (`--gold catalog` 은 애초에 도메인 표제만 찾으므로 품종·회사 버킷이 0 이다.)" if meta['gold_rule'] == 'catalog' else ""), "",
        "## 팔별 헤드룸 (정제 골드 발생 단위, 발화 안에 정답 표기가 남아 있는가)", "",
        "| 팔 | 발생 | 살아남음 | 오청 | recall |", "|---|---:|---:|---:|---:|",
    ]
    for h in arms:
        lines.append(f"| {h['arm']} | {h['occurrences']} | {h['hit']} | {h['miss']} | {h['recall']:.4f} |")
    lines.append("")
    for h in arms:
        lines += [f"### {h['arm']} 오청 용어", "",
                  ", ".join(f"`{t}`×{n}" for t, n in h["top_misses"]) or "없음", ""]
    lines += ["## 다음", "", "```bash",
              "python -m app.agents.voice_eval.term_fix \\",
              f"  --fixtures {meta['out']}/fixtures/{(meta.get('arms') or ['base'])[0]} --arms pass1 \\",
              "  --provider gemini --min-confidence 0.8 --sweep 0.7,0.8,0.9 \\",
              "  --out out/term-fix-calls-base", "```", ""]
    (out / "import-report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.agents.voice_eval.term_fix.import_calls",
                                description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--base", default=os.environ.get("JINONG_GPU_BASE") or DEFAULT_BASE)
    p.add_argument("--arm", action="append", default=None,
                   help="<이름>=<덤프 디렉터리 또는 jsonl>, 반복 가능 (기본: deploy-base/champ)")
    p.add_argument("--dataset", default="jinong-call", help="벤치마크 이름 — 덤프 파일명·골드 파일명에 쓴다")
    p.add_argument("--dataset-dir", default="tools/model-train/data/asr_eval/jinong-call",
                   help="원격 arrow 디렉터리(BASE 상대 또는 절대)")
    p.add_argument("--gold", default="catalog", choices=["catalog", "bias"],
                   help="catalog=카탈로그 도메인 표제 매칭(권장) / bias=덤프 옆 bias_positives")
    p.add_argument("--stopwords", default="")
    p.add_argument("--denylist", default=str(GOLD_DENYLIST))
    p.add_argument("--catalog", default=os.environ.get("TERM_CATALOG_PATH") or str(DEFAULT_CATALOG))
    p.add_argument("--min-utts", type=int, default=3, help="이보다 발화가 적은 통화는 문맥이 없어 제외")
    p.add_argument("--out", default="out/term-fix-calls")
    p.add_argument("--raw", default="", help="원격 추출 결과 JSON 을 재사용(없으면 <out>/rows.json 에 캐시)")
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    arms = [a for a in (args.arm or DEFAULT_ARMS)]
    raw_path = Path(args.raw) if args.raw else out / "rows.json"
    if raw_path.exists():
        log.info("캐시 사용: %s", raw_path)
        data = json.loads(raw_path.read_text(encoding="utf-8"))
    else:
        data = fetch(args.host, args.base, arms, args.dataset_dir, args.dataset, args.timeout)
        raw_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    rows = data["rows"]
    # 조인 검증 — 덤프에 audio_path 가 없어 i 로 붙이므로 ref 전수 대조가 유일한 안전망이다
    match = 0
    for r in rows:
        pairs = [v for v in r["hyp"].values() if v]
        if pairs and all(v["ref"] == r["ref"] for v in pairs):
            match += 1
    if match != len(rows):
        print(f"조인 불일치: dump.ref == arrow.text 가 {match}/{len(rows)} — 반입 중단", file=sys.stderr)
        return 2
    log.info("조인 검증 통과: %d/%d", match, len(rows))

    cats = term_categories(Path(args.catalog))
    if args.gold == "catalog":
        cat_path = Path(args.catalog)
        sw = Path(args.stopwords) if args.stopwords else cat_path.with_name("stopwords_v3top5k.txt")
        keys = gold_keys(cat_path, sw if sw.exists() else None, read_denylist(Path(args.denylist)))
        log.info("골드 자: 카탈로그 도메인 표제 %d개(≥%d자, 거부목록 적용)", len(keys), GOLD_MIN_LEN)
        for r in rows:
            r["gold"] = find_gold(r["ref"], keys)
    tally = write_gold_domain(out / f"{args.dataset}-gold-domain.jsonl", rows, cats)
    heads = [headroom(rows, name.split("=", 1)[0], cats) for name in arms]

    n_calls = n_fixtures = n_skipped = 0
    for spec in arms:
        name = spec.split("=", 1)[0]
        d = out / "fixtures" / name
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*.json"):
            old.unlink()
        calls = group_calls(rows, name, cats)
        n_calls = len(calls)
        kept = [c for c in calls if c["source"]["n_utts"] >= args.min_utts]
        n_fixtures, n_skipped = len(kept), len(calls) - len(kept)
        for c in kept:
            (d / f"{c['case']}.json").write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("%s: 통화 %d → 픽스처 %d (%d 제외) → %s", name, len(calls), len(kept), n_skipped, d)

    meta = {"host": args.host, "base": args.base, "catalog": args.catalog, "out": args.out,
            "dataset": args.dataset, "gold_rule": args.gold,
            "n_rows": len(rows), "ref_match": match, "n_calls": n_calls, "n_fixtures": n_fixtures,
            "n_skipped": n_skipped, "min_utts": args.min_utts, "tally": tally, "headroom": heads}
    (out / "import-summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    write_report(out, meta)
    print(f"골드 정제: {sum(tally.values())} → domain {tally.get('domain', 0)} "
          f"(회사 {tally.get('company', 0)} · 품종 {tally.get('variety', 0)} 제외)")
    for h in heads:
        print(f"헤드룸 [{h['arm']}] {h['hit']}/{h['occurrences']} recall {h['recall']:.4f} · 오청 {h['miss']}건")
    print(f"리포트: {out / 'import-report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
