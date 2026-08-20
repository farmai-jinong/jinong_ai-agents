"""그래프 노드에 주입되는 의존성 — config["configurable"]["deps"] 로 전달 (전역 없음, 테스트에서 Fake 교체)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from ..config import Settings


class FarmosLike(Protocol):
    async def list_crops(self) -> list[dict[str, Any]]: ...
    async def fetch_refs(self, date: str, prdlst_code: str) -> Any: ...
    async def pesti_list(self, prdlst_code: str, prvnbe_code: str | None = None) -> list[dict[str, Any]]: ...


FarmosFactory = Callable[[str], FarmosLike]   # token → client


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Deps:
    settings: Settings
    llm: Any                                  # BaseChatModel (ChatOpenAI 또는 Fake)
    farmos_factory: FarmosFactory | None       # None → farmos 비활성
    clock: Callable[[], datetime] = _utcnow
    prompt_version: str = "1"
    dump_dir: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def get_deps(config: dict[str, Any]) -> Deps:
    return config["configurable"]["deps"]
