"""`correct_terms` 노드 — turn 텍스트의 용어 오청을 카탈로그 표기로 치환한다(LLM 1콜 + 결정적 적용).

`prepare_transcript` 뒤에 `load_farm_context`·`assign_speaker_roles` 와 **병렬**로 돌아 지연을 숨긴다(둘 다 교정된 용어가
필요 없다). `assign_speaker_roles` 가 같은 `transcript` 객체의 turn.role 을 제자리에서 채우고 그 객체를 되돌려주므로, 여기서는
`transcript` 키를 **다시 쓰지 않고** turn.text 만 제자리에서 바꾼다 — 같은 스텝에서 한 키를 둘이 쓰면 LangGraph 가 거부한다.
실패하면 원문 그대로 두고 경고만 남긴다. `state["raw"]`(API 로 나가는 원 전사)는 건드리지 않는다 — 바뀌는 것은
LLM 이 보는 turn 과 그 근거 인용뿐이며, 치환 내역은 `term_fix` 메타로 결과에 실린다.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..deps import get_deps
from ..nodes._common import err
from ..state import PipelineState
from ..tools.transcript import est_tokens, format_turns
from .apply import apply_corrections
from .catalog import get_catalog
from .run import propose

log = logging.getLogger("term_fix")


async def correct_terms(state: PipelineState, config) -> dict:  # type: ignore[no-untyped-def]
    deps = get_deps(config)
    s = deps.settings
    if not s.term_fix_enabled or not s.term_fix_catalog_path:
        return {"term_fix": None}
    nt = state["transcript"]
    if not nt.turns:
        return {"term_fix": None}
    t0 = time.perf_counter()
    try:
        catalog = get_catalog(s.term_fix_catalog_path, s.term_fix_stopwords_path)
        segments = [{"speaker": t.speaker_letter, "text": t.text} for t in nt.turns]     # seg_id = turn index
        hints = state["ctx"].hints
        crops = [hints.prdlst_nm] if hints and hints.prdlst_nm else []
        proposals, traces = await propose(deps.llm, segments, catalog, crops=crops, name="term_fix",
                                          mode=s.llm_structured_mode, dump_dir=deps.dump_dir, timeout=s.node_timeout_s)
        fixed, applied = apply_corrections(segments, proposals, catalog, min_confidence=s.term_fix_min_confidence,
                                           max_per_segment=s.term_fix_max_per_segment)
    except Exception as e:  # noqa: BLE001 — 교정 실패는 치명적이지 않다
        log.warning("correct_terms failed: %s", e)
        return {"term_fix": {"status": "failed", "error": f"{type(e).__name__}: {e}"[:200],
                             "elapsed_s": round(time.perf_counter() - t0, 1)},
                "errors": [err("correct_terms", e, fatal=False)],
                "warnings": ["용어 교정 실패 — 전사 원문 그대로 사용"]}
    for i, t in enumerate(nt.turns):
        if fixed[i]["text"] != t.text:
            t.text = fixed[i]["text"]
    nt.est_tokens = est_tokens(format_turns(nt.turns, nt.n_files))
    ok = [a for a in applied if a.status == "applied"]
    meta: dict[str, Any] = {
        "status": "ok", "n_applied": len(ok), "n_rejected": len(applied) - len(ok),
        "applied": [{"tid": nt.turns[a.seg_id].tid, "original": a.original, "replacement": a.replacement,
                     "category": a.category, "confidence": a.confidence} for a in ok],
        "rejected": [{"tid": nt.turns[a.seg_id].tid if 0 <= a.seg_id < len(nt.turns) else a.seg_id,
                      "original": a.original, "replacement": a.replacement, "why": a.why}
                     for a in applied if a.status != "applied"],
        "elapsed_s": round(time.perf_counter() - t0, 1), "n_calls": len(traces),
        "prompt_tokens": sum(t.prompt_tokens for t in traces),
        "completion_tokens": sum(t.completion_tokens for t in traces),
    }
    return {"term_fix": meta, "usage": [t.usage() for t in traces]}
