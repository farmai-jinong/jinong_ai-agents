"""기타 기록사항(content) — 소형 LLM 패스; 실패 시 사실 bullet 로 결정적 대체."""

from __future__ import annotations

import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage

from ...deps import get_deps
from ...llm import structured_call
from ...prompts.loader import load_system, render_user
from ...render.markdown import cited_turns
from ...schemas import CropFacts, DiaryContentOut, NormalizedTranscript
from ...state import CropDiaryState
from ...tools.transcript import fmt_ts, role_label
from .._common import err

log = logging.getLogger(__name__)
MAX_CHARS = 500


def facts_evidence(cf: CropFacts) -> list[int]:
    ev: list[int] = []
    for group in (cf.farmworks, cf.observations, cf.pests, cf.products, cf.follow_ups, cf.actions):
        for x in group:
            ev.extend(x.evidence)
    return sorted(set(ev))


def evidence_text(transcript: NormalizedTranscript, ev: list[int], cap: int = 60) -> str:
    return "\n".join(f"#{t.tid} [{fmt_ts(t.abs_start)}] {role_label(t, transcript.n_files)}: {t.text}"
                     for t in cited_turns(transcript, ev, cap))


def deterministic_content(cf: CropFacts, unmatched_fw: list[str]) -> DiaryContentOut:
    lines = ["[AI 초안·통화 기반]"]
    ev: list[int] = []
    for o in cf.observations[:4]:
        lines.append(f"- {o.text}")
        ev.extend(o.evidence)
    for p in cf.pests[:3]:
        loc = f" ({p.location})" if p.location else ""
        lines.append(f"- {p.name} {p.status}{loc}" + (f", 정도: {p.severity_raw or p.severity}" if p.severity != '불명' else ""))
        ev.extend(p.evidence)
    for name in unmatched_fw[:3]:
        lines.append(f"- {name} 함 (표준 목록 미등록)")
    for f in [f for f in cf.farmworks if f.when == "planned"][:3]:
        lines.append(f"- {f.name} 예정" + (f" ({f.date_hint})" if f.date_hint else ""))
        ev.extend(f.evidence)
    for pr in [p for p in cf.products if p.dose][:2]:
        lines.append(f"- {pr.name} {pr.dose} (확인 필요)")
        ev.extend(pr.evidence)
    text = "\n".join(lines[:8])
    return DiaryContentOut(content=text[:MAX_CHARS], evidence=sorted(set(ev)))


def _trim(text: str) -> str:
    text = text.strip()
    if not text.startswith("[AI 초안"):
        text = "[AI 초안·통화 기반]\n" + text
    lines = [ln for ln in text.splitlines() if ln.strip()][:8]
    return "\n".join(lines)[:MAX_CHARS]


async def write_content(state: CropDiaryState, config) -> dict:  # type: ignore[no-untyped-def]
    deps = get_deps(config)
    cf: CropFacts = state["crop_facts"]
    rep = state["mapping"]
    unmatched_fw = [m.source for m in rep.farmworks if m.status in ("unmatched", "ambiguous")]
    if cf.is_empty():
        return {"content": None}
    ev = facts_evidence(cf)
    msgs = [SystemMessage(content=load_system("diary_content")),
            HumanMessage(content=render_user("diary_content", crop_name=state["target"].prdlst_nm,
                                             diary_date=state["diary_date"],
                                             facts_json=json.dumps(cf.model_dump(exclude={"warnings"}), ensure_ascii=False, indent=1),
                                             evidence_text=evidence_text(state["transcript"], ev)))]
    try:
        out, trace = await structured_call(deps.llm, DiaryContentOut, msgs, name=f"diary_content_{state['target'].prdlst_code or 'x'}",
                                           mode=deps.settings.llm_structured_mode, dump_dir=deps.dump_dir,
                                           timeout=deps.settings.node_timeout_s)
        out.content = _trim(out.content)
        valid = set(t.tid for t in state["transcript"].turns)
        out.evidence = [e for e in out.evidence if e in valid] or ev
        return {"content": out, "usage": [trace.usage()]}
    except Exception as e:  # noqa: BLE001
        log.warning("write_content failed: %s", e)
        return {"content": deterministic_content(cf, unmatched_fw), "errors": [err("write_content", e)],
                "warnings": [f"{state['target'].prdlst_nm}: 기타 기록사항 LLM 생성 실패 — 사실 요약으로 대체"]}
