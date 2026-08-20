"""녹취문 → CallFacts (LLM 1회; 길면 turn 청크 → 결정적 병합 + 요약 통합)."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..deps import get_deps
from ..llm import StructuredOutputError, structured_call
from ..mapping.matcher import dedupe_texts
from ..prompts.loader import load_system, render_user
from ..schemas import CallFacts, LLMUsage, SummaryOut, Turn
from ..state import PipelineState
from ..tools.transcript import chunk_turns, format_turns
from ._common import call_when_text, err, participants_view

log = logging.getLogger(__name__)


def _speaker_note(state: PipelineState) -> str:
    sr = state.get("speaker_roles")
    if not sr or not sr.files:
        return "미식별 (화자A/B 로만 표기됨)"
    parts = []
    for f in sr.files:
        if f.confidence >= 0.6 and f.mapping:
            parts.append(f"파일{f.file_index + 1}: " + ", ".join(f"{k}={v}" for k, v in f.mapping.items()))
        else:
            parts.append(f"파일{f.file_index + 1}: 불확실 (화자 글자 그대로)")
    return "; ".join(parts)


def _hints_text(ctx) -> str:  # type: ignore[no-untyped-def]
    h = ctx.hints
    parts = []
    if h.prdlst_nm or h.prdlst_code:
        parts.append(f"대상 작물 힌트: {h.prdlst_nm or ''} {h.prdlst_code or ''}".strip())
    if h.topic:
        parts.append(f"주제: {h.topic}")
    return "; ".join(parts)


def merge_facts(parts: list[CallFacts]) -> CallFacts:
    """청크별 CallFacts 를 결정적으로 병합 (텍스트 유사 ≥90 dedupe)."""
    if len(parts) == 1:
        return parts[0]
    base = CallFacts.empty()
    def merge_list(name: str, textkey) -> None:  # type: ignore[no-untyped-def]
        items: list[Any] = []
        for p in parts:
            items.extend(getattr(p, name))
        keep = dedupe_texts([textkey(i) for i in items])
        setattr(base, name, [items[i] for i in keep])
    merge_list("crops_mentioned", lambda x: x.name_raw)
    merge_list("farm_status", lambda x: x.text)
    merge_list("farmworks", lambda x: f"{x.name}|{x.when}|{x.crop or ''}")
    merge_list("observations", lambda x: x.text)
    merge_list("pests", lambda x: f"{x.name}|{x.status}|{x.crop or ''}")
    merge_list("products", lambda x: f"{x.name}|{x.when}|{x.crop or ''}")
    merge_list("questions", lambda x: x.text)
    merge_list("advice", lambda x: x.text)
    merge_list("actions", lambda x: x.text)
    merge_list("follow_ups", lambda x: x.text)
    base.has_farmwork_content = any(p.has_farmwork_content for p in parts)
    seen: set[str] = set()
    for p in parts:
        for u in p.stt_uncertainties:
            if u not in seen:
                seen.add(u)
                base.stt_uncertainties.append(u)
    kws: list[str] = []
    for p in parts:
        for k in p.keywords:
            if k not in kws:
                kws.append(k)
    base.keywords = kws[:8]
    base.one_line_summary = " / ".join(p.one_line_summary for p in parts if p.one_line_summary)[:300]
    return base


def _valid_evidence(facts: CallFacts, max_tid: int) -> CallFacts:
    """범위 밖 tid 제거."""
    def fix(items: list[Any]) -> None:
        for it in items:
            it.evidence = [e for e in it.evidence if isinstance(e, int) and 0 <= e <= max_tid]
    for name in ("crops_mentioned", "farm_status", "farmworks", "observations", "pests", "products",
                 "questions", "advice", "actions", "follow_ups"):
        fix(getattr(facts, name))
    return facts


async def _extract_chunk(deps, state: PipelineState, turns: list[Turn], chunk_note: str, n: int) -> tuple[CallFacts, LLMUsage]:  # type: ignore[no-untyped-def]
    ctx = state["ctx"]
    farm = state.get("farm")
    crops = [c.prdlstNm for c in (farm.crops if farm else [])]
    msgs = [SystemMessage(content=load_system("extract")),
            HumanMessage(content=render_user("extract", call_when=call_when_text(ctx),
                                             participants=participants_view(ctx),
                                             speaker_note=_speaker_note(state), crops=crops,
                                             hints=_hints_text(ctx), chunk_note=chunk_note,
                                             transcript=format_turns(turns, state["transcript"].n_files)))]
    out, trace = await structured_call(deps.llm, CallFacts, msgs, name=f"extract_{n}",
                                       mode=deps.settings.llm_structured_mode, dump_dir=deps.dump_dir,
                                       timeout=deps.settings.node_timeout_s)
    return out, trace.usage()


async def extract_facts(state: PipelineState, config) -> dict:  # type: ignore[no-untyped-def]
    deps = get_deps(config)
    s = deps.settings
    nt = state["transcript"]
    if not nt.turns:
        return {"facts": CallFacts.empty("전사 없음"), "facts_meta": {"chunks": 0}}
    usage: list[LLMUsage] = []
    meta: dict[str, Any] = {}
    max_tid = nt.turns[-1].tid
    try:
        if nt.est_tokens <= s.extract_max_input_tokens:
            facts, u = await _extract_chunk(deps, state, nt.turns, "", 0)
            usage.append(u)
            meta = {"chunks": 1, "mode": u.mode}
            return {"facts": _valid_evidence(facts, max_tid), "facts_meta": meta, "usage": usage}
        chunks = chunk_turns(nt.turns, s.chunk_tokens, s.chunk_overlap_turns)
        parts: list[CallFacts] = []
        prev = ""
        for i, ch in enumerate(chunks):
            note = f"이 녹취문은 전체 {len(chunks)}구간 중 {i + 1}번째 구간이다."
            if prev:
                note += f" 이전 구간 요약: {prev}"
            try:
                f, u = await _extract_chunk(deps, state, ch, note, i)
            except StructuredOutputError:
                # 청크를 반으로 재시도
                half = len(ch) // 2 or 1
                f1, u1 = await _extract_chunk(deps, state, ch[:half], note, i * 100)
                f2, u2 = await _extract_chunk(deps, state, ch[half:], note, i * 100 + 1)
                f, u = merge_facts([f1, f2]), u1
                usage.append(u2)
            usage.append(u)
            parts.append(f)
            prev = f.one_line_summary
        merged = merge_facts(parts)
        try:
            msgs = [SystemMessage(content=load_system("extract_merge")),
                    HumanMessage(content=render_user("extract_merge", summaries=[
                        {"one_line_summary": p.one_line_summary, "keywords": p.keywords} for p in parts]))]
            so, tr = await structured_call(deps.llm, SummaryOut, msgs, name="extract_merge",
                                           mode=s.llm_structured_mode, dump_dir=deps.dump_dir,
                                           timeout=s.node_timeout_s)
            merged.one_line_summary = so.one_line_summary or merged.one_line_summary
            merged.keywords = so.keywords or merged.keywords
            usage.append(tr.usage())
        except Exception as e:  # noqa: BLE001
            log.warning("extract_merge failed: %s", e)
        meta = {"chunks": len(chunks)}
        return {"facts": _valid_evidence(merged, max_tid), "facts_meta": meta, "usage": usage}
    except Exception as e:  # noqa: BLE001
        log.error("extract_facts failed: %s", e)
        return {"facts": CallFacts.empty(f"추출 실패: {type(e).__name__}"), "facts_meta": {"error": str(e)},
                "usage": usage, "errors": [err("extract_facts", e, fatal=False)],
                "warnings": ["사실 추출 실패 — 일지/보고서를 생성하지 못함(전사만 저장됨)"]}
