"""진단 — 타깃 셀 선정 + 제안 세션에 줄 브리핑 생성. LLM 을 쓰지 않는다.

타깃 셀은 `cause/kind/section` 단위(`judge.cell_key`)다. 한 번에 하나만 잡아야 점수 변화를 그 변경에
귀속시킬 수 있고, "고치려던 셀이 실제로 줄었는가"라는 직접 증거를 쓸 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..cases import TESTCASES, load_case
from ..judge import cell_key
from .journal import Journal

REPO = Path(__file__).resolve().parents[4]
PROMPTS = REPO / "app" / "agents" / "prompts"

# 셀의 원인 → 그 원인을 실제로 고칠 수 있는 파일(브리핑에 원문을 붙여 준다)
CAUSE_PROMPTS = {
    "extraction": ["extract.system.md", "diary_content.system.md"],
    "mapping": ["extract.system.md"],
    "rendering": ["diary_content.system.md"],
    "unknown": ["extract.system.md"],
    "stt": [],
}
CAUSE_DATA = {
    "mapping": ["app/agents/mapping/synonyms.yaml", "app/agents/mapping/severity.yaml"],
}


@dataclass
class Target:
    cell: str
    cause: str
    kind: str
    section: str
    count: int
    items: list[dict[str, Any]]            # 이 셀에 속한 judge 항목 (+ case 키)

    def to_dict(self) -> dict[str, Any]:
        return {"cell": self.cell, "cause": self.cause, "kind": self.kind,
                "section": self.section, "count": self.count}


def pick_target(summary: dict[str, Any], journal: Journal, *, cooldown: int = 3,
                forced: str | None = None) -> Target | None:
    """가장 큰 셀. 최근 `cooldown` 회 안에 거부당한 셀은 건너뛴다(같은 벽 반복 방지).

    `stt` 원인 셀은 애초에 제외한다 — 프롬프트로 고칠 수 없는 전사 문제다.
    """
    by_cell: dict[str, list[dict[str, Any]]] = {}
    for row in summary.get("cases", []):
        for it in (row.get("judge") or {}).get("items", []):
            by_cell.setdefault(cell_key(it), []).append({**it, "case": row["case"]})

    if forced:
        items = by_cell.get(forced, [])
        cause, kind, section = (forced.split("/", 2) + ["", ""])[:3]
        return Target(forced, cause, kind, section, len(items), items)

    cooled = journal.cooled_cells(cooldown)
    ranked = sorted(by_cell.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for cell, items in ranked:
        cause = cell.split("/", 1)[0]
        if cause == "stt" or cell in cooled:
            continue
        _c, kind, section = (cell.split("/", 2) + ["", ""])[:3]
        return Target(cell, cause, kind, section, len(items), items)
    return None


def _rel(p: Path) -> str:
    """브리핑에 적을 경로 — 리포지토리 기준 상대경로로, 밖이면 그대로."""
    try:
        return str(p.resolve().relative_to(REPO))
    except ValueError:
        return str(p)


def _section_of(markdown: str, section: str) -> str:
    """일지 마크다운에서 `## <section>` 블록만 잘라낸다."""
    if not section:
        return ""
    lines = markdown.splitlines()
    out: list[str] = []
    inside = False
    for ln in lines:
        if ln.startswith("## "):
            if inside:
                break
            inside = ln[3:].strip() == section.strip()
        if inside:
            out.append(ln)
    return "\n".join(out)


def briefing(target: Target, summary: dict[str, Any], journal: Journal, out_dir: Path,
             allowlist_note: str) -> str:
    """제안 세션에 줄 브리핑. 타깃 셀의 감점 근거 + 관련 원문 + 이미 시도한 가설."""
    L: list[str] = [
        "# 개선 브리핑",
        "",
        f"## 타깃: `{target.cell}` — {target.count}건",
        "",
        f"- 원인 귀속: **{target.cause}** / 종류: **{target.kind}** / 일지 섹션: **{target.section}**",
        f"- 현재 종합점수: `{summary['summary'].get('composite')}` "
        f"(judge {summary['summary'].get('judge_overall_mean')} / 재현율 "
        f"{summary['summary'].get('facts_recall')} / 발생단계 {summary['summary'].get('severity_exact')})",
        "",
        "이 셀의 감점을 줄이는 것이 이번 반복의 목표다. 다른 셀을 건드려 이 셀이 늘면 거부된다.",
        "",
        "## 감점 항목 원문 (채점관이 쓴 것)",
        "",
    ]
    for it in target.items:
        L.append(f"- **{it['case']}** — {it['text']}")
    L.append("")

    L += ["## 케이스별 대조 (정답 기준 ↔ 실제 산출)", ""]
    for case_name in dict.fromkeys(it["case"] for it in target.items):
        case = load_case(case_name)
        row = next((r for r in summary["cases"] if r["case"] == case_name), {})
        diary_path = next(iter(sorted((out_dir / case_name).glob("diary_*.md"))), None)
        actual = _section_of(diary_path.read_text(encoding="utf-8"), target.section) if diary_path else ""
        expected = _section_of(case.expected_diary, target.section)
        L += [f"### {case_name}", "",
              f"judge 총평: {(row.get('judge') or {}).get('summary', '-')}", "",
              "**정답 기준 해당 섹션**", "", "```markdown", expected or "(해당 섹션 없음)", "```", "",
              "**실제 산출 해당 섹션**", "", "```markdown", actual or "(해당 섹션 없음)", "```", "",
              f"전사 전문: `{_rel(out_dir / case_name / 'stt.md')}` · "
              f"추출 사실: `{_rel(out_dir / case_name / 'facts.json')}`", ""]

    L += ["## 수정 가능 파일", "", allowlist_note, ""]
    for name in CAUSE_PROMPTS.get(target.cause, []):
        p = PROMPTS / name
        if p.exists():
            L += [f"### `app/agents/prompts/{name}` (현재 원문)", "", "```markdown",
                  p.read_text(encoding="utf-8").rstrip(), "```", ""]
    for rel in CAUSE_DATA.get(target.cause, []):
        L.append(f"- 데이터 파일도 볼 것: `{rel}`")

    tried = journal.tried_hypotheses()
    if tried:
        L += ["", "## 이미 시도한 가설 (반복 금지)", ""]
        for t in tried:
            L.append(f"- [{t['decision']}] `{t['cell']}` — {t['hypothesis']} → {t['reason']}")
    L += ["", "## 참고", "",
          f"- 케이스 정의: `{_rel(TESTCASES)}/<case>/` (script.md·expected_diary.md·expect.json)",
          "- 평가 실행은 하네스가 한다. 직접 `voice_eval` 을 돌리지 말 것(측정은 바깥에서 통제된다).", ""]
    return "\n".join(L)
