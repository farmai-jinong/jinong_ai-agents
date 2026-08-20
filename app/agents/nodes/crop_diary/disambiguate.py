"""ambiguous 항목만 LLM 에게 후보 중 선택하게 함 (후보 밖 값 거부)."""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from ...deps import get_deps
from ...llm import structured_call
from ...prompts.loader import load_system, render_user
from ...schemas import DisambiguateOut, MappedItem, MappingReport
from ...state import CropDiaryState
from .._common import err

log = logging.getLogger(__name__)
FAM_KO = {"farmwork": "농작업", "pest": "병해충", "product": "약제", "crop": "작물"}


def _context(item: MappedItem, transcript) -> str:  # type: ignore[no-untyped-def]
    texts = []
    for e in item.evidence[:2]:
        t = transcript.by_tid(e)
        if t:
            texts.append(t.text[:120])
    return " / ".join(texts)


def apply_pick(item: MappedItem, choice: str | None) -> None:
    if not choice:
        item.status = "unmatched"
        return
    for c in item.candidates:
        if str(c.get("code")) == str(choice):
            item.status = "matched"
            item.code, item.name, item.score, item.method = str(c["code"]), str(c.get("name")), float(c.get("score") or 0), "llm-pick"
            return
    item.status = "unmatched"
    item.warnings.append("LLM 이 후보 밖 값을 골라 무시함")


def _fill_payload(rep: MappingReport, refs) -> None:  # type: ignore[no-untyped-def]
    """LLM 선택 후 payload 보강 (병해충 단일값 / 약제 코드 / 농작업 항목)."""
    if refs is None:
        return
    for m in rep.pests:
        if m.status == "matched" and m.code and "dbyhsCode" not in m.payload:
            row = next((r for r in refs.dbyhs if r.dbyhs_code == m.code), None)
            if row:
                m.payload.update(row.single(int(m.payload.get("step_index", 1))))
    for m in rep.products:
        if m.status == "matched" and m.code and not m.payload.get("pestiCode"):
            row = next((r for r in refs.pesti_all if str(r.get("pestiCode")) == m.code), None)
            if row:
                m.payload.update({"pestiCode": row.get("pestiCode", ""), "pestiNm": row.get("pestiNm", "")})
    for m in rep.farmworks:
        if m.status == "matched" and m.code and not m.payload:
            row = next((r for r in refs.farmworks if str(r.get("userFarmworkId")) == m.code), None)
            if row:
                m.payload = dict(row)


async def disambiguate(state: CropDiaryState, config) -> dict:  # type: ignore[no-untyped-def]
    deps = get_deps(config)
    rep: MappingReport = state["mapping"]
    amb = rep.ambiguous()
    if not amb:
        return {}
    items = [{"item_id": m.item_id, "family_ko": FAM_KO[m.family], "source": m.source,
              "context": _context(m, state["transcript"]), "candidates": m.candidates} for m in amb]
    msgs = [SystemMessage(content=load_system("disambiguate")),
            HumanMessage(content=render_user("disambiguate", crop_name=state["target"].prdlst_nm, items=items))]
    try:
        out, trace = await structured_call(deps.llm, DisambiguateOut, msgs, name="disambiguate",
                                           mode=deps.settings.llm_structured_mode, dump_dir=deps.dump_dir,
                                           timeout=deps.settings.node_timeout_s)
        picks = {p.item_id: p.choice for p in out.picks}
        for m in amb:
            if m.item_id in picks:
                apply_pick(m, picks[m.item_id])
        _fill_payload(rep, state.get("refs"))
        return {"mapping": rep, "usage": [trace.usage()]}
    except Exception as e:  # noqa: BLE001
        log.warning("disambiguate failed: %s", e)
        return {"mapping": rep, "errors": [err("disambiguate", e)],
                "warnings": [f"{state['target'].prdlst_nm}: 후보 확정 실패 — 후보 목록으로 표기"]}
