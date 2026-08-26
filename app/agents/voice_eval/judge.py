"""LLM judge — 산출 일지를 정답 기준·통화 전사에 비추어 채점한다.

파이프라인과 **다른 모델**을 쓴다(자기채점 편향 회피). 설정은 `JUDGE_PROVIDER`/`JUDGE_MODEL`,
CLI `--judge-provider`/`--judge-model` 로 덮어쓴다. 호출은 기존 `structured_call` 사다리 그대로.
"""

from __future__ import annotations

import logging
import statistics
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ...clients.llm import make_chat_model
from ...config import Settings
from ..llm import structured_call
from ..prompts.loader import load_system, render_user
from ..schemas import DiaryJudgeOut
from .cases import VoiceCase

log = logging.getLogger("voice_eval.judge")

DIMENSIONS = ("coverage", "faithfulness", "classification", "severity", "chatter", "format")
CAUSES = ("stt", "extraction", "mapping", "rendering", "unknown")
KINDS = ("missing", "hallucinated", "misclassified", "chatter_leak")


def make_judge_llm(settings: Settings, provider: str | None = None, model: str | None = None) -> Any:
    """judge 전용 챗 모델. 파이프라인 Settings 를 복사해 provider/model 만 갈아끼운다(원본은 그대로).

    `gemini_thinking_level` 은 파이프라인 값(low)을 물려받지 않고 `JUDGE_THINKING_LEVEL` 을 쓴다 —
    gemini-2.5-* 는 그 파라미터 자체를 400 으로 거부한다.
    """
    s = settings.model_copy(deep=True)
    s.llm_provider = provider or settings.judge_provider or settings.llm_provider
    s.llm_model = model or settings.judge_model or settings.llm_model
    s.llm_temperature = 0.0
    s.gemini_thinking_level = settings.judge_thinking_level
    return make_chat_model(s)


async def probe_judge(llm: Any) -> None:
    """모델이 실제로 응답하는지 한 번 확인한다 — 없는 모델 ID 로 5케이스를 태우고 나서 실패하지 않도록."""
    from langchain_core.messages import HumanMessage as _HM
    try:
        await llm.ainvoke([_HM(content="ping")])
    except Exception as e:  # noqa: BLE001
        name = getattr(llm, "model_name", None) or getattr(llm, "model", "?")
        raise RuntimeError(
            f"judge 모델 '{name}' 호출 실패 — JUDGE_MODEL / --judge-model 을 확인할 것.\n  {type(e).__name__}: {e}"
        ) from e


async def judge_once(llm: Any, case: VoiceCase, actual_diary: str, transcript_text: str, *,
                     settings: Settings, dump_dir: str | None = None) -> tuple[DiaryJudgeOut, Any]:
    msgs = [
        SystemMessage(content=load_system("judge_diary", preamble=False)),
        HumanMessage(content=render_user(
            "judge_diary", crop_name=case.crop_name, case_name=case.name,
            expected_diary=case.expected_diary, actual_diary=actual_diary,
            transcript_text=transcript_text)),
    ]
    return await structured_call(llm, DiaryJudgeOut, msgs, name=f"judge_{case.name}",
                                 mode=settings.llm_structured_mode, dump_dir=dump_dir,
                                 timeout=settings.node_timeout_s)


async def judge(llm: Any, case: VoiceCase, actual_diary: str, transcript_text: str, *,
                settings: Settings, repeat: int = 1, dump_dir: str | None = None) -> dict[str, Any]:
    """repeat 회 채점해 축별 점수는 중앙값, items 는 합집합(중복 제거)으로 합친다."""
    runs: list[DiaryJudgeOut] = []
    tokens = 0
    for _ in range(max(1, repeat)):
        out, trace = await judge_once(llm, case, actual_diary, transcript_text,
                                      settings=settings, dump_dir=dump_dir)
        runs.append(out)
        tokens += trace.total_tokens
    return aggregate(runs, tokens=tokens, model=getattr(llm, "model_name", None) or settings.judge_model)


def aggregate(runs: list[DiaryJudgeOut], *, tokens: int = 0, model: str | None = None) -> dict[str, Any]:
    dims: dict[str, int] = {}
    for name in DIMENSIONS:
        vals = [d.score for r in runs for d in r.dimensions if d.name == name]
        if vals:
            dims[name] = int(statistics.median(vals))
    reasons = {d.name: d.reason for r in runs for d in r.dimensions}
    seen: set[tuple[str, str, str]] = set()
    items: list[dict[str, Any]] = []
    for r in runs:
        for it in r.items:
            key = (it.kind, it.section, it.text.strip())
            if key in seen:
                continue
            seen.add(key)
            items.append(it.model_dump())
    return {
        "dimensions": dims,
        "dimension_reasons": reasons,
        "items": items,
        "overall": int(statistics.median([r.overall for r in runs])) if runs else 0,
        "summary": runs[0].summary if runs else "",
        "runs": len(runs),
        "tokens": tokens,
        "model": model,
    }


def cause_matrix(judges: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """원인 × 종류 교차표 — 다음에 뭘 고칠지 정하는 표."""
    m = {c: dict.fromkeys(KINDS, 0) for c in CAUSES}
    for j in judges:
        for it in j.get("items", []):
            cause = it.get("cause") or "unknown"
            kind = it.get("kind") or "missing"
            if cause in m and kind in m[cause]:
                m[cause][kind] += 1
    return m


def cell_key(item: dict[str, Any]) -> str:
    """감점 항목 → `cause/kind/section` 셀 키. 자가 개선 루프의 타깃 단위."""
    return f"{item.get('cause') or 'unknown'}/{item.get('kind') or 'missing'}/{(item.get('section') or '').strip()}"


def cells(judges: list[dict[str, Any]]) -> dict[str, int]:
    """셀 키 → 건수. 큰 셀이 다음 개선 타깃이 된다."""
    out: dict[str, int] = {}
    for j in judges:
        for it in j.get("items", []):
            k = cell_key(it)
            out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))
