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


def residual_facts(cf: CropFacts, unmatched_fw: list[str]) -> dict:
    """기타 기록사항 입력 = 다른 섹션이 결정적으로 렌더하지 않는 잔여 사실만.

    follow_ups·actions·planned/recommended 제품·planned 작업은 '향후 작업·확인 계획'이 이미 렌더하므로
    입력에서 제외한다 — 섹션 배타성을 프롬프트 문장이 아니라 입력 집합으로 보장한다.
    서술(note·detail)은 반드시 그 사실에 붙어 들어온다 — 어떤 사실에도 안 붙는 잡담은 담을 칸이 없다.
    """
    farmworks = []
    for f in cf.farmworks:
        if f.when == "planned":
            continue
        checked = f.when in ("today", "ongoing", "unknown") and f.name not in unmatched_fw
        if checked and not f.detail:
            continue                     # 체크 줄로 이미 표현됨 — 부가 서술이 있을 때만 산문으로
        farmworks.append({"name": f.name, "when": f.when, "date_hint": f.date_hint,
                          "detail": f.detail, "이미_체크됨": checked, "evidence": f.evidence})
    products = [{"name": p.name, "note": p.note, "date_hint": p.date_hint, "evidence": p.evidence}
                for p in cf.products if p.note and p.when not in ("planned", "recommended")]
    pests = [{"name": p.name, "note": p.note, "evidence": p.evidence} for p in cf.pests if p.note]
    return {"observations": [o.model_dump() for o in cf.observations],
            "farmworks": farmworks, "products": products, "pests": pests}


def residual_evidence(residual: dict) -> list[int]:
    ev: list[int] = []
    for group in residual.values():
        for x in group:
            ev.extend(x.get("evidence") or [])
    return sorted(set(ev))


def evidence_text(transcript: NormalizedTranscript, ev: list[int], cap: int = 60) -> str:
    return "\n".join(f"#{t.tid} [{fmt_ts(t.abs_start)}] {role_label(t, transcript.n_files)}: {t.text}"
                     for t in cited_turns(transcript, ev, cap))


def deterministic_content(cf: CropFacts, unmatched_fw: list[str]) -> DiaryContentOut:
    """LLM 실패 시 폴백 — residual_facts 와 같은 잔여 집합에서 bullet 을 만든다."""
    res = residual_facts(cf, unmatched_fw)
    lines = ["[AI 초안·통화 기반]"]
    ev: list[int] = []
    for o in res["observations"][:4]:
        lines.append(f"- {o['text']}")
        ev.extend(o.get("evidence") or [])
    for p in cf.pests[:3]:
        loc = f" ({p.location})" if p.location else ""
        lines.append(f"- {p.name} {p.status}{loc}" + (f", 정도: {p.severity_raw or p.severity}" if p.severity != '불명' else ""))
        ev.extend(p.evidence)
    for f in res["farmworks"][:3]:
        hint = f" ({f['date_hint']})" if f.get("date_hint") else ""
        detail = f" — {f['detail']}" if f.get("detail") else ""
        tail = " (표준 목록 미등록)" if (f["name"] in unmatched_fw and not f["이미_체크됨"]) else ""
        lines.append(f"- {f['name']} 함{hint}{detail}{tail}")
        ev.extend(f.get("evidence") or [])
    for pr in res["products"][:2]:
        lines.append(f"- {pr['name']}: {pr['note']}")
        ev.extend(pr.get("evidence") or [])
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
    residual = residual_facts(cf, unmatched_fw)
    ev = residual_evidence(residual) or facts_evidence(cf)
    msgs = [SystemMessage(content=load_system("diary_content")),
            HumanMessage(content=render_user("diary_content", crop_name=state["target"].prdlst_nm,
                                             diary_date=state["diary_date"],
                                             facts_json=json.dumps(residual, ensure_ascii=False, indent=1),
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
