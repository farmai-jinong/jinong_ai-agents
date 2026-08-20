"""스토리지 구현 선택 — `build_pipeline`(PIPELINE_IMPL) 과 같은 패턴 (STORAGE_IMPL=s3|local).

키 빌더는 계속 `app.clients.s3.Keys` 가 유일 정본이며, 두 구현 모두 같은 Keys 를 사용해
레이아웃이 동일하다. `local` 은 로컬 개발 전용 — 배포 환경에서는 항상 `s3`.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..config import Settings
from .s3 import HeadInfo, Keys


class StorageClient(Protocol):
    bucket: str
    keys: Keys

    async def head(self, bucket: str, key: str) -> HeadInfo: ...
    async def get_bytes(self, bucket: str, key: str) -> bytes: ...
    async def get_json(self, key: str, bucket: str | None = None) -> Any: ...
    async def put_text(self, key: str, text: str, content_type: str = "text/markdown; charset=utf-8") -> str: ...
    async def put_json(self, key: str, obj: Any) -> str: ...
    async def head_bucket(self) -> bool: ...


def build_storage(settings: Settings) -> StorageClient:
    if settings.storage_impl == "local":
        from .local_storage import LocalStorageClient

        return LocalStorageClient(settings)
    from .s3 import S3Client

    return S3Client(settings)
