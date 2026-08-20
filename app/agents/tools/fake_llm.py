"""테스트/드라이런용 가짜 챗 모델 — 프롬프트 종류를 감지해 캔드 JSON 을 돌려준다.

`responses` 는 kind → (dict | str | callable(messages) -> dict|str). kind ∈ speaker_roles | extract |
extract_merge | disambiguate | diary_content | report. 없는 kind 는 최소 유효 응답.
`fail_kinds` 에 든 kind 는 예외를 던진다(강등 경로 테스트). `bad_json_first` 면 첫 응답을 깨뜨려 repair 경로를 태운다.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

_KIND_MARKERS = [
    ("speaker_roles", "화자 글자"),
    ("extract_merge", "하나로 통합"),
    ("extract", "구조화된 사실(CallFacts)"),
    ("disambiguate", "후보 중 하나"),
    ("diary_content", "기타 기록사항"),
    ("report", "컨설팅 보고서 초안"),
]

_DEFAULTS: dict[str, Any] = {
    "speaker_roles": {"files": []},
    "extract": {"one_line_summary": "", "keywords": [], "crops_mentioned": [], "farm_status": [], "farmworks": [],
                "observations": [], "pests": [], "products": [], "questions": [], "advice": [], "actions": [],
                "follow_ups": [], "has_farmwork_content": False, "stt_uncertainties": []},
    "extract_merge": {"one_line_summary": "", "keywords": []},
    "disambiguate": {"picks": []},
    "diary_content": {"content": "[AI 초안·통화 기반]\n언급 없음", "evidence": []},
    "report": {"farm_status": [], "issues": [], "advice": [], "farmer_actions": [], "follow_ups": [],
               "summary_line": "", "keywords": [], "action_items": []},
}


def detect_kind(messages: list[BaseMessage]) -> str:
    sys_text = ""
    for m in messages:
        if m.type == "system":
            sys_text = m.content if isinstance(m.content, str) else str(m.content)
            break
    for kind, marker in _KIND_MARKERS:
        if marker in sys_text:
            return kind
    return "unknown"


class FakeChatModel(BaseChatModel):
    responses: dict[str, Any] = {}
    fail_kinds: set[str] = set()
    bad_json_first: bool = False
    model_name: str = "fake-llm"
    calls: list[dict[str, Any]] = []
    _seen: dict[str, int] = {}

    @property
    def _llm_type(self) -> str:
        return "fake-chat"

    def _resolve(self, messages: list[BaseMessage]) -> str:
        kind = detect_kind(messages)
        n = self._seen.get(kind, 0)
        self._seen[kind] = n + 1
        self.calls.append({"kind": kind, "n": n})
        if kind in self.fail_kinds:
            raise RuntimeError(f"fake failure for {kind}")
        r = self.responses.get(kind, _DEFAULTS.get(kind, {}))
        if callable(r):
            r = r(messages)
        text = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
        # repair 경로: 첫 응답을 코드펜스+깨진 JSON 으로
        is_repair = any(m.type == "human" and "스키마를 위반" in (m.content if isinstance(m.content, str) else "") for m in messages)
        if self.bad_json_first and n == 0 and not is_repair:
            return "```json\n" + text[:-1] + ", \"unexpected_field\": 1}\n```"
        return text

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:
        text = self._resolve(messages)
        msg = AIMessage(content=text, usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
                        response_metadata={"model_name": self.model_name})
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools: Any, **kwargs: Any):  # type: ignore[override]
        raise NotImplementedError("fake model has no tool calling")

    def with_structured_output(self, schema: Any, **kwargs: Any):  # type: ignore[override]
        raise NotImplementedError("fake model: structured output unsupported (use json_mode/prompt)")
