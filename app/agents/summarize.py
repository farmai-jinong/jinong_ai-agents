"""통화 단순요약 — 영농일지 파이프라인과 **독립된** LLM 패스.

백엔드 통화요약 콜백(`.../voicetalk/public/call-summary-callback`)의 `content` 가 되는 짧은 불릿
(주제/조치/후속)을 전사에서 직접 뽑는다. 일지·보고서 산출물을 요약하는 게 아니라 녹취문을 다시 읽는다
— 일지 렌더 결과에 의존하지 않으므로 일지 서식이 바뀌어도 요약 문구는 흔들리지 않는다.

긴 통화(EXTRACT_MAX_INPUT_TOKENS 초과)는 `extract_facts` 와 같은 map-reduce: 구간 요약 → 통합 1회.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..clients.llm import make_chat_model
from ..config import Settings
from ..schemas.pipeline import CallContext, CallSummaryResult
from ..schemas.transcript import MergedTranscript
from .llm import structured_call
from .nodes._common import call_when_text, participants_view
from .nodes.prepare_transcript import build_turns
from .prompts.loader import load_system, render_user
from .schemas import Ev, _Strict
from .tools.transcript import chunk_turns, format_turns

log = logging.getLogger(__name__)

MAX_ACTIONS = 3
MAX_FOLLOW_UPS = 2
SUMMARY_MAX_TOKENS = 2048   # 본문은 200자 남짓이지만 gemini thinking 토큰이 같은 예산을 소비한다


class CallSummaryOut(_Strict):
    """통화 단순요약 LLM 출력 (프롬프트 `call_summary`)."""
    topic: str
    actions: list[str]
    follow_ups: list[str]
    evidence: Ev


def _clean(items: list[str], cap: int) -> list[str]:
    out: list[str] = []
    for x in items:
        v = " ".join((x or "").split()).lstrip("-•* ").strip()
        if v and v not in out:
            out.append(v)
    return out[:cap]


def render_summary(topic: str, actions: list[str], follow_ups: list[str]) -> str:
    """불릿 3~5줄로 결정적 렌더 — LLM 에 서식을 맡기지 않는다. 빈 항목 줄은 생략."""
    lines: list[str] = []
    topic = " ".join((topic or "").split())
    if topic:
        lines.append(f"- 주제: {topic}")
    acts, fups = _clean(actions, MAX_ACTIONS), _clean(follow_ups, MAX_FOLLOW_UPS)
    if acts:
        lines.append(f"- 조치: {' / '.join(acts)}")
    if fups:
        lines.append(f"- 후속: {' / '.join(fups)}")
    return "\n".join(lines)


def summary_from_report(report_structured: dict[str, Any] | None) -> CallSummaryResult | None:
    """폴백 — 요약 LLM 이 실패했을 때 이미 만들어진 보고서 요약/실행항목으로 같은 모양을 만든다."""
    st = report_structured or {}
    topic = " ".join(str(st.get("summary") or "").split())
    if not topic:
        return None
    actions = [str(a.get("text") or "") for a in (st.get("action_items") or []) if isinstance(a, dict)]
    md = render_summary(topic, actions, [])
    return CallSummaryResult(topic=topic, actions=_clean(actions, MAX_ACTIONS), markdown=md,
                             source="report_fallback")


class FakeSummarizer:
    """PIPELINE_IMPL=fake — LLM 없이 배선 확인용."""

    async def summarize(self, transcript: MergedTranscript, ctx: CallContext) -> CallSummaryResult:
        topic = f"(fake) 통화 세그먼트 {len(transcript.segments)}건"
        return CallSummaryResult(topic=topic, actions=["(fake) 조치"], follow_ups=[],
                                 markdown=render_summary(topic, ["(fake) 조치"], []),
                                 model="fake", source="fake")


class LlmSummarizer:
    def __init__(self, settings: Settings, llm: Any | None = None) -> None:
        self.settings = settings
        # 파이프라인의 LLM 인스턴스를 공유하지 않는다 — 요약 전용 상한을 따로 준다.
        # 출력 자체는 짧지만 gemini 는 thinking 토큰이 같은 예산을 쓴다 — 너무 낮으면 JSON 이 잘린다.
        self.llm = llm if llm is not None else make_chat_model(settings, max_tokens=SUMMARY_MAX_TOKENS)
        self.dump_dir = settings.prompt_dump_dir or None

    async def _call(self, msgs: list, tag: str) -> tuple[CallSummaryOut, dict[str, Any]]:
        out, trace = await structured_call(self.llm, CallSummaryOut, msgs, name=tag,
                                           mode=self.settings.llm_structured_mode, dump_dir=self.dump_dir,
                                           timeout=self.settings.node_timeout_s)
        return out, trace.usage().model_dump()

    def _chunk_msgs(self, ctx: CallContext, turns: list, n_files: int, chunk_note: str) -> list:
        return [SystemMessage(content=load_system("call_summary")),
                HumanMessage(content=render_user("call_summary", call_when=call_when_text(ctx),
                                                 participants=participants_view(ctx), chunk_note=chunk_note,
                                                 transcript=format_turns(turns, n_files)))]

    async def summarize(self, transcript: MergedTranscript, ctx: CallContext) -> CallSummaryResult:
        s = self.settings
        nt = build_turns(transcript)
        if not nt.turns:
            return CallSummaryResult(markdown="", source="llm")
        model = getattr(self.llm, "model_name", None) or s.llm_model

        if nt.est_tokens <= s.extract_max_input_tokens:
            out, usage = await self._call(self._chunk_msgs(ctx, nt.turns, nt.n_files, ""), "call_summary")
            return CallSummaryResult(topic=out.topic, actions=_clean(out.actions, MAX_ACTIONS),
                                     follow_ups=_clean(out.follow_ups, MAX_FOLLOW_UPS),
                                     markdown=render_summary(out.topic, out.actions, out.follow_ups),
                                     model=model, usage=usage)

        chunks = chunk_turns(nt.turns, s.chunk_tokens, s.chunk_overlap_turns)
        parts: list[CallSummaryOut] = []
        for i, ch in enumerate(chunks):
            note = f"이 녹취문은 전체 {len(chunks)}구간 중 {i + 1}번째 구간이다."
            try:
                out, _ = await self._call(self._chunk_msgs(ctx, ch, nt.n_files, note), f"call_summary_{i}")
                parts.append(out)
            except Exception as e:  # noqa: BLE001 — 구간 하나가 실패해도 나머지로 요약한다
                log.warning("call_summary chunk %d failed: %s", i, e)
        if not parts:
            raise RuntimeError("call_summary: 모든 구간 실패")

        msgs = [SystemMessage(content=load_system("call_summary_merge")),
                HumanMessage(content=render_user("call_summary_merge", summaries=[
                    {"topic": p.topic, "actions": p.actions, "follow_ups": p.follow_ups} for p in parts]))]
        try:
            merged, usage = await self._call(msgs, "call_summary_merge")
        except Exception as e:  # noqa: BLE001 — 통합 실패면 구간 요약을 결정적으로 이어붙인다
            log.warning("call_summary_merge failed: %s", e)
            merged = CallSummaryOut(topic=" / ".join(p.topic for p in parts if p.topic)[:200],
                                    actions=[a for p in parts for a in p.actions],
                                    follow_ups=[f for p in parts for f in p.follow_ups], evidence=[])
            usage = {}
        return CallSummaryResult(topic=merged.topic, actions=_clean(merged.actions, MAX_ACTIONS),
                                 follow_ups=_clean(merged.follow_ups, MAX_FOLLOW_UPS),
                                 markdown=render_summary(merged.topic, merged.actions, merged.follow_ups),
                                 model=model, usage=usage)
