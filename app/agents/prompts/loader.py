"""프롬프트 로더 — `*.system.md`(정적) / `*.user.md.j2`(Jinja2). PROMPT_VERSION 은 산출물 푸터에 찍힌다."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

PROMPT_VERSION = "1"
_DIR = Path(__file__).parent

_PREAMBLE = (
    "[공통 규칙]\n"
    "- 반드시 한국어로 답한다.\n"
    "- 녹취문에 없는 내용을 절대 추가·추정하지 않는다. 확실하지 않으면 null 또는 빈 배열로 둔다.\n"
    "- 모든 항목에는 근거가 되는 발화 번호(`#n`)를 정수 배열 `evidence` 로 반드시 넣는다. 근거 없는 항목은 만들지 않는다.\n"
    "- 녹취문은 음성인식(STT) 결과라 농업 용어·상표명이 잘못 들렸을 수 있다. 이름 필드에는 원문 표기를 그대로 두고, "
    "의심되면 stt_uncertainties 나 rationale 에 적는다.\n"
    "- 출력은 JSON 하나만. 설명·코드펜스 금지.\n"
)


@lru_cache
def _env() -> Environment:
    return Environment(loader=FileSystemLoader(str(_DIR)), undefined=StrictUndefined,
                       autoescape=select_autoescape(default=False), trim_blocks=True, lstrip_blocks=True,
                       keep_trailing_newline=True)


@lru_cache
def load_system(name: str) -> str:
    text = (_DIR / f"{name}.system.md").read_text(encoding="utf-8")
    return _PREAMBLE + "\n" + text


def render_user(name: str, **ctx: Any) -> str:
    return _env().get_template(f"{name}.user.md.j2").render(**ctx)
