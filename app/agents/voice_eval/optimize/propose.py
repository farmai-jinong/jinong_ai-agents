"""제안 — 격리된 git worktree 에서 Claude 세션을 돌려 가설 하나 + 최소 변경을 받는다.

worktree 를 쓰는 이유: 제안이 실패해도 작업 트리가 그대로고, 변경 집합이 `git diff` 로 정확히 잡히며,
측정을 원본과 병렬로 돌릴 수 있다. worktree 는 **커밋된 것만** 복제하므로 `.env`(비밀)와
`out/voice-eval/*/fixture.json`(동결된 전사)은 직접 넣어 준다.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...llm import extract_json_text
from .gates import ALLOWLIST, DENYLIST

log = logging.getLogger("optimize.propose")

SYSTEM_RULES = """너는 농업 통화→영농일지 생성 파이프라인의 **프롬프트 튜너**다. 평가 하네스가 지목한 감점 하나를 고치는 것이 유일한 임무다.

[불변 규칙]
- 수정 가능한 파일은 아래 allowlist 뿐이다. 그 밖의 파일은 **읽기만** 하라. 하나라도 고치면 변경 전체가 자동 폐기된다.
- 가설은 **하나만** 세우고, 그 가설을 검증할 **최소 변경**만 하라. 여러 개를 한꺼번에 고치면 무엇이 효과가 있었는지 알 수 없어 전부 버려진다.
- **특정 테스트케이스의 제품명·병해충명·농가명을 프롬프트에 적지 마라.** 정답을 박아 넣는 순간 자동 거부된다. 언제나 일반 규칙으로 써라 — "사파이어는 잿빛곰팡이병 약이다"(금지) 대신 "농가가 사용을 거부한 약제는 투입 제품에 넣지 않는다"(허용).
- 평가를 직접 돌리지 마라(`voice_eval` 실행 금지). 측정은 바깥 하네스가 통제한다. `pytest -q` 로 문법·회귀만 확인하는 것은 허용된다.
- 테스트·정답 기준(`expected_diary.md`·`expect.json`)·채점 프롬프트(`judge_diary.*`)는 열람만 가능하다.

[작업 순서]
1. 브리핑의 감점 항목과 정답↔산출 대조를 읽는다.
2. 왜 지금 프롬프트가 그 내용을 놓치는지 한 문장으로 진단한다.
3. allowlist 파일에 최소 변경을 가한다.
4. 마지막 메시지에 아래 JSON **하나만** 출력한다(설명 산문 금지).

{"hypothesis": "한 줄 가설", "why": "왜 이 변경이 그 감점을 고치는가", "files": ["바꾼 파일 경로"],
 "expected": {"cause": "...", "kind": "...", "section": "...", "from": 5, "to": 2}}
"""


@dataclass
class Proposal:
    ok: bool
    hypothesis: str = ""
    why: str = ""
    files: list[str] = None            # type: ignore[assignment]
    expected: dict[str, Any] = None    # type: ignore[assignment]
    session_id: str | None = None
    tokens: int = 0
    error: str = ""

    def __post_init__(self) -> None:
        self.files = self.files or []
        self.expected = self.expected or {}


# --------------------------------------------------------------------------- worktree
def make_worktree(repo: Path, wt: Path, base: str, eval_out: Path) -> None:
    """`base` 커밋으로 worktree 를 만들고, 커밋되지 않는 것들(비밀·전사 캐시)을 채워 넣는다."""
    wt.parent.mkdir(parents=True, exist_ok=True)
    if wt.exists():
        remove_worktree(repo, wt)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), base],
                   check=True, capture_output=True)
    _exclude_injected(wt)
    env = repo / ".env"
    if env.exists():
        shutil.copyfile(env, wt / ".env")
    # 동결된 전사 — 루프 내내 재전사하지 않으므로 fixture/stt 캐시만 있으면 된다
    dest_root = wt / "out" / "voice-eval"
    for case_dir in sorted(p for p in eval_out.iterdir() if p.is_dir() and not p.name.startswith("_")):
        dest = dest_root / case_dir.name
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("stt.json", "stt.md", "stt_score.json", "fixture.json"):
            if (case_dir / name).exists():
                shutil.copyfile(case_dir / name, dest / name)


def _exclude_injected(wt: Path) -> None:
    """하네스가 넣을 파일을 worktree 전용 exclude 에 등록 — 리포지토리의 .gitignore 와 무관하게 안 보이게."""
    from .gates import INJECTED
    gitdir = subprocess.run(["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"],
                            capture_output=True, text=True, check=True).stdout.strip()
    info = Path(gitdir) / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "exclude").write_text("\n".join(["# 자가 개선 루프가 넣어 준 것들", *INJECTED, ""]), encoding="utf-8")


def remove_worktree(repo: Path, wt: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                   capture_output=True)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)


# --------------------------------------------------------------------------- Claude 세션
def _allowlist_note() -> str:
    return ("수정 가능:\n" + "\n".join(f"  - `{a}`" for a in ALLOWLIST)
            + "\n금지(읽기만):\n" + "\n".join(f"  - `{d}`" for d in DENYLIST if "**" not in d))


def run_claude(wt: Path, briefing: str, *, model: str | None = None, max_turns: int = 30,
               timeout: float = 1800.0) -> Proposal:
    """`claude -p` 로 제안 세션을 돌린다. 반환값은 파싱된 최종 JSON."""
    session_id = str(uuid.uuid4())
    prompt = briefing + "\n\n---\n\n위 브리핑의 타깃 셀을 고치는 가설 하나를 세우고 최소 변경을 적용한 뒤, 규정된 JSON 을 출력하라."
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--max-turns", str(max_turns),
        "--session-id", session_id,
        "--append-system-prompt", SYSTEM_RULES + "\n\n[allowlist]\n" + _allowlist_note(),
        "--allowedTools", "Read", "Edit", "Grep", "Glob", "Bash(pytest*)",
        "--disallowedTools", "WebFetch", "WebSearch", "Write",
    ]
    if model:
        cmd += ["--model", model]
    env = {**os.environ, "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"}
    try:
        r = subprocess.run(cmd, cwd=str(wt), capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return Proposal(False, session_id=session_id, error=f"claude 세션 타임아웃 ({timeout:.0f}s)")
    if r.returncode != 0:
        return Proposal(False, session_id=session_id,
                        error=f"claude 종료코드 {r.returncode}: {(r.stderr or r.stdout)[-400:]}")
    return parse_response(r.stdout, session_id)


def parse_response(stdout: str, session_id: str | None = None) -> Proposal:
    """`--output-format json` 의 결과 봉투에서 최종 텍스트를 꺼내 제안 JSON 을 파싱한다."""
    try:
        envelope = json.loads(stdout)
    except ValueError:
        return Proposal(False, session_id=session_id, error=f"claude 응답이 JSON 이 아님: {stdout[:200]}")
    if isinstance(envelope, list):                      # stream-json 폴백
        envelope = next((e for e in reversed(envelope) if isinstance(e, dict) and e.get("result")), {})
    text = envelope.get("result") or ""
    usage = envelope.get("usage") or {}
    tokens = int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
    sid = envelope.get("session_id") or session_id
    if envelope.get("is_error"):
        return Proposal(False, session_id=sid, tokens=tokens, error=f"claude 오류: {str(text)[:300]}")
    try:
        d = json.loads(extract_json_text(text if isinstance(text, str) else json.dumps(text)))
    except ValueError as e:
        return Proposal(False, session_id=sid, tokens=tokens,
                        error=f"제안 JSON 파싱 실패({e}): {str(text)[-300:]}")
    if not isinstance(d, dict) or not d.get("hypothesis"):
        return Proposal(False, session_id=sid, tokens=tokens, error=f"제안에 hypothesis 가 없다: {str(d)[:200]}")
    return Proposal(True, hypothesis=str(d.get("hypothesis", "")), why=str(d.get("why", "")),
                    files=[str(f) for f in (d.get("files") or [])],
                    expected=d.get("expected") or {}, session_id=sid, tokens=tokens)
