"""LLM 호출 — 세그먼트 + 카탈로그 → `TermFixOut`. 기존 `structured_call` 사다리를 그대로 탄다.

세그먼트가 많으면 청크로 나누되 seg_id 는 전체 인덱스를 유지한다(카탈로그는 청크마다 반복 — ~15k 토큰).
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ...llm import CallTrace, structured_call
from ...prompts.loader import load_system, render_user
from .catalog import Catalog
from .schemas import TermCorrection, TermFixOut

log = logging.getLogger("voice_eval.term_fix")
CHUNK_SEGMENTS = 90


def _view(segments: list[dict[str, Any]], lo: int, hi: int) -> list[dict[str, Any]]:
    return [{"id": i, "speaker": str(s.get("speaker") or "?"), "text": (s.get("text") or "").strip()}
            for i, s in enumerate(segments) if lo <= i < hi and (s.get("text") or "").strip()]


async def propose(llm: Any, segments: list[dict[str, Any]], catalog: Catalog, *, crops: list[str],
                  name: str = "term_fix", mode: str = "auto", dump_dir: str | None = None,
                  timeout: float | None = None) -> tuple[list[TermCorrection], list[CallTrace]]:
    system = load_system("term_fix", preamble=False)
    block = catalog.prompt_block()
    n = len(segments)
    bounds = [(lo, min(lo + CHUNK_SEGMENTS, n)) for lo in range(0, max(n, 1), CHUNK_SEGMENTS)]
    out: list[TermCorrection] = []
    traces: list[CallTrace] = []
    for k, (lo, hi) in enumerate(bounds):
        view = _view(segments, lo, hi)
        if not view:
            continue
        note = f"전체 {n}개 발화 중 #{lo}~#{hi - 1} 구간" if len(bounds) > 1 else ""
        msgs = [SystemMessage(content=system),
                HumanMessage(content=render_user("term_fix", crops=crops, note=note, segments=view, catalog=block))]
        res, tr = await structured_call(llm, TermFixOut, msgs, name=f"{name}_{k}", mode=mode,
                                        dump_dir=dump_dir, timeout=timeout)
        traces.append(tr)
        out.extend(c for c in res.corrections if lo <= c.seg_id < hi)
    return out, traces
