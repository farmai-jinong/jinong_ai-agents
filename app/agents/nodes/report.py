"""컨설팅 보고서 — LLM 서술 1회 + 결정적 개요; 실패 시 CallFacts 로 결정적 대체."""

from __future__ import annotations

import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from ..deps import get_deps
from ..llm import structured_call
from ..prompts.loader import PROMPT_VERSION, load_system, render_user
from ..render.markdown import cited_turns, render_report
from ..schemas import ActionItem, Bullet, CallFacts, ReportJSON, ReportNarrative, ReportResult
from ..state import PipelineState
from ..tools.transcript import fmt_ts, role_label
from ._common import call_when_text, err, participants_view

log = logging.getLogger(__name__)
_VERIFY = re.compile(r"(농약|약제|살포|희석|배액|비료|시비|양액|\bEC\b|ppm|\d+\s*배\b|리터|\bml\b|\bkg\b|그램|용량|안전사용|독성)", re.I)


def _all_evidence(f: CallFacts) -> list[int]:
    ev: list[int] = []
    for name in ("farm_status", "farmworks", "observations", "pests", "products", "questions", "advice", "actions", "follow_ups"):
        for x in getattr(f, name):
            ev.extend(x.evidence)
    return sorted(set(ev))


def deterministic_narrative(f: CallFacts) -> ReportNarrative:
    def b(text: str, ev: list[int]) -> Bullet:
        return Bullet(text=text, evidence=ev, needs_verification=bool(_VERIFY.search(text)))
    farm = [b(x.text, x.evidence) for x in f.farm_status]
    issues = [b(x.text, x.evidence) for x in f.questions] + [b(f"{p.name} {p.status}" + (f" ({p.severity_raw})" if p.severity_raw else ""), p.evidence) for p in f.pests]
    advice = [b(f"[{a.category}] {a.text}", a.evidence) for a in f.advice]
    actions = [b(x.text, x.evidence) for x in f.actions if x.actor == "farmer"]
    follow = [b(x.text + (f" ({x.when_hint})" if x.when_hint else ""), x.evidence) for x in f.follow_ups]
    items = [ActionItem(owner=a.actor, text=a.text, due_hint=a.due_hint, evidence=a.evidence) for a in f.actions if a.status != "done"]
    return ReportNarrative(farm_status=farm, issues=issues, advice=advice, farmer_actions=actions, follow_ups=follow,
                           summary_line=f.one_line_summary, keywords=f.keywords, action_items=items)


def _validate(n: ReportNarrative, valid: set[int]) -> tuple[ReportNarrative, list[str]]:
    dropped: list[str] = []
    def keep(bs: list[Bullet]) -> list[Bullet]:
        out = []
        for x in bs:
            x.evidence = [e for e in x.evidence if e in valid]
            if not x.evidence:
                dropped.append(x.text)
                continue
            if _VERIFY.search(x.text):
                x.needs_verification = True
            out.append(x)
        return out
    n.farm_status, n.issues, n.advice = keep(n.farm_status), keep(n.issues), keep(n.advice)
    n.farmer_actions, n.follow_ups = keep(n.farmer_actions), keep(n.follow_ups)
    items = []
    for a in n.action_items:
        a.evidence = [e for e in a.evidence if e in valid]
        if a.evidence:
            items.append(a)
        else:
            dropped.append(a.text)
    n.action_items = items
    return n, dropped


async def build_report(state: PipelineState, config) -> dict:  # type: ignore[no-untyped-def]
    deps = get_deps(config)
    facts: CallFacts = state["facts"]
    nt = state["transcript"]
    ctx = state["ctx"]
    valid = {t.tid for t in nt.turns}
    warnings: list[str] = []
    usage = []
    errors = []
    if facts.is_blank():
        n = deterministic_narrative(facts)
        warnings.append("추출된 사실이 없어 개요만 작성됨")
    else:
        ev = _all_evidence(facts)
        ev_text = "\n".join(f"#{t.tid} [{fmt_ts(t.abs_start)}] {role_label(t, nt.n_files)}: {t.text}" for t in cited_turns(nt, ev, 80))
        msgs = [SystemMessage(content=load_system("report")),
                HumanMessage(content=render_user("report", call_when=call_when_text(ctx), participants=participants_view(ctx),
                                                 facts_json=json.dumps(facts.model_dump(), ensure_ascii=False, indent=1),
                                                 evidence_text=ev_text))]
        try:
            n, trace = await structured_call(deps.llm, ReportNarrative, msgs, name="report",
                                             mode=deps.settings.llm_structured_mode, dump_dir=deps.dump_dir,
                                             timeout=deps.settings.node_timeout_s)
            usage.append(trace.usage())
            n, dropped = _validate(n, valid)
            if dropped:
                warnings.append(f"근거 없는 항목 {len(dropped)}건 제외")
        except Exception as e:  # noqa: BLE001
            log.warning("report LLM failed: %s", e)
            n = deterministic_narrative(facts)
            n, _ = _validate(n, valid)
            errors.append(err("build_report", e))
            warnings.append("보고서 서술 LLM 생성 실패 — 사실 목록으로 대체")
    crops = [t.prdlst_nm for t in state.get("crop_targets", []) if t.resolved]
    sr = state.get("speaker_roles")
    needs = [b.text for sec in (n.farm_status, n.issues, n.advice, n.farmer_actions, n.follow_ups) for b in sec if b.needs_verification]
    rj = ReportJSON(summary=n.summary_line, keywords=n.keywords, action_items=n.action_items,
                    sections={"farm_status": n.farm_status, "issues": n.issues, "advice": n.advice,
                              "farmer_actions": n.farmer_actions, "follow_ups": n.follow_ups},
                    speaker_map=list(sr.files) if sr else [], needs_verification=needs)
    md = render_report(n, ctx, nt, speaker_roles=sr, crops=crops, warnings=warnings + list(state.get("warnings") or [])[:5],
                       model=getattr(deps.llm, "model_name", None) or deps.settings.llm_model,
                       prompt_version=PROMPT_VERSION, now=deps.clock())
    return {"report": ReportResult(markdown=md, json=rj), "usage": usage, "errors": errors, "warnings": warnings}
