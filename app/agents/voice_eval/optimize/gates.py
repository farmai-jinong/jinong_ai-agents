"""게이트 — 제안된 변경을 측정 전에 걸러낸다. 전부 결정적이고 LLM 을 쓰지 않는다.

순서가 곧 비용 순서다: 경로·일반화(즉시) → ruff → pytest → 픽스처 재현율(LLM 없음) → 그 다음에야
실제 측정(유료)으로 넘어간다. 앞에서 걸리면 judge 토큰을 한 푼도 안 쓴다.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from ..cases import load_cases

# 수정 허용 경로 (mode=tune). 프롬프트 + 매핑 데이터만.
ALLOWLIST = [
    "app/agents/prompts/*.system.md",
    "app/agents/prompts/*.user.md.j2",
    "app/agents/prompts/loader.py",
    "app/agents/mapping/synonyms.yaml",
    "app/agents/mapping/severity.yaml",
]
# allowlist 와 겹쳐도 무조건 금지. 채점 하네스·정답·테스트는 최적화 대상이지 수단이 아니다.
DENYLIST = [
    "app/agents/prompts/judge_diary.*",
    "app/agents/voice_eval/*",
    "app/agents/voice_eval/**",
    "tests/*",
    "tests/**",
    "docs/eval-journal.*",
    "app/config.py",
    ".env*",
]

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]{2,}")
# 케이스 고유 토큰이라도 이런 일반 명사는 프롬프트에 있어도 과적합이 아니다
_GENERIC = {"딸기", "토마토", "포도", "농약", "비료", "종자", "농자재", "관수", "적엽", "수확", "정식",
            "시비", "추비", "환기", "적심", "적과", "유인", "약제살포", "방제", "살포", "병해충", "예방"}


def _generic_vocab() -> set[str]:
    """일반 어휘 = 하드코딩 고정 목록 + `synonyms.yaml` 에 이미 있는 표기.

    제형어("액상수화제")나 이미 동의어 사전에 등재된 병해충 표준명은 특정 케이스의 정답이 아니라
    공용 어휘다. 프롬프트에 나와도 과적합이 아니므로 일반화 게이트에서 제외한다.
    """
    from ...mapping.matcher import load_synonyms
    vocab = set(_GENERIC)
    syn = load_synonyms()
    for key in ("farmwork", "pest", "product"):
        for k, v in (syn.get(key) or {}).items():
            vocab.update(_TOKEN.findall(f"{k} {v}"))
    for key in ("formulation_suffixes", "generic_suffixes"):
        vocab.update(str(x) for x in (syn.get(key) or []))
    return vocab


@dataclass
class GateResult:
    name: str
    ok: bool
    detail: str = ""


# 하네스가 worktree 에 직접 넣어 준 것들 — 제안자의 변경이 아니므로 diff 에서 뺀다.
# (실제 리포지토리에서는 .gitignore 가 이미 가려 주지만, 그 사실에 기대지 않는다.)
INJECTED = (":(exclude).env", ":(exclude).env.*", ":(exclude)out")


def changed_files(wt: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(wt), "status", "--porcelain", "--", ".", *INJECTED],
                         capture_output=True, text=True, check=True).stdout
    return sorted({ln[3:].strip().split(" -> ")[-1] for ln in out.splitlines() if ln.strip()})


def stage(wt: Path) -> None:
    subprocess.run(["git", "-C", str(wt), "add", "-A", "--", ".", *INJECTED],
                   check=True, capture_output=True)


def diff_text(wt: Path) -> str:
    stage(wt)
    return subprocess.run(["git", "-C", str(wt), "diff", "--cached", "--", ".", *INJECTED],
                          capture_output=True, text=True, check=True).stdout


def diff_stat(wt: Path) -> str:
    out = subprocess.run(["git", "-C", str(wt), "diff", "--cached", "--shortstat", "--", ".", *INJECTED],
                         capture_output=True, text=True, check=True).stdout.strip()
    return out or "변경 없음"


# --------------------------------------------------------------------------- 개별 게이트
def gate_paths(files: list[str]) -> GateResult:
    """allowlist 밖이거나 denylist 에 걸리는 파일이 하나라도 있으면 거부."""
    if not files:
        return GateResult("path", False, "변경된 파일이 없다")
    bad = [f for f in files
           if any(fnmatch(f, d) for d in DENYLIST) or not any(fnmatch(f, a) for a in ALLOWLIST)]
    return GateResult("path", not bad, "" if not bad else f"허용되지 않은 경로: {', '.join(bad)}")


def case_tokens() -> set[str]:
    """테스트케이스 고유 토큰 — 제품명·병해충명·농가명. 프롬프트에 새로 박히면 과적합이다."""
    toks: set[str] = set()
    for c in load_cases():
        for p in c.expect.get("products", []):
            toks.update(_TOKEN.findall(p))
        for p in c.expect.get("pests", []):
            toks.update(_TOKEN.findall(p[0] if isinstance(p, list) else p))
        origin = c.origin
        for key in ("user_id", "dbyhs_names", "prvnbe_info"):
            toks.update(_TOKEN.findall(str(origin.get(key) or "")))
    generic = _generic_vocab()
    return {t for t in toks if t not in generic and not t.isdigit()}


def gate_generalization(diff: str, tokens: set[str] | None = None) -> GateResult:
    """diff 의 **추가된 줄**에 케이스 고유 토큰이 새로 등장하면 거부.

    정답을 프롬프트에 박아 점수를 올리는 길을 막는다. 삭제 줄은 보지 않는다(원래 있던 것을 지우는 건 자유).
    """
    toks = case_tokens() if tokens is None else tokens
    added = "\n".join(ln[1:] for ln in diff.splitlines()
                      if ln.startswith("+") and not ln.startswith("+++"))
    hits = sorted({t for t in toks if t in added})
    return GateResult("generalization", not hits,
                      "" if not hits else f"케이스 고유 토큰이 하드코딩됨: {', '.join(hits[:8])}")


def _run(cmd: list[str], cwd: Path, timeout: float = 900.0) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timeout: {' '.join(cmd)}"
    tail = (r.stdout + r.stderr).strip().splitlines()[-6:]
    return r.returncode == 0, "\n".join(tail)


def gate_ruff(wt: Path, python: str) -> GateResult:
    ok, out = _run([python, "-m", "ruff", "check", "app", "tests"], wt)
    return GateResult("ruff", ok, "" if ok else out)


def gate_pytest(wt: Path, python: str) -> GateResult:
    """홀드아웃 — 제안자가 보지 않은 합성 픽스처·골든 렌더 테스트가 회귀를 잡는다."""
    ok, out = _run([python, "-m", "pytest", "-q"], wt)
    return GateResult("pytest", ok, "" if ok else out)


def fixture_recall(wt: Path, python: str, out_dir: Path) -> tuple[bool, dict[str, list[int]], str]:
    """`eval.py --provider fake` 재현율 — LLM 없이 매핑·렌더 회귀를 잡는 무료 지표."""
    ok, out = _run([python, "-m", "app.agents.eval", "--provider", "fake", "--out", str(out_dir)], wt)
    if not ok:
        return False, {}, out
    rows = json.loads((out_dir / "eval.json").read_text(encoding="utf-8"))
    got: dict[str, list[int]] = {}
    for r in rows:
        if "farmworks_recall" not in r:
            continue
        got[r["fixture"]] = [r["farmworks_recall"][0] + r["pests_recall"][0] + r["products_recall"][0],
                             r["farmworks_recall"][1] + r["pests_recall"][1] + r["products_recall"][1]]
    return True, got, ""


def gate_fixture_recall(before: dict[str, list[int]], after: dict[str, list[int]]) -> GateResult:
    drops = [f"{k}: {before[k][0]}→{after[k][0]}" for k in before
             if k in after and after[k][0] < before[k][0]]
    missing = sorted(set(before) - set(after))
    if missing:
        return GateResult("fixture_recall", False, f"픽스처가 사라짐: {', '.join(missing)}")
    return GateResult("fixture_recall", not drops, "" if not drops else f"재현율 하락: {', '.join(drops)}")


def summarize(results: list[GateResult]) -> tuple[bool, dict[str, Any], str]:
    """(전부 통과?, 저널용 dict, 첫 실패 사유)."""
    gates = {g.name: (True if g.ok else g.detail or "실패") for g in results}
    first = next((g for g in results if not g.ok), None)
    return first is None, gates, ("" if first is None else f"[{first.name}] {first.detail}")
