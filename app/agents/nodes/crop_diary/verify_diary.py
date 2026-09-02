"""일지 검수 — 렌더된 초안을 독립 LLM 패스로 다시 보고, 실질 내용이 없으면 EMPTY 로 강등한다.

`render_diary` 의 규칙 판정(CropFacts.is_empty)은 "추출된 사실이 하나라도 있는가"만 보기 때문에,
잡담에서 관찰 1건이 잘못 뽑히면 모든 칸이 `언급 없음` 인 빈 템플릿이 status=OK 로 나간다.
이 노드는 그 결과물을 사람이 보듯 다시 읽고 판정한다. 강등만 하고 승격은 하지 않는다.
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from ...deps import get_deps
from ...llm import structured_call
from ...prompts.loader import load_system, render_user
from ...schemas import DiaryResult, DiaryVerdictOut
from ...state import CropDiaryState
from .._common import err
from .render_diary import EMPTY_PRAISE, render_both

log = logging.getLogger(__name__)

SKIP_STATUSES = ("EMPTY", "UNRESOLVED_CROP")


def strip_lead_quotes(markdown: str) -> str:
    """상단 `> 📝 요약 / > 💬 격려` 인용 블록을 떼어낸다 — 통화 요약·격려는 이 작물의 일지 내용이 아니므로 판정 근거에서 뺀다."""
    lines = markdown.splitlines()
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    return "\n".join(lines[i:]).lstrip("\n")


async def verify_diary(state: CropDiaryState, config) -> dict:  # type: ignore[no-untyped-def]
    deps = get_deps(config)
    d: DiaryResult = state["diary"]
    if not deps.settings.verify_diary_enabled or d.status in SKIP_STATUSES:
        return {}                                   # 이미 빈 판정이거나 검수 비활성 — LLM 호출 없음
    msgs = [SystemMessage(content=load_system("verify_diary")),
            HumanMessage(content=render_user("verify_diary", crop_name=d.prdlst_nm, diary_date=d.diary_date,
                                             diary_markdown=strip_lead_quotes(d.markdown), content=d.content))]
    try:
        out, trace = await structured_call(deps.llm, DiaryVerdictOut, msgs,
                                           name=f"verify_diary_{d.prdlst_code or 'x'}",
                                           mode=deps.settings.llm_structured_mode, dump_dir=deps.dump_dir,
                                           timeout=deps.settings.node_timeout_s)
    except Exception as e:  # noqa: BLE001 — 검수 실패가 생성을 막지 않는다(fail-open)
        log.warning("verify_diary failed: %s", e)
        return {"errors": [err("verify_diary", e)],
                "warnings": [f"{d.prdlst_nm}: 일지 실질내용 검수 실패 — 규칙 판정 유지"]}

    d.verify = out
    if out.has_diary_content or out.confidence < deps.settings.verify_diary_min_confidence:
        return {"diary": d, "usage": [trace.usage()]}

    # 강등 — 같은 템플릿을 EMPTY 로 다시 렌더하고 prefill 을 거둔다(농가 앱에 빈 초안을 밀어 넣지 않기 위해)
    d.status = "EMPTY"
    d.prefill = None
    d.prefill_ready = False
    d.praise = EMPTY_PRAISE
    d.warnings.append(f"검수: 실질 영농일지 내용 없음 — {out.reason}".strip().rstrip("—").strip())
    render_both(d, state, state["crop_facts"], deps)          # internal·public 둘 다 EMPTY 로 다시 렌더
    return {"diary": d, "usage": [trace.usage()],
            "warnings": [f"{d.prdlst_nm}: 검수에서 실질 내용 없음으로 판정 — EMPTY"]}
