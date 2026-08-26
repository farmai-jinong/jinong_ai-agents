"""자가 개선 루프 CLI.

    python -m app.agents.voice_eval.optimize [--max-iters 5] [--resume] [--dry-run]
        [--cell <cause/kind/section>] [--model opus] [--judge-repeat-confirm 3]
        [--noise-band 0.02] [--cell-cooldown 3] [--plateau 3] [--token-budget 3000000]

반복 = 진단 → 제안(Claude, worktree) → 게이트 → 스크리닝 → 확정 → 수락/거부 → 기록.
수락된 변경은 `eval/auto-tune` 브랜치에만 커밋된다. `main` 은 건드리지 않는다.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import report as report_mod
from . import decide, gates, propose, retro
from .diagnose import Target, briefing, pick_target
from .journal import Journal, Record

log = logging.getLogger("optimize")
REPO = Path(__file__).resolve().parents[4]
BRANCH = "eval/auto-tune"


# --------------------------------------------------------------------------- git
def git(*args: str, cwd: Path = REPO) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def ensure_clean_tree() -> str | None:
    """루프는 dirty tree 에서 시작하지 않는다 — 매 반복의 diff 가 그 반복의 변경과 일치해야 한다."""
    dirty = git("status", "--porcelain")
    if dirty:
        n = len(dirty.splitlines())
        return (f"작업 트리에 커밋되지 않은 변경이 {n}건 있다. 루프는 커밋된 상태에서만 시작한다\n"
                f"  (worktree 는 커밋된 것만 복제하고, 반복마다의 diff 가 그 반복의 변경과 정확히 일치해야 한다)\n"
                f"  먼저 커밋한 뒤 다시 실행할 것: git add -A && git commit")
    return None


def ensure_branch(base: str) -> None:
    """`eval/auto-tune` 이 없으면 현재 커밋에서 만든다. 체크아웃은 하지 않는다."""
    if subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", BRANCH],
                      capture_output=True).returncode != 0:
        git("branch", BRANCH, base)
        log.info("브랜치 생성: %s", BRANCH)


def tip() -> str:
    """다음 반복의 베이스 = auto-tune 브랜치 끝(수락분이 누적된 지점)."""
    return git("rev-parse", BRANCH)


def commit_worktree(wt: Path, message: str) -> str:
    gates.stage(wt)                            # 하네스가 넣은 .env/out 은 제외하고 스테이징
    subprocess.run(["git", "-C", str(wt), "commit", "-m", message], check=True, capture_output=True)
    sha = git("rev-parse", "HEAD", cwd=wt)
    git("branch", "-f", BRANCH, sha)          # detached worktree 의 커밋을 브랜치 끝으로 옮긴다
    return sha


# --------------------------------------------------------------------------- 반복 1회
def run_iteration(args: argparse.Namespace, journal: Journal, base_summary: dict[str, Any],
                  base_measure: decide.Measurement) -> tuple[Record, decide.Measurement, dict[str, Any]]:
    """(기록, 새 baseline 측정, 새 baseline summary). 거부면 baseline 은 그대로 돌려준다."""
    n = journal.next_iter
    it_dir = Path(args.work) / f"iter-{n:03d}"
    it_dir.mkdir(parents=True, exist_ok=True)
    base_commit = tip()
    rec = Record(iter=n, ts=datetime.now().astimezone().isoformat(timespec="seconds"),
                 base_commit=base_commit)

    target = pick_target(base_summary, journal, cooldown=args.cell_cooldown, forced=args.cell)
    if target is None:
        rec.decision, rec.reason = "error", "타깃으로 삼을 감점 셀이 없다(쿨다운 중이거나 감점 0)"
        return rec, base_measure, base_summary
    rec.target = target.to_dict()
    log.info("#%d 타깃 셀: %s (%d건)", n, target.cell, target.count)

    brief = briefing(target, base_summary, journal, Path(args.eval_out), propose._allowlist_note())
    (it_dir / "briefing.md").write_text(brief, encoding="utf-8")

    wt = it_dir / "wt"
    propose.make_worktree(REPO, wt, base_commit, Path(args.eval_out))
    try:
        return _propose_and_judge(args, journal, rec, target, brief, wt, it_dir,
                                  base_measure, base_summary)
    finally:
        if not args.keep_worktree:
            propose.remove_worktree(REPO, wt)


def _propose_and_judge(args, journal, rec: Record, target: Target, brief: str, wt: Path, it_dir: Path,
                       base_measure: decide.Measurement, base_summary: dict[str, Any]):
    # --- 제안 -------------------------------------------------------------
    if args.dry_run:
        prop = _stub_proposal(wt)
    else:
        prop = propose.run_claude(wt, brief, model=args.model, max_turns=args.max_turns)
    rec.claude_session_id = prop.session_id
    rec.tokens["claude"] = prop.tokens
    rec.hypothesis, rec.why = prop.hypothesis, prop.why
    if not prop.ok:
        rec.decision, rec.reason = "error", prop.error
        return rec, base_measure, base_summary

    # --- 게이트 (무료) ------------------------------------------------------
    files = gates.changed_files(wt)
    diff = gates.diff_text(wt)
    rec.files, rec.diff_stat = files, gates.diff_stat(wt)
    (it_dir / "proposal.diff").write_text(diff, encoding="utf-8")

    checks = [gates.gate_paths(files), gates.gate_generalization(diff)]
    if all(c.ok for c in checks):
        checks.append(gates.gate_ruff(wt, args.python))
    if all(c.ok for c in checks):
        checks.append(gates.gate_pytest(wt, args.python))
    if all(c.ok for c in checks):
        ok, after, err = gates.fixture_recall(wt, args.python, it_dir / "fake-eval")
        checks.append(gates.GateResult("fixture_recall", ok, err) if not ok
                      else gates.gate_fixture_recall(args.baseline_fixture_recall, after))
    passed, rec.gates, first_fail = gates.summarize(checks)
    if not passed:
        rec.decision, rec.reason = "rejected", first_fail
        return rec, base_measure, base_summary

    # --- 스크리닝 (싼 측정) --------------------------------------------------
    log.info("#%d 스크리닝 측정 (judge×1)", rec.iter)
    cand = decide.run_eval(wt, wt / "out" / "voice-eval", python=args.python,
                           judge_repeat=1, force_judge=True)
    rec.tokens["screen"] = cand.tokens
    rec.screen = cand.row(target.cell, base_measure)
    v = decide.screen(cand, base_measure, target.cell, args.noise_band)
    if not v.accept:
        rec.decision, rec.reason = "rejected", v.reason
        return rec, base_measure, base_summary

    # --- 확정 (짝지은 재채점) -------------------------------------------------
    log.info("#%d 확정 재채점 (judge×%d, baseline 과 짝지어)", rec.iter, args.judge_repeat_confirm)
    base_copy = decide.snapshot(Path(args.eval_out), it_dir / "base")
    base_re = decide.run_eval(REPO, base_copy, python=args.python,
                              judge_repeat=args.judge_repeat_confirm, force_judge=True)
    cand_re = decide.run_eval(wt, wt / "out" / "voice-eval", python=args.python,
                              judge_repeat=args.judge_repeat_confirm, force_judge=True)
    rec.tokens["confirm"] = base_re.tokens + cand_re.tokens
    rec.confirm = cand_re.row(target.cell, base_re)
    v = decide.confirm(cand_re, base_re, target.cell, args.noise_band)
    rec.reason = v.reason
    if not v.accept:
        rec.decision = "rejected"
        return rec, base_measure, base_summary

    # --- 수락 -------------------------------------------------------------
    msg = (f"자동튜닝: {rec.hypothesis}\n\n"
           f"타깃 {target.cell} {base_re.cell(target.cell)}→{cand_re.cell(target.cell)}, "
           f"종합 {base_re.composite}→{cand_re.composite} "
           f"(judge {base_re.judge}→{cand_re.judge}, 재현율 {base_re.facts_recall}→{cand_re.facts_recall})\n"
           f"{rec.why}")
    rec.commit = commit_worktree(wt, msg)
    rec.decision = "accepted"
    # 수락된 산출물을 새 baseline 으로 승격 — 다음 반복이 이 상태를 기준으로 진단한다
    shutil.rmtree(Path(args.eval_out), ignore_errors=True)
    shutil.copytree(wt / "out" / "voice-eval", Path(args.eval_out))
    new_summary = json.loads((Path(args.eval_out) / "summary.json").read_text(encoding="utf-8"))
    return rec, cand_re, new_summary


def _stub_proposal(wt: Path) -> propose.Proposal:
    """--dry-run: LLM 없이 무해한 주석 한 줄을 넣어 게이트·측정 배선만 확인한다."""
    p = wt / "app" / "agents" / "mapping" / "synonyms.yaml"
    p.write_text(p.read_text(encoding="utf-8") + "\n# dry-run 확인용 주석\n", encoding="utf-8")
    return propose.Proposal(True, hypothesis="(dry-run) 배선 확인", why="게이트·측정 경로 점검",
                            files=["app/agents/mapping/synonyms.yaml"], session_id=None)


# --------------------------------------------------------------------------- 루프
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="평가 결과로 프롬프트·매핑을 스스로 고치는 루프")
    ap.add_argument("--max-iters", type=int, default=3)
    ap.add_argument("--cell", help="타깃 셀 강제 지정 (cause/kind/section)")
    ap.add_argument("--model", help="제안 세션 모델 (opus|sonnet|...)")
    ap.add_argument("--max-turns", type=int, default=30)
    ap.add_argument("--judge-repeat-confirm", type=int, default=3)
    ap.add_argument("--noise-band", type=float, default=0.02, help="이만큼 넘게 올라야 개선으로 본다")
    ap.add_argument("--cell-cooldown", type=int, default=3, help="최근 N회 거부된 셀은 건너뛴다")
    ap.add_argument("--plateau", type=int, default=3, help="연속 N회 거부되면 구조 제안서를 쓰고 멈춘다")
    ap.add_argument("--token-budget", type=int, default=0, help="0 = 무제한")
    ap.add_argument("--eval-out", default="out/voice-eval")
    ap.add_argument("--work", default="out/optimize")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--dry-run", action="store_true", help="LLM 없이 게이트·측정 배선만 확인")
    ap.add_argument("--no-retro", action="store_true", help="플래토에도 구조 제안서를 쓰지 않는다")
    ap.add_argument("--keep-worktree", action="store_true", help="디버그용 — worktree 를 지우지 않는다")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    if (err := ensure_clean_tree()):
        print(err, file=sys.stderr)
        return 2
    eval_out = Path(args.eval_out)
    if not (eval_out / "summary.json").exists():
        print(f"{eval_out}/summary.json 이 없다. 먼저 평가를 한 번 돌릴 것:\n"
              f"  python -m app.agents.voice_eval --audio-dir ~/Downloads/recordings", file=sys.stderr)
        return 2

    ensure_branch(git("rev-parse", "HEAD"))
    journal = Journal()
    base_summary = json.loads((eval_out / "summary.json").read_text(encoding="utf-8"))
    s = report_mod.aggregate(base_summary["cases"])
    base_measure = decide.Measurement(
        composite=s["composite"], judge=s["judge_overall_mean"], facts_recall=s["facts_recall"],
        severity=s["severity_exact"], diary_status_all_ok=s["diary_status_all_ok"],
        errors=s["errors"], cells=s["cells"], tokens=0, summary=s)
    ok, args.baseline_fixture_recall, err = gates.fixture_recall(
        REPO, args.python, Path(args.work) / "baseline-fake-eval")
    if not ok:
        print(f"기준 픽스처 재현율 측정 실패: {err}", file=sys.stderr)
        return 2

    print(f"기준: 종합 {base_measure.composite} · judge {base_measure.judge} · "
          f"재현율 {base_measure.facts_recall} · 발생단계 {base_measure.severity}")
    print(f"브랜치 {BRANCH} @ {tip()[:9]} · 저널 {len(journal.records)}건")

    for _ in range(args.max_iters):
        if args.token_budget and journal.spent_tokens() >= args.token_budget:
            print(f"토큰 예산 소진 ({journal.spent_tokens():,} ≥ {args.token_budget:,}) — 중단")
            break
        rec, base_measure, base_summary = run_iteration(args, journal, base_summary, base_measure)
        journal.append(rec)
        icon = {"accepted": "✅", "rejected": "❌", "error": "💥"}.get(rec.decision, "?")
        print(f"{icon} #{rec.iter} [{rec.target.get('cell', '-')}] {rec.hypothesis or '-'}\n   → {rec.reason}")
        if journal.consecutive_rejects() >= args.plateau:
            print(f"\n연속 {args.plateau}회 개선 실패 — 프롬프트·매핑 데이터로는 여기까지다.")
            if not args.no_retro and not args.dry_run:
                print("구조 개선 제안서를 작성한다(자동 적용하지 않음)...")
                path, err = retro.write_proposal(
                    REPO, base_measure.summary, base_measure.cells,
                    Journal().md.read_text(encoding="utf-8") if Journal().md.exists() else "",
                    model=args.model)
                print(f"  → {path}" if path else f"  제안서 작성 실패: {err}")
            break

    print(f"\n저널: docs/eval-journal.md · 수락 커밋: git log --oneline {BRANCH}")
    print(f"최종: 종합 {base_measure.composite} · judge {base_measure.judge} · 재현율 {base_measure.facts_recall}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
