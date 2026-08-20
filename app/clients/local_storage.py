"""로컬 파일시스템 스토리지 — **로컬 개발 전용** (STORAGE_IMPL=local).

S3 없이 raw audio 파일로 E2E 를 돌리기 위한 `S3Client` 대체 구현. boto3 를 쓰지 않는다.
- 입력 오디오: bucket != S3_BUCKET (예: 센티널 "local") 이면 `LOCAL_AUDIO_DIR/<key>` 를 읽는다.
- 산출물: 자체 버킷(bucket == S3_BUCKET) 은 `LOCAL_STORAGE_DIR` 아래, `Keys`(유일 정본) 그대로
  → 디렉터리 레이아웃이 S3 와 동일하게 미러된다 (`agents/voicecall/<call_id>/...`).
- 에러 코드는 `S3Error` 를 그대로 재사용해 422 매핑(services/calls)과 재시도 분류(worker/stt_job)를 보존.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path
from typing import Any

from ..config import Settings
from .s3 import HeadInfo, Keys, S3Error


class LocalStorageClient:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self.keys = Keys(settings.s3_prefix)
        self._storage_dir = Path(settings.local_storage_dir).resolve()
        self._audio_dir = Path(settings.local_audio_dir).resolve()

    def _resolve(self, bucket: str, key: str) -> Path:
        base = self._storage_dir if bucket == self.bucket else self._audio_dir
        p = (base / key).resolve()
        if not p.is_relative_to(base):
            raise S3Error("S3_ACCESS_DENIED", f"{bucket}/{key} escapes {base}")
        return p

    # --- 읽기 -----------------------------------------------------------
    async def head(self, bucket: str, key: str) -> HeadInfo:
        def _do() -> HeadInfo:
            p = self._resolve(bucket, key)
            try:
                st = p.stat()
            except FileNotFoundError as e:
                raise S3Error("S3_OBJECT_NOT_FOUND", f"{bucket}/{key} not found ({p})") from e
            except PermissionError as e:
                raise S3Error("S3_ACCESS_DENIED", f"{bucket}/{key} access denied") from e
            if not p.is_file():
                raise S3Error("S3_OBJECT_NOT_FOUND", f"{bucket}/{key} is not a file ({p})")
            return HeadInfo(size=st.st_size, etag=f'"{st.st_size}-{int(st.st_mtime)}"',
                            content_type=mimetypes.guess_type(key)[0])
        return await asyncio.to_thread(_do)

    async def get_bytes(self, bucket: str, key: str) -> bytes:
        def _do() -> bytes:
            p = self._resolve(bucket, key)
            try:
                return p.read_bytes()
            except OSError as e:
                raise S3Error("S3_GET_FAILED", f"{bucket}/{key}: {e}") from e
        return await asyncio.to_thread(_do)

    async def get_json(self, key: str, bucket: str | None = None) -> Any:
        data = await self.get_bytes(bucket or self.bucket, key)
        return json.loads(data.decode("utf-8"))

    # --- 쓰기 (자체 버킷/prefix 로만 — S3Client 와 동일 의미론) -------------
    async def put_text(self, key: str, text: str, content_type: str = "text/markdown; charset=utf-8") -> str:
        def _do() -> None:
            p = self._resolve(self.bucket, key)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode("utf-8"))
        await asyncio.to_thread(_do)
        return key

    async def put_json(self, key: str, obj: Any) -> str:
        return await self.put_text(key, json.dumps(obj, ensure_ascii=False, indent=2, default=str),
                                   content_type="application/json; charset=utf-8")

    async def head_bucket(self) -> bool:
        def _do() -> bool:
            self._storage_dir.mkdir(parents=True, exist_ok=True)
            self._audio_dir.mkdir(parents=True, exist_ok=True)
            return True
        return await asyncio.to_thread(_do)
