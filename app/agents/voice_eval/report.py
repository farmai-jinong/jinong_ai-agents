"""리포트 렌더 + baseline 델타 + 회귀 게이트.

report.md 의 핵심은 2번 절 **원인 귀속 집계**다 — 감점이 STT 에서 났는지, 추출 프롬프트에서 났는지,
매핑에서 났는지가 다음에 뭘 고칠지를 정한다. 임계값은 코드가 아니라
`tests/agents/testcases/voice/thresholds.json` 에 있다(코드 수정 없이 조정).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cases import TESTCASES
from .judge import CAUSES, DIMENSIONS, KINDS, cause_matrix, cells

THRESHOLDS_PATH = TESTCASES / "thresholds.json"
DEFAULT_THRESHOLDS: dict[str, Any] = {
    "stt_keyword_recall": 0.85,
    "facts_recall": 0.80,
    "severity_exact": 0.60,
    "judge_overall_mean": 3.5,
    "judge_faithfulness_min": 3,
    "diary_status_all_ok": True,
}
DELTA_TOLERANCE = 0.02              # baseline 대비 이만큼 넘게 떨어지면 회귀로 표시


def load_thresholds(path: Path = THRESHOLDS_PATH) -> dict[str, Any]:
    if not path.exists():
        return dict(DEFAULT_THRESHOLDS)
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = dict(DEFAULT_THRESHOLDS)
    out.update({k: v for k, v in raw.items() if not k.startswith("_")})
    return out


# --------------------------------------------------------------------------- 집계
def _mean(vals: list[float]) -> float | None:
    """데이터가 없으면 None — 실행하지 않은 단계를 0점으로 오인해 게이트가 터지지 않게 한다."""
    return round(sum(vals) / len(vals), 4) if vals else None


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if not r.get("error")]
    judged = [r["judge"] for r in ok if r.get("judge")]
    stt = [r["stt"] for r in ok if r.get("stt")]
    pipe = [r["pipeline"] for r in ok if r.get("pipeline")]
    dims = {d: _mean([j["dimensions"][d] for j in judged if d in j.get("dimensions", {})]) for d in DIMENSIONS}
    return {
        "cases": len(rows),
        "errors": [r["case"] for r in rows if r.get("error")],
        "stt_keyword_recall": _mean([s["keyword_recall"] for s in stt]),
        "cer": _mean([s["cer"] for s in stt]),
        "wer": _mean([s["wer"] for s in stt]),
        "facts_recall": _mean([p["facts_recall"] for p in pipe]),
        "severity_exact": _mean([p["severity_ratio"] for p in pipe if p["severity_exact"][1]]),
        "judge_overall_mean": _mean([float(j["overall"]) for j in judged]),
        "judge_dimensions": dims,
        "judge_faithfulness_min": min([j["dimensions"].get("faithfulness", 0) for j in judged], default=None),
        "diary_status_all_ok": all(p["diary_status_ok"] for p in pipe) if pipe else None,
        "speaker_role_ok": sum(1 for p in pipe if p.get("speaker_role_ok")),
        "tokens": sum(int(p.get("tokens") or 0) for p in pipe) + sum(int(j.get("tokens") or 0) for j in judged),
        "cause_matrix": cause_matrix(judged),
        "cells": cells(judged),
        "judge_dimension_mean": _mean([float(v) for j in judged for v in j.get("dimensions", {}).values()]),
        "judge_items": sum(len(j.get("items", [])) for j in judged),
        "composite": composite(_mean([p["facts_recall"] for p in pipe]),
                               _mean([p["severity_ratio"] for p in pipe if p["severity_exact"][1]]),
                               _mean([float(v) for j in judged for v in j.get("dimensions", {}).values()])),
    }


# 자가 개선 루프의 단일 목적함수. judge 를 절반만 싣는 이유는 5케이스 LLM 채점이 흔들리기 때문이고,
# 나머지 절반은 LLM 과 무관한 결정적 지표라 게이밍이 어렵다.
COMPOSITE_WEIGHTS = {"judge": 0.50, "facts_recall": 0.30, "severity": 0.20}


def composite(facts_recall: float | None, severity_ratio: float | None,
              judge_dimension_mean: float | None) -> float | None:
    """0~1 종합점수. judge 항은 **축 평균**을 쓴다(총점 `overall` 이 아니라).

    `overall` 은 케이스당 1~5 정수 5개뿐이라 평균의 최소 눈금이 0.2 → 종합점수 0.02 이고, 이는 관측된
    채점 변동폭(노이즈 밴드)과 같다. 즉 검출하려는 개선과 같은 크기로 양자화돼 있어 실제로 타깃 감점을
    5→2 로 줄인 변경이 judge 총점 1점 하락에 상쇄돼 묻혔다(저널 #1). 6축×5케이스=30개 정수를 쓰는 축
    평균은 눈금이 6배 미세하다. 대신 총점이 담당하던 "정직성을 코버리지와 맞바꾸지 말 것"은
    `decide.confirm` 의 faithfulness·chatter 하드 가드로 옮겼다.

    셋 중 하나라도 미측정이면 None (미실행 단계를 0점으로 오인하지 않는다).
    """
    if judge_dimension_mean is None or facts_recall is None:
        return None
    w = COMPOSITE_WEIGHTS
    sev = 1.0 if severity_ratio is None else severity_ratio
    return round(w["judge"] * (judge_dimension_mean / 5.0) + w["facts_recall"] * facts_recall
                 + w["severity"] * sev, 4)


def gate(summary: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    """미달 항목 목록. 비어 있으면 통과."""
    fails: list[str] = []
    if summary["errors"]:
        fails.append(f"실행 실패 케이스: {', '.join(summary['errors'])}")
    for key in ("stt_keyword_recall", "facts_recall", "severity_exact", "judge_overall_mean"):
        want = thresholds.get(key)
        got = summary.get(key)
        if want is not None and got is not None and got < want:
            fails.append(f"{key} {got:.3f} < {want}")
    want_f = thresholds.get("judge_faithfulness_min")
    got_f = summary["judge_faithfulness_min"]
    if want_f is not None and got_f is not None and got_f < want_f:
        fails.append(f"judge_faithfulness_min {got_f} < {want_f}")
    if thresholds.get("diary_status_all_ok") and summary["diary_status_all_ok"] is False:
        fails.append("diary_status 기대와 불일치한 케이스 있음")
    return fails


def _fmt(v: Any, spec: str = ".3f") -> str:
    """미실행 단계(None)는 `—` 로."""
    return format(v, spec) if isinstance(v, (int, float)) else "—"


def _delta(cur: Any, base: Any) -> str:
    if not isinstance(cur, (int, float)) or not isinstance(base, (int, float)):
        return ""
    d = cur - base
    if abs(d) < 1e-9:
        return " (=)"
    return f" ({'▲' if d > 0 else '▼'}{abs(d):.3f})"


# --------------------------------------------------------------------------- 렌더
def _pair(t: list[int] | tuple[int, int]) -> str:
    return f"{t[0]}/{t[1]}"


def render(rows: list[dict[str, Any]], summary: dict[str, Any], thresholds: dict[str, Any],
           baseline: dict[str, Any] | None, fails: list[str]) -> str:
    b = baseline or {}
    L: list[str] = ["# 음성 테스트케이스 평가 리포트", ""]
    L.append(f"- 케이스 {summary['cases']}건 · 총 토큰 {summary['tokens']:,}")
    if any(summary.get(k) is None for k in ("stt_keyword_recall", "facts_recall", "judge_overall_mean")):
        L.append("- `—` 는 이번 실행에서 돌리지 않은 단계 (게이트에서도 제외)")
    L.append(f"- 게이트: {'**통과**' if not fails else '**실패** — ' + '; '.join(fails)}")
    if baseline:
        L.append("- baseline 대비 델타를 괄호로 표시 (▲ 개선 / ▼ 악화)")
    L.append("")

    L += ["## 1. 케이스별 요약", "",
          "| 케이스 | 핵심어 | CER | 농작업 | 병해충 | 제품 | 단계 | judge | 최저 축 | 일지 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r.get("error"):
            L.append(f"| `{r['case']}` | — | — | — | — | — | — | — | — | ERROR: {r['error'][:60]} |")
            continue
        s, p, j = r.get("stt") or {}, r.get("pipeline") or {}, r.get("judge") or {}
        dims = j.get("dimensions") or {}
        worst = min(dims.items(), key=lambda kv: kv[1])[0] if dims else "—"
        fw, pe, pr, sv = (_pair(p["farmworks_recall"]), _pair(p["pests_recall"]),
                          _pair(p["products_recall"]), _pair(p["severity_exact"])) if p else ("—",) * 4
        L.append(
            f"| `{r['case']}` | {_fmt(s.get('keyword_recall'), '.2f')} | {_fmt(s.get('cer'))} | "
            f"{fw} | {pe} | {pr} | {sv} | "
            f"{j.get('overall', '—')} | {worst}({dims.get(worst, '—')}) | "
            f"{'OK' if p.get('diary_status_ok') else ('MISMATCH ' + str(p.get('diaries'))) if p else '—'} |")
    L.append("")
    L += ["**전체 평균**", "",
          f"- 핵심어 인식률 `{_fmt(summary['stt_keyword_recall'])}`{_delta(summary['stt_keyword_recall'], b.get('stt_keyword_recall'))}"
          f" (임계 {thresholds['stt_keyword_recall']})",
          f"- 기대 추출 재현율 `{_fmt(summary['facts_recall'])}`{_delta(summary['facts_recall'], b.get('facts_recall'))}"
          f" (임계 {thresholds['facts_recall']})",
          f"- 발생단계 정확도 `{_fmt(summary['severity_exact'])}`{_delta(summary['severity_exact'], b.get('severity_exact'))}"
          f" (임계 {thresholds['severity_exact']})",
          f"- judge 총점 `{_fmt(summary['judge_overall_mean'], '.2f')}`{_delta(summary['judge_overall_mean'], b.get('judge_overall_mean'))}"
          f" (임계 {thresholds['judge_overall_mean']})",
          f"- CER `{_fmt(summary['cer'])}` · WER `{_fmt(summary['wer'])}` (참고 — 대본 낭독이라 애드리브 포함)",
          f"- 화자 역할 정합 {summary['speaker_role_ok']}/{summary['cases']}",
          f"- judge 축 평균 `{_fmt(summary['judge_dimension_mean'], '.3f')}`"
          f"{_delta(summary['judge_dimension_mean'], b.get('judge_dimension_mean'))}"
          f" · 감점 항목 {summary['judge_items']}건",
          f"- **종합점수 `{_fmt(summary['composite'], '.4f')}`**{_delta(summary['composite'], b.get('composite'))}"
          f" (judge 축평균 {COMPOSITE_WEIGHTS['judge']:.0%} + 재현율 {COMPOSITE_WEIGHTS['facts_recall']:.0%}"
          f" + 발생단계 {COMPOSITE_WEIGHTS['severity']:.0%} — 자가 개선 루프의 목적함수)", ""]
    L += ["**judge 축별 평균**", "",
          "| " + " | ".join(DIMENSIONS) + " |", "|" + "---|" * len(DIMENSIONS)]
    bd = (b.get("judge_dimensions") or {})
    L.append("| " + " | ".join(f"{_fmt(summary['judge_dimensions'][d], '.2f')}"
                               f"{_delta(summary['judge_dimensions'][d], bd.get(d))}"
                               for d in DIMENSIONS) + " |")
    L.append("")

    L += ["## 2. 원인 귀속 집계", "", "감점 항목을 원인별로 모은 표. **다음에 뭘 고칠지는 이 표가 정한다.**", "",
          "| 원인 | " + " | ".join(KINDS) + " | 합계 |", "|---|" + "---|" * (len(KINDS) + 1)]
    matrix = summary["cause_matrix"]
    for cause in CAUSES:
        row = matrix[cause]
        total = sum(row.values())
        if total == 0:
            continue
        L.append(f"| **{cause}** | " + " | ".join(str(row[k]) for k in KINDS) + f" | {total} |")
    if not any(sum(matrix[c].values()) for c in CAUSES):
        L.append("| — | — | — | — | — | 감점 항목 없음 |")
    top_cells = list(summary.get("cells", {}).items())[:6]
    if top_cells:
        L += ["", "**상위 셀 (원인/종류/섹션)** — 자가 개선 루프가 한 번에 하나씩 타깃으로 잡는 단위", ""]
        L += [f"{i + 1}. `{k}` — {v}건" for i, (k, v) in enumerate(top_cells)]
    L += ["", "- `stt` → 전사에 없음. 게이트웨이 STT·오디오 품질 문제 (프롬프트로 해결 불가)",
          "- `extraction` → 전사엔 있는데 못 뽑음. `extract.system.md` / `diary_content` 프롬프트 수정 대상",
          "- `mapping` → 표준 명칭·코드·발생단계 변환 오류. `app/agents/mapping/` 수정 대상",
          "- `rendering` → 섹션 배치·표기. `render/templates/diary.md.j2` 수정 대상", ""]

    L += ["## 3. STT 미인식 핵심어", ""]
    L.append("`n/a` = 대본에서 그 표기로 발화되지 않음(구어체) — STT 책임이 아니라 매핑 단계 몫이라 분모에서 제외.")
    L.append("")
    any_miss = False
    for r in rows:
        s = r.get("stt") or {}
        miss = [k["keyword"] for k in s.get("keywords", []) if k["status"] == "miss"]
        fuzzy = [f"{k['keyword']}({k['score']})" for k in s.get("keywords", []) if k["status"] == "fuzzy"]
        na = [k["keyword"] for k in s.get("keywords", []) if k["status"] == "n/a"]
        if miss or fuzzy or na:
            any_miss = True
            L.append(f"- `{r['case']}` — 미인식: {', '.join(miss) or '없음'} / 근사: {', '.join(fuzzy) or '없음'}"
                     f" / n/a: {', '.join(na) or '없음'}")
    if not any_miss:
        L.append("- 전 케이스 핵심어 인식 (miss/fuzzy 없음)")
    L.append("")

    L += ["## 4. 케이스별 상세", ""]
    for r in rows:
        L.append(f"### `{r['case']}`")
        if r.get("error"):
            L += ["", f"ERROR: {r['error']}", ""]
            continue
        s, p, j = r.get("stt") or {}, r.get("pipeline") or {}, r.get("judge") or {}
        L.append(f"- 전사: {s.get('n_segments')}세그 · 화자 {s.get('n_speakers')}명(최다 {s.get('top_speaker_share', 0):.0%})"
                 f" · {s.get('duration_sec')}초 · 대본유사도 {s.get('similarity')}")
        L.append(f"- 일지: {p.get('diaries')} · farmos={p.get('farmos_status')} · 매핑확정 {p.get('mapped')}건"
                 f" · 근거 {_pair(p.get('evidence_valid', [0, 0]))} · {p.get('calls')}콜 {p.get('tokens')}토큰")
        if j:
            L.append(f"- judge({j.get('model')}): **{j.get('overall')}/5** — {j.get('summary')}")
            for name in DIMENSIONS:
                if name in (j.get("dimensions") or {}):
                    L.append(f"  - `{name}` {j['dimensions'][name]}/5 — {(j.get('dimension_reasons') or {}).get(name, '')}")
            for it in j.get("items", []):
                L.append(f"  - [{it['kind']} · {it['cause']}] ({it['section']}) {it['text']}")
        for w in (p.get("warnings") or [])[:6]:
            L.append(f"- ⚠ {w}")
        L.append("")
    return "\n".join(L)
