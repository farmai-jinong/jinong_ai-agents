"""회고 — 프롬프트로 못 넘는 벽에 부딪혔을 때 **구조 개선 제안서**를 쓴다.

allowlist(프롬프트·매핑 데이터)로 고칠 수 없는 감점이 남아 루프가 계속 거부되면, 별도 Claude 세션이
저널 전체와 현재 원인 귀속 표를 읽고 `docs/proposals/NNN-<slug>.md` 를 쓴다. **자동 적용하지 않는다** —
5개 케이스와 LLM judge 로 구조 변경을 자동 수락하는 것은 위험하다. 사람에게 넘기고 루프는 멈춘다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("optimize.retro")

PROPOSALS = Path(__file__).resolve().parents[4] / "docs" / "proposals"

SYSTEM = """너는 이 파이프라인의 아키텍트다. 프롬프트 튜닝만으로 개선이 멈춘 상태에서, **구조 변경 제안서**를 쓴다.

[규칙]
- 코드를 고치지 마라. 오직 제안서 마크다운 한 편만 쓴다.
- 감점이 왜 프롬프트로 못 고쳐지는지 **코드의 특정 지점**(파일:줄)을 짚어 설명하라. 추측이 아니라 실제로 읽고 써라.
- 제안은 3개 이내. 각각 [문제 / 근거(어느 케이스의 어느 감점) / 제안 변경 / 예상 효과 / 리스크·되돌리기] 구성.
- 예상 효과는 평가 지표로 말하라(어느 셀이 몇 건 줄어드는가).
- 저널에서 이미 시도해 실패한 방향은 다시 제안하지 마라.
"""

TASK = """저널(`docs/eval-journal.md`)과 아래 현황을 읽고, 프롬프트·매핑 데이터로는 고칠 수 없는 구조적 원인을 찾아 제안서를 써라.

파이프라인 코드는 `app/agents/` 아래에 있다. 특히 다음이 감점과 얽혀 있는지 직접 읽고 확인하라:
- `app/agents/nodes/crop_diary/map_facts.py` — 방제이력 편입 조건, 농작업 체크 조건
- `app/agents/render/markdown.py` — 향후 계획 목록 조립, 계획 중복 제거
- `app/agents/nodes/select_crops.py` — 작물별 사실 라우팅
- `app/agents/mapping/matcher.py` — 매칭 임계값

## 현재 지표
{metrics}

## 남은 감점 (원인/종류/섹션 별)
{cells}

## 저널 요약 (최근 시도와 결과)
{journal}

제안서를 `{path}` 에 쓰고, 마지막 메시지에는 `{{"proposals": ["제목1", "제목2"]}}` JSON 만 출력하라.
"""


def _slug(text: str) -> str:
    s = re.sub(r"[^0-9A-Za-z가-힣]+", "-", text).strip("-").lower()
    return (s[:40] or "structural") if s else "structural"


def next_path(title_hint: str) -> Path:
    PROPOSALS.mkdir(parents=True, exist_ok=True)
    n = max((int(p.name[:3]) for p in PROPOSALS.glob("[0-9][0-9][0-9]-*.md")), default=0) + 1
    return PROPOSALS / f"{n:03d}-{_slug(title_hint)}.md"


def write_proposal(repo: Path, metrics: dict[str, Any], cells: dict[str, int], journal_md: str,
                   *, model: str | None = None, timeout: float = 1800.0) -> tuple[Path | None, str]:
    """(제안서 경로, 오류메시지). 실패해도 루프를 죽이지 않는다."""
    path = next_path("structural-" + (next(iter(cells), "improvements").split("/")[0]))
    task = TASK.format(
        metrics=json.dumps(metrics, ensure_ascii=False, indent=1),
        cells="\n".join(f"- `{k}` — {v}건" for k, v in list(cells.items())[:12]) or "- (없음)",
        journal=journal_md[-6000:] or "(저널 없음)",
        path=path.relative_to(repo),
    )
    cmd = ["claude", "-p", task, "--output-format", "json", "--permission-mode", "acceptEdits",
           "--max-turns", "40", "--session-id", str(uuid.uuid4()),
           "--append-system-prompt", SYSTEM,
           "--allowedTools", "Read", "Grep", "Glob", "Write", "Edit",
           "--disallowedTools", "WebFetch", "WebSearch", "Bash"]
    if model:
        cmd += ["--model", model]
    try:
        r = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, timeout=timeout,
                           env={**os.environ})
    except subprocess.TimeoutExpired:
        return None, "회고 세션 타임아웃"
    if r.returncode != 0:
        return None, f"회고 세션 실패({r.returncode}): {(r.stderr or r.stdout)[-300:]}"
    return (path, "") if path.exists() else (None, "제안서 파일이 생성되지 않았다")
