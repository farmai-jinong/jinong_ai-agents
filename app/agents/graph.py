"""LangGraph 조립 — 메인 그래프 + 작물별 서브그래프(Send fan-out).

START → prepare_transcript → {load_farm_context, assign_speaker_roles} → extract_facts → select_crops
      → Send("build_crop_diary")×N ‖ build_report → finalize → END
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..clients.ap_backend import ApBackendClient
from ..clients.farmos import FarmosClient
from ..clients.llm import make_chat_model
from ..config import Settings
from ..schemas.pipeline import CallContext, PipelineResult
from ..schemas.transcript import MergedTranscript
from .deps import Deps
from .interface import PipelineEmpty
from .nodes._common import err
from .nodes.crop_diary.disambiguate import disambiguate
from .nodes.crop_diary.fetch_refs import fetch_refs
from .nodes.crop_diary.map_facts import map_facts
from .nodes.crop_diary.render_diary import render_diary_node
from .nodes.crop_diary.verify_diary import verify_diary
from .nodes.crop_diary.write_content import write_content
from .nodes.extract_facts import extract_facts
from .nodes.farm_context import load_farm_context
from .nodes.finalize import assemble, finalize
from .nodes.prepare_transcript import prepare_transcript
from .nodes.report import build_report
from .nodes.select_crops import select_crops
from .nodes.speaker_roles import assign_speaker_roles
from .prompts.loader import PROMPT_VERSION
from .schemas import CropFacts, DiaryResult
from .state import CropDiaryState, PipelineState

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- crop-diary subgraph
def _needs_llm(state: CropDiaryState) -> str:
    return "disambiguate" if state["mapping"].ambiguous() else "write_content"


def build_crop_diary_graph():  # type: ignore[no-untyped-def]
    g = StateGraph(CropDiaryState)
    g.add_node("fetch_refs", fetch_refs)
    g.add_node("map_facts", map_facts)
    g.add_node("disambiguate", disambiguate)
    g.add_node("write_content", write_content)
    g.add_node("render_diary", render_diary_node)
    g.add_node("verify_diary", verify_diary)
    g.add_edge(START, "fetch_refs")
    g.add_edge("fetch_refs", "map_facts")
    g.add_conditional_edges("map_facts", _needs_llm, {"disambiguate": "disambiguate", "write_content": "write_content"})
    g.add_edge("disambiguate", "write_content")
    g.add_edge("write_content", "render_diary")
    g.add_edge("render_diary", "verify_diary")
    g.add_edge("verify_diary", END)
    return g.compile()


_CROP_GRAPH = None


def crop_graph():  # type: ignore[no-untyped-def]
    global _CROP_GRAPH
    if _CROP_GRAPH is None:
        _CROP_GRAPH = build_crop_diary_graph()
    return _CROP_GRAPH


async def build_crop_diary(state: CropDiaryState, config) -> dict:  # type: ignore[no-untyped-def]
    """Send 페이로드(작물 1개)를 받아 서브그래프 실행 → diaries 에 1건 추가."""
    target = state["target"]
    try:
        out = await crop_graph().ainvoke(
            {"ctx": state["ctx"], "diary_date": state["diary_date"], "transcript": state["transcript"],
             "farm": state.get("farm"), "target": target, "crop_facts": state["crop_facts"],
             "diaries": [], "errors": [], "usage": [], "warnings": []},
            config=config)
        return {"diaries": [out["diary"]], "errors": out.get("errors") or [], "usage": out.get("usage") or [],
                "warnings": out.get("warnings") or []}
    except Exception as e:  # noqa: BLE001
        log.exception("crop diary subgraph failed for %s", target.prdlst_nm)
        d = DiaryResult(prdlst_code=target.prdlst_code, prdlst_nm=target.prdlst_nm, diary_date=state["diary_date"],
                        status="EMPTY", warnings=[f"일지 생성 실패: {type(e).__name__}"])
        return {"diaries": [d], "errors": [err("build_crop_diary", e)],
                "warnings": [f"{target.prdlst_nm}: 일지 생성 실패"]}


def fan_out_crops(state: PipelineState) -> list[Send]:
    sends: list[Send] = []
    for t in state.get("crop_targets") or []:
        key = t.prdlst_code or t.prdlst_nm
        cf = (state.get("crop_facts") or {}).get(key, CropFacts())
        sends.append(Send("build_crop_diary", {
            "ctx": state["ctx"], "diary_date": state["diary_date"], "transcript": state["transcript"],
            "farm": state.get("farm"), "target": t, "crop_facts": cf}))
    return sends


# --------------------------------------------------------------------------- main graph
def build_graph(checkpointer: Any = None):  # type: ignore[no-untyped-def]
    g = StateGraph(PipelineState)
    g.add_node("prepare_transcript", prepare_transcript)
    g.add_node("load_farm_context", load_farm_context)
    g.add_node("assign_speaker_roles", assign_speaker_roles)
    g.add_node("extract_facts", extract_facts)
    g.add_node("select_crops", select_crops)
    g.add_node("build_crop_diary", build_crop_diary)
    g.add_node("build_report", build_report)
    g.add_node("finalize", finalize)
    g.add_edge(START, "prepare_transcript")
    g.add_edge("prepare_transcript", "load_farm_context")
    g.add_edge("prepare_transcript", "assign_speaker_roles")
    g.add_edge(["load_farm_context", "assign_speaker_roles"], "extract_facts")
    g.add_edge("extract_facts", "select_crops")
    g.add_conditional_edges("select_crops", fan_out_crops, ["build_crop_diary"])
    g.add_edge("select_crops", "build_report")
    g.add_edge(["build_crop_diary", "build_report"], "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------- pipeline facade
def default_farmos_factory(settings: Settings) -> Callable[[str], FarmosClient]:
    def factory(token: str) -> FarmosClient:
        return FarmosClient(settings.farmos_base_url, token, timeout=settings.farmos_timeout)
    return factory


def default_ap_backend(settings: Settings) -> ApBackendClient | None:
    """AP 백엔드 research API — URL·키가 다 있을 때만. 없으면 기존대로 hints 강등."""
    if not (settings.ap_backend_base_url and settings.callback_api_key):
        return None
    return ApBackendClient(settings.ap_backend_base_url, settings.callback_api_key,
                           timeout=settings.ap_backend_timeout)


class LangGraphPipeline:
    """워커가 쓰는 파사드. `deps` 를 직접 주면(테스트/CLI) 그대로 사용."""

    def __init__(self, settings: Settings, deps: Deps | None = None, *, use_farmos: bool = True) -> None:
        self.settings = settings
        if deps is None:
            deps = Deps(settings=settings, llm=make_chat_model(settings),
                        farmos_factory=default_farmos_factory(settings) if use_farmos else None,
                        ap_backend=default_ap_backend(settings),
                        prompt_version=PROMPT_VERSION, dump_dir=settings.prompt_dump_dir or None)
        self.deps = deps
        self.graph = build_graph()

    async def run(self, transcript: MergedTranscript, ctx: CallContext) -> PipelineResult:
        if transcript.is_empty:
            raise PipelineEmpty("전사에 발화가 없음")
        config = {"configurable": {"deps": self.deps}, "recursion_limit": 60}
        state: PipelineState = {"ctx": ctx, "raw": transcript, "diaries": [], "errors": [], "usage": [], "warnings": []}
        out = await self.graph.ainvoke(state, config=config)
        result = assemble(out, config)
        all_empty = all(d.status in ("EMPTY", "UNRESOLVED_CROP") for d in result.diaries) if result.diaries else True
        report_blank = not result.report or not (result.report.structured.get("summary") or any(
            result.report.structured.get("sections", {}).get(k) for k in ("farm_status", "issues", "advice", "farmer_actions", "follow_ups")))
        # 생성 자체가 실패한 경우는 EMPTY 로 오보하지 않는다 — 전사는 남기고 COMPLETED 로 둔다
        degraded = bool((out.get("facts_meta") or {}).get("error")) or any(
            e.node in ("build_report", "verify_diary") for e in (out.get("errors") or []))
        if all_empty and report_blank and not degraded:
            raise PipelineEmpty("영농일지로 남길 실질 내용이 없음")
        return result
