"""Prototype: same candidate semantics as ctx_retrieval.retrieve, two speedups measured separately.
 A) positional prefilter: a candidate cluster must contain >= n_bg - 2*maxd distinct term bigrams
    (the existing prefilter counts bigrams anywhere in the transcript, so 99.6% of the catalog passes).
 B) C-speed DP: rapidfuzz Levenshtein with score_cutoff over the prefix lengths instead of the Python DP.
"""
import sys, time, json
from bisect import bisect_left
from collections import defaultdict
sys.path.insert(0, "/Users/jang-yeongbin/dev/jinong/jinong_gpu/stt-serve")
import ctx_retrieval as cr
from rapidfuzz.distance import Levenshtein

def prefix_distance_rf(pat, text, maxd):
    """min Levenshtein(pat, text[:l]) over l — same contract as cr._best_prefix_distance."""
    lp = len(pat); best = None
    lo = max(1, lp - maxd); hi = min(len(text), lp + maxd)
    for l in range(lo, hi + 1):
        d = Levenshtein.distance(pat, text[:l], score_cutoff=maxd)
        if d <= maxd and (best is None or d < best[0]):
            best = (d, l)
            if d == 0: break
    return best

def make_retrieve(r, positional=False):
    """Copy of CtxRetriever.retrieve's candidate loop with an optional positional prefilter."""
    def retrieve(text, topk=10, category_caps=None, min_score=None, domain_fuzzy_min_jamo=0, domain_first=False):
        tj, syl_starts, word_starts = cr.to_jamo_with_bounds(text)
        if len(tj) < 2: return []
        ends_ok = syl_starts | {len(tj)}
        word_start_list = sorted(word_starts)
        pos_by_bigram = defaultdict(list)
        for i in range(len(tj) - 1): pos_by_bigram[tj[i:i + 2]].append(i)
        hits = defaultdict(set)
        for bg, positions in pos_by_bigram.items():
            for eid in r._index.get(bg, ()): hits[eid].add(bg)
        stats = {"cand": len(hits), "dp": 0, "clusters": 0}
        results = {}
        for eid, bgs in hits.items():
            j = r._jamo[eid]
            n_bg = len(set(j[i:i + 2] for i in range(len(j) - 1)))
            maxd = r.edit_budget(len(j), r.entries[eid].get("category", ""), domain_fuzzy_min_jamo)
            need = n_bg - 2 * maxd
            if len(bgs) < need: continue
            if maxd == 0:
                best = None; idx = tj.find(j)
                while idx != -1:
                    if idx in word_starts and (idx + len(j)) in ends_ok:
                        best = (0, idx + len(j)); break
                    idx = tj.find(j, idx + 1)
                if best is None: continue
                e = r.entries[eid]; inject = e.get("canonical") or e["term"]
                prev = results.get(inject)
                if prev is None or 1.0 > prev["score"]:
                    results[inject] = {"term": e["term"], "inject": inject, "category": e.get("category", ""),
                                       "score": 1.0, "_span": (best[1] - len(j), best[1]), "_jlen": len(j)}
                continue
            # clusters of bigram positions (as in the original), optionally requiring enough DISTINCT
            # term bigrams inside one cluster before any DP runs
            occ = sorted((p, bg) for bg in bgs for p in pos_by_bigram[bg])
            clusters = []
            for p, bg in occ:
                if clusters and p - clusters[-1][1] <= len(j):
                    clusters[-1][1] = p; clusters[-1][2].add(bg)
                else:
                    clusters.append([p, p, {bg}])
            best = None
            for pmin, pmax, cbgs in clusters:
                if positional and len(cbgs) < need: continue
                stats["clusters"] += 1
                lo = max(0, pmin - len(j) - maxd)
                a = bisect_left(word_start_list, lo)
                while a < len(word_start_list) and word_start_list[a] <= pmax:
                    ws = word_start_list[a]; a += 1
                    stats["dp"] += 1
                    hit = DP(j, tj[ws:ws + len(j) + maxd], maxd)
                    if hit is not None and (best is None or hit[0] < best[0]):
                        best = (hit[0], ws + hit[1])
                        if best[0] == 0: break
                if best is not None and best[0] == 0: break
            if best is None: continue
            d, end = best
            score = 1.0 - d / len(j)
            if min_score is not None and score < min_score: continue
            e = r.entries[eid]; inject = e.get("canonical") or e["term"]
            prev = results.get(inject)
            if prev is None or score > prev["score"]:
                results[inject] = {"term": e["term"], "inject": inject, "category": e.get("category", ""),
                                   "score": round(score, 3), "_span": (end - len(j), end), "_jlen": len(j)}
        return results, stats
    return retrieve

text = open(sys.argv[1]).read()
r = cr.CtxRetriever("/Users/jang-yeongbin/dev/jinong/jinong_gpu/stt-serve/catalog/catalog.jsonl",
                    "/Users/jang-yeongbin/dev/jinong/jinong_gpu/stt-serve/catalog/stopwords_v3top5k.txt",
                    edit_div=4, max_edits=5, fuzzy_min_jamo=10)
kw = dict(min_score=0.8, domain_fuzzy_min_jamo=6)
def top(res): return sorted(((v["inject"], v["score"]) for v in res.values()), key=lambda x: (-x[1], x[0]))
runs = [("baseline python DP, old prefilter", cr._best_prefix_distance, False),
        ("rapidfuzz DP, old prefilter",       prefix_distance_rf,      False),
        ("python DP, positional prefilter",   cr._best_prefix_distance, True),
        ("rapidfuzz DP + positional",         prefix_distance_rf,      True)]
ref = None
for label, dp, pos in runs:
    DP = dp; f = make_retrieve(r, positional=pos)
    t0 = time.time(); res, st = f(text, **kw); dt = time.time() - t0
    t = top(res)
    same = "ref" if ref is None else ("IDENTICAL" if t == ref else f"DIFF {len(set(t)^set(ref))}")
    if ref is None: ref = t
    print(f"{label:38s} {dt:7.1f}s  candidates={st['cand']} clusters={st['clusters']} dp_calls={st['dp']} matches={len(t)} {same}", flush=True)
print("top10:", ref[:10])
