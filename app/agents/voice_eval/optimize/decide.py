"""측정과 수락 판정.

2단계다. **스크리닝**은 judge 1회로 싸게 걸러내고, 그걸 통과한 후보만 **확정** 단계에서 baseline 과
나란히 judge 3회 재채점한다. 짝지어 재채점하는 이유는 LLM 채점이 실행마다 흔들려서, 후보만 새로 채점하고
예전 baseline 점수와 비교하면 드리프트를 개선으로 오인하기 때문이다.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import report as report_mod
from ..judge import cell_key

log = logging.getLogger("optimize.decide")


# 종합점수에서 judge 총점을 뺀 대가로, 총점이 담당하던 "정직성을 코버리지와 맞바꾸지 말 것"을
# 여기서 하드 가드로 강제한다. 이 두 축은 떨어지면 점수가 올라도 거부다.
PROTECTED_DIMENSIONS = ("faithfulness", "chatter")


@dataclass
class Measurement:
    composite: float | None
    judge: float | None                          # 축 평균 (총점 아님 — report.composite 주석 참조)
    facts_recall: float | None
    severity: float | None
    diary_status_all_ok: bool | None
    errors: list[str]
    cells: dict[str, int]
    tokens: int
    summary: dict[str, Any]
    dimensions: dict[str, float] = None          # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.dimensions = self.dimensions or {}

    def cell(self, key: str) -> int:
        return self.cells.get(key, 0)

    def row(self, target: str, base: "Measurement | None" = None) -> dict[str, Any]:
        """저널 표에 넣을 dict."""
        return {"composite": self.composite, "judge": self.judge, "facts_recall": self.facts_recall,
                "severity": self.severity, "target_after": self.cell(target),
                "target_before": None if base is None else base.cell(target),
                "base_composite": None if base is None else base.composite}


def run_eval(cwd: Path, out: Path, *, python: str, judge_repeat: int, force_judge: bool,
             timeout: float = 3600.0) -> Measurement:
    """`voice_eval --stages pipeline,judge` 를 돌리고 summary.json 을 읽어 측정치를 만든다.

    `--stages pipeline,judge` 라도 result.json 이 있으면 파이프라인은 캐시를 쓴다 → 재채점만 할 때는
    `force_judge=True` 로 judge 만 다시 돌린다(파이프라인 토큰 0).
    """
    out = out.resolve()                            # cwd 가 worktree 라 상대경로는 엉뚱한 데로 간다
    cmd = [python, "-m", "app.agents.voice_eval", "--stages", "pipeline,judge",
           "--out", str(out), "--judge-repeat", str(judge_repeat), "--no-gate"]
    if force_judge:
        cmd += ["--force", "judge"]
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    path = out / "summary.json"
    if not path.exists():
        raise RuntimeError(f"측정 실패 (summary.json 없음): {(r.stderr or r.stdout)[-500:]}")
    data = json.loads(path.read_text(encoding="utf-8"))
    s = report_mod.aggregate(data["cases"])          # 저장된 summary 를 믿지 않고 항상 재집계
    return from_summary(s)


def from_summary(s: dict[str, Any], tokens: int | None = None) -> Measurement:
    return Measurement(
        composite=s["composite"], judge=s["judge_dimension_mean"], facts_recall=s["facts_recall"],
        severity=s["severity_exact"], diary_status_all_ok=s["diary_status_all_ok"],
        errors=s["errors"], cells=s["cells"],
        tokens=int(s["tokens"] or 0) if tokens is None else tokens, summary=s,
        dimensions=dict(s.get("judge_dimensions") or {}))


def snapshot(src: Path, dest: Path) -> Path:
    """baseline 산출물을 복사한다 — 재채점이 원본 judge.json 을 덮어쓰지 않도록."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("_unmatched", "audio.wav"))
    return dest


# --------------------------------------------------------------------------- 판정
@dataclass
class Verdict:
    accept: bool
    reason: str


def _drop(cur: float | None, base: float | None, tol: float) -> bool:
    return cur is not None and base is not None and cur < base - tol


def screen(cand: Measurement, base: Measurement, target: str, band: float) -> Verdict:
    """싼 1차 판정 — 여기서 떨어지면 비싼 재채점을 하지 않는다.

    **확정과 같은 문턱을 쓰면 안 된다.** 스크리닝은 judge 1회라 확정(3회 중앙값)보다 잡음이 크다.
    같은 밴드를 요구하면 진짜 개선이 1회 채점의 흔들림에 걸려 확정 단계까지 가지도 못한다(저널 #1·#2 가
    둘 다 스크리닝에서 끝났다). 그래서 여기서는 **명백한 악화만** 쳐내고, 판단은 확정 단계에 맡긴다.
    타깃 셀이 늘어난 것도 이 단계에서 걸러 낸다 — 고치려던 것을 못 고쳤다는 싸고 확실한 신호다.
    """
    if cand.errors:
        return Verdict(False, f"케이스 실행 실패: {', '.join(cand.errors)}")
    if cand.composite is None or base.composite is None:
        return Verdict(False, "종합점수를 계산할 수 없다(측정 누락)")
    if cand.cell(target) > base.cell(target):
        return Verdict(False, f"타깃 셀이 오히려 증가 {base.cell(target)} → {cand.cell(target)}")
    floor = base.composite - band / 2
    if cand.composite < floor:
        return Verdict(False, f"종합점수 {cand.composite:.4f} < 기준 {base.composite:.4f} - 밴드/2 "
                              f"({floor:.4f}) — 명백한 악화")
    return Verdict(True, f"스크리닝 통과 {base.composite:.4f} → {cand.composite:.4f} "
                         f"(타깃 {base.cell(target)} → {cand.cell(target)}) — 확정 재채점으로")


def confirm(cand: Measurement, base: Measurement, target: str, band: float,
            det_tol: float = 0.001) -> Verdict:
    """확정 판정 — 짝지어 재채점한 결과로 수락 여부를 정한다.

    종합점수 상승만으로는 부족하다. 결정적 지표(LLM 과 무관)가 하나라도 떨어지면 거부하고,
    고치려던 셀이 늘어도 거부한다 — "무엇을 고쳤는가"에 대한 직접 증거를 요구하는 것이다.
    """
    if cand.errors:
        return Verdict(False, f"케이스 실행 실패: {', '.join(cand.errors)}")
    if cand.diary_status_all_ok is False and base.diary_status_all_ok is not False:
        return Verdict(False, "일지 상태가 기대와 어긋난 케이스가 생겼다")
    if _drop(cand.facts_recall, base.facts_recall, det_tol):
        return Verdict(False, f"기대추출 재현율 하락 {base.facts_recall} → {cand.facts_recall}")
    if _drop(cand.severity, base.severity, det_tol):
        return Verdict(False, f"발생단계 정확도 하락 {base.severity} → {cand.severity}")
    for dim in PROTECTED_DIMENSIONS:
        if _drop(cand.dimensions.get(dim), base.dimensions.get(dim), 0.001):
            return Verdict(False, f"{dim} 축 하락 {base.dimensions[dim]} → {cand.dimensions[dim]} "
                                  f"— 없는 말을 지어내거나 잡담이 섞이는 대가로 얻은 점수는 받지 않는다")
    if cand.cell(target) > base.cell(target):
        return Verdict(False, f"타깃 셀이 오히려 증가 {base.cell(target)} → {cand.cell(target)}")
    if cand.composite is None or base.composite is None:
        return Verdict(False, "종합점수를 계산할 수 없다(측정 누락)")
    if cand.composite <= base.composite + band:
        return Verdict(False, f"재채점 후 종합점수 {cand.composite:.4f} ≤ 기준 {base.composite:.4f} + 노이즈 {band}")
    return Verdict(True, f"종합점수 {base.composite:.4f} → {cand.composite:.4f}, "
                         f"타깃 셀 {base.cell(target)} → {cand.cell(target)}")


def target_of(items_summary: dict[str, Any], cell: str) -> int:
    """summary(cases 포함) 에서 특정 셀의 건수."""
    n = 0
    for row in items_summary.get("cases", []):
        for it in (row.get("judge") or {}).get("items", []):
            if cell_key(it) == cell:
                n += 1
    return n
