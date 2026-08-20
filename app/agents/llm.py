"""구조화 출력 사다리 + 사용량 기록.

`structured_call(llm, schema, messages)`:
  json_schema(with_structured_output) → json_mode(스키마를 프롬프트에) → 순수 텍스트 JSON 추출
  → ValidationError 시 1회 repair. 프로세스별로 처음 성공한 모드를 캐시한다.
`function_calling` 은 쓰지 않는다 (EXAONE/vLLM tool-call 지원 불확실).
Gemini(langchain-google-genai) 도 같은 사다리를 탄다: `method="json_schema"` → response_json_schema(제약 디코딩),
`bind(response_format={"type":"json_object"})` → response_mime_type=application/json 으로 자동 변환된다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from .schemas import LLMUsage

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

MODES = ("json_schema", "json_mode", "prompt")
_MODE_CACHE: dict[str, str] = {}


class StructuredOutputError(Exception):
    pass


@dataclass
class CallTrace:
    name: str
    mode: str = ""
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str | None = None
    raw: str = ""
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)

    def usage(self) -> LLMUsage:
        return LLMUsage(name=self.name, model=self.model, prompt_tokens=self.prompt_tokens,
                        completion_tokens=self.completion_tokens, total_tokens=self.total_tokens,
                        mode=self.mode, attempts=self.attempts)


# --------------------------------------------------------------------------- helpers
def _model_name(llm: Any) -> str | None:
    for attr in ("model_name", "model"):
        v = getattr(llm, attr, None)
        if isinstance(v, str):
            return v
    return None


def _cache_key(llm: Any) -> str:
    return f"{type(llm).__name__}:{_model_name(llm)}:{getattr(llm, 'openai_api_base', None) or getattr(llm, 'base_url', None)}"


def _accumulate(trace: CallTrace, msg: Any) -> None:
    um = getattr(msg, "usage_metadata", None)
    if isinstance(um, dict):
        trace.prompt_tokens += int(um.get("input_tokens") or 0)
        trace.completion_tokens += int(um.get("output_tokens") or 0)
        trace.total_tokens += int(um.get("total_tokens") or 0)
    rm = getattr(msg, "response_metadata", None)
    if isinstance(rm, dict) and rm.get("model_name") and not trace.model:
        trace.model = rm.get("model_name")


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json_text(text: str) -> str:
    """코드펜스 제거 → 첫 `{`..마지막 `}` (또는 `[`..`]`) 추출."""
    if not text:
        return ""
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    text = text.strip()
    for o, c in (("{", "}"), ("[", "]")):
        i, j = text.find(o), text.rfind(c)
        if i != -1 and j > i:
            cand = text[i:j + 1]
            try:
                json.loads(cand)
                return cand
            except ValueError:
                continue
    return text


def _msg_text(msg: Any) -> str:
    c = getattr(msg, "content", msg)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return str(c)


def _schema_json(schema: type[BaseModel]) -> str:
    return json.dumps(schema.model_json_schema(), ensure_ascii=False)


def _with_schema_prompt(messages: list[BaseMessage], schema: type[BaseModel]) -> list[BaseMessage]:
    note = ("\n\n[출력 형식] 아래 JSON 스키마를 만족하는 JSON 객체 **하나만** 출력한다. 설명·코드펜스·주석 금지. "
            "선택 필드는 null, 목록은 빈 배열로 채운다.\n" + _schema_json(schema))
    out = list(messages)
    if out and isinstance(out[0], SystemMessage):
        out[0] = SystemMessage(content=_msg_text(out[0]) + note)
    else:
        out.insert(0, SystemMessage(content=note.strip()))
    return out


def dump_trace(dump_dir: str | None, name: str, messages: list[BaseMessage], response: str, n: int) -> None:
    if not dump_dir:
        return
    try:
        d = Path(dump_dir) / "trace"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}_{n}.prompt.txt").write_text(
            "\n\n".join(f"### {type(m).__name__}\n{_msg_text(m)}" for m in messages), encoding="utf-8")
        (d / f"{name}_{n}.response.txt").write_text(response, encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.debug("trace dump failed", exc_info=True)


# --------------------------------------------------------------------------- ladder
async def _try_json_schema(llm: Any, schema: type[T], messages: list[BaseMessage], trace: CallTrace) -> T:
    runnable = llm.with_structured_output(schema, method="json_schema", include_raw=True)
    out = await runnable.ainvoke(messages)
    if isinstance(out, dict):
        raw = out.get("raw")
        if raw is not None:
            _accumulate(trace, raw)
            trace.raw = _msg_text(raw)
        if out.get("parsing_error"):
            raise StructuredOutputError(str(out["parsing_error"]))
        parsed = out.get("parsed")
        if parsed is None:
            raise StructuredOutputError("json_schema returned no parsed object")
        return parsed if isinstance(parsed, schema) else schema.model_validate(parsed)
    if isinstance(out, schema):
        return out
    return schema.model_validate(out)


async def _try_json_mode(llm: Any, schema: type[T], messages: list[BaseMessage], trace: CallTrace) -> T:
    msgs = _with_schema_prompt(messages, schema)
    try:
        bound = llm.bind(response_format={"type": "json_object"})
    except Exception:  # noqa: BLE001
        bound = llm
    resp = await bound.ainvoke(msgs)
    _accumulate(trace, resp)
    trace.raw = _msg_text(resp)
    return schema.model_validate_json(extract_json_text(trace.raw))


async def _try_prompt(llm: Any, schema: type[T], messages: list[BaseMessage], trace: CallTrace) -> T:
    msgs = _with_schema_prompt(messages, schema)
    resp = await llm.ainvoke(msgs)
    _accumulate(trace, resp)
    trace.raw = _msg_text(resp)
    return schema.model_validate_json(extract_json_text(trace.raw))


_RUNNERS = {"json_schema": _try_json_schema, "json_mode": _try_json_mode, "prompt": _try_prompt}


def _is_unsupported(e: Exception) -> bool:
    s = str(e).lower()
    return any(k in s for k in ("400", "unsupported", "not supported", "invalid_request", "response_format",
                                "not implemented", "notimplemented", "does not support", "unrecognized",
                                "structured", "attribute", "bad request"))


async def structured_call(llm: Any, schema: type[T], messages: list[BaseMessage], *, name: str,
                          mode: str = "auto", repair_rounds: int = 1, dump_dir: str | None = None,
                          timeout: float | None = None) -> tuple[T, CallTrace]:
    trace = CallTrace(name=name, model=_model_name(llm))
    key = _cache_key(llm)
    if mode == "auto":
        cached = _MODE_CACHE.get(key)
        order = [cached] + [m for m in MODES if m != cached] if cached else list(MODES)
    else:
        order = [mode]
    t0 = time.perf_counter()
    last_err: Exception | None = None
    for m in order:
        runner = _RUNNERS[m]
        msgs = list(messages)
        for r in range(repair_rounds + 1):
            trace.attempts += 1
            try:
                coro = runner(llm, schema, msgs, trace)
                obj = await (asyncio.wait_for(coro, timeout) if timeout else coro)
                trace.mode = m
                trace.elapsed_s = time.perf_counter() - t0
                _MODE_CACHE[key] = m
                dump_trace(dump_dir, name, msgs, trace.raw, trace.attempts)
                return obj, trace
            except (ValidationError, StructuredOutputError, ValueError) as e:
                # 형식은 통했으나 내용이 스키마 위반 → repair 1회
                last_err = e
                trace.errors.append(f"{m}: {type(e).__name__}: {str(e)[:300]}")
                dump_trace(dump_dir, name, msgs, trace.raw, trace.attempts)
                if r < repair_rounds and trace.raw:
                    msgs = list(messages) + [
                        AIMessage(content=trace.raw[:6000]),
                        HumanMessage(content="위 출력은 JSON 스키마를 위반했다. 오류: "
                                     f"{str(e)[:800]}\n같은 내용을 스키마에 맞는 JSON 객체 하나로만 다시 출력하라."),
                    ]
                    continue
                break
            except TimeoutError as e:
                last_err = e
                trace.errors.append(f"{m}: timeout")
                raise
            except Exception as e:  # noqa: BLE001 — 전송 모드 미지원 등 → 다음 모드
                last_err = e
                trace.errors.append(f"{m}: {type(e).__name__}: {str(e)[:300]}")
                if m == "prompt" and not _is_unsupported(e):
                    # 순수 네트워크 오류 등은 즉시 전파
                    raise
                break
    trace.elapsed_s = time.perf_counter() - t0
    raise StructuredOutputError(f"{name}: structured output failed: {trace.errors[-3:]}") from last_err


async def text_call(llm: Any, messages: list[BaseMessage], *, name: str, dump_dir: str | None = None,
                    timeout: float | None = None) -> tuple[str, CallTrace]:
    trace = CallTrace(name=name, model=_model_name(llm), mode="text", attempts=1)
    t0 = time.perf_counter()
    coro = llm.ainvoke(messages)
    resp = await (asyncio.wait_for(coro, timeout) if timeout else coro)
    _accumulate(trace, resp)
    trace.raw = _msg_text(resp)
    trace.elapsed_s = time.perf_counter() - t0
    dump_trace(dump_dir, name, messages, trace.raw, 1)
    return trace.raw, trace


def reset_mode_cache() -> None:
    _MODE_CACHE.clear()


def env_has_llm_key() -> bool:
    return bool(os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"))
