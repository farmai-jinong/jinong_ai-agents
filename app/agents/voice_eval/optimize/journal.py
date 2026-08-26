"""저널 — 반복마다의 실행기록. 재개·중복 가설 회피·플래토 감지·셀 쿨다운의 상태 소스.

두 파일 다 git 추적한다(`out/` 은 gitignore 라 기록이 날아간다):
  docs/eval-journal.jsonl  기계용 (이 모듈이 읽고 쓴다)
  docs/eval-journal.md     사람용 누적 로그 (반복마다 한 절 append)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
JSONL = REPO / "docs" / "eval-journal.jsonl"
MD = REPO / "docs" / "eval-journal.md"

MD_HEADER = """# 자가 개선 루프 저널

`python -m app.agents.voice_eval.optimize` 가 반복마다 append 하는 누적 기록이다.
기계용 원본은 `eval-journal.jsonl`; 이 파일은 사람이 읽는 뷰다.

종합점수 = judge 50% + 기대추출 재현율 30% + 발생단계 정확도 20% (`voice_eval/report.py:composite`).
수락된 변경은 `eval/auto-tune` 브랜치에만 커밋된다 — `main` 은 루프가 건드리지 않는다.

---
"""


@dataclass
class Record:
    iter: int
    ts: str
    base_commit: str
    target: dict[str, Any] = field(default_factory=dict)      # {cell, cause, kind, section, count}
    hypothesis: str = ""
    why: str = ""
    files: list[str] = field(default_factory=list)
    diff_stat: str = ""
    gates: dict[str, Any] = field(default_factory=dict)
    screen: dict[str, Any] = field(default_factory=dict)      # {composite, delta, ...}
    confirm: dict[str, Any] = field(default_factory=dict)
    decision: str = "rejected"                                # accepted | rejected | error
    reason: str = ""
    tokens: dict[str, Any] = field(default_factory=dict)
    claude_session_id: str | None = None
    commit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Journal:
    def __init__(self, jsonl: Path = JSONL, md: Path = MD) -> None:
        self.jsonl = jsonl
        self.md = md
        self.records: list[Record] = []
        if jsonl.exists():
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self.records.append(Record(**json.loads(line)))

    # ------------------------------------------------------------------ 상태 질의
    @property
    def next_iter(self) -> int:
        return (max((r.iter for r in self.records), default=0)) + 1

    def tried_hypotheses(self, limit: int = 20) -> list[dict[str, str]]:
        """최근 시도 목록 — 브리핑에 넣어 같은 가설을 다시 내지 않게 한다."""
        return [{"cell": r.target.get("cell", ""), "hypothesis": r.hypothesis,
                 "decision": r.decision, "reason": r.reason} for r in self.records[-limit:]]

    def cooled_cells(self, cooldown: int) -> set[str]:
        """최근 `cooldown` 회 안에 거부당한 셀 — 같은 벽에 반복해 부딪히지 않게 건너뛴다.

        `cooldown <= 0` 은 "쿨다운 없음". (`records[-0:]` 는 전체 슬라이스라 그냥 넘기면 정반대로 동작한다.)
        """
        if cooldown <= 0:
            return set()
        return {r.target.get("cell", "") for r in self.records[-cooldown:]
                if r.decision != "accepted" and r.target.get("cell")}

    def consecutive_rejects(self) -> int:
        n = 0
        for r in reversed(self.records):
            if r.decision == "accepted":
                break
            n += 1
        return n

    def spent_tokens(self) -> int:
        return sum(int(v or 0) for r in self.records for v in r.tokens.values())

    # ------------------------------------------------------------------ 기록
    def append(self, rec: Record) -> None:
        self.records.append(rec)
        self.jsonl.parent.mkdir(parents=True, exist_ok=True)
        with self.jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        if not self.md.exists():
            self.md.write_text(MD_HEADER, encoding="utf-8")
        with self.md.open("a", encoding="utf-8") as f:
            f.write(render_record(rec))


def _delta(cur: Any, base: Any) -> str:
    if not isinstance(cur, (int, float)) or not isinstance(base, (int, float)):
        return ""
    d = cur - base
    return " (=)" if abs(d) < 1e-9 else f" ({'▲' if d > 0 else '▼'}{abs(d):.4f})"


def render_record(r: Record) -> str:
    icon = {"accepted": "✅ 수락", "rejected": "❌ 거부", "error": "💥 오류"}.get(r.decision, r.decision)
    L = [f"\n## #{r.iter} — {icon} · {r.ts}", ""]
    L.append(f"- **타깃 셀**: `{r.target.get('cell', '-')}` ({r.target.get('count', 0)}건)")
    L.append(f"- **가설**: {r.hypothesis or '-'}")
    if r.why:
        L.append(f"- **근거**: {r.why}")
    L.append(f"- **베이스**: `{r.base_commit[:9]}`" + (f" → 커밋 `{r.commit[:9]}`" if r.commit else ""))
    if r.files:
        L.append(f"- **변경 파일**: {', '.join(f'`{f}`' for f in r.files)}" + (f" ({r.diff_stat})" if r.diff_stat else ""))
    failed = [k for k, v in r.gates.items() if v not in (True, "ok", None)]
    L.append(f"- **게이트**: {'전부 통과' if not failed else '실패 — ' + ', '.join(failed)}")

    if r.screen or r.confirm:
        L += ["", "| 단계 | 종합점수 | judge | 재현율 | 발생단계 | 타깃 셀 |", "|---|---|---|---|---|---|"]
        for name, d in (("스크리닝", r.screen), ("확정(judge×3)", r.confirm)):
            if not d:
                continue
            L.append(f"| {name} | {d.get('composite', '—')}{_delta(d.get('composite'), d.get('base_composite'))} "
                     f"| {d.get('judge', '—')} | {d.get('facts_recall', '—')} | {d.get('severity', '—')} "
                     f"| {d.get('target_before', '—')} → {d.get('target_after', '—')} |")
    L += ["", f"- **판정 사유**: {r.reason or '-'}"]
    if r.claude_session_id:
        L.append(f"- 제안 세션: `claude --resume {r.claude_session_id}`")
    if r.tokens:
        L.append(f"- 토큰: {', '.join(f'{k}={v:,}' for k, v in r.tokens.items() if v)}")
    return "\n".join(L) + "\n"
