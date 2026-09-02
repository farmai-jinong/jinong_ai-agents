"""S3 클라이언트 + **키 빌더(유일 정본)**.

- 입력 오디오는 호출자 소유 (bucket/key 그대로 읽기만). 우리 prefix 로 복사하지 않는다(계획 D3).
- 산출물은 `{S3_PREFIX}/{call_id}/...` 아래에만 쓴다. 다른 prefix(raw/, stt-raw/ …)는 audio_labeler 소유.
- boto3(동기)를 `asyncio.to_thread` 로 감싼다 (moto 호환, 의존성 최소).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from ..config import Settings


class S3Error(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class HeadInfo:
    size: int
    etag: str | None
    content_type: str | None


class Keys:
    """S3 키 규칙 — 계획 §S3 레이아웃."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix.strip("/")

    def base(self, call_id: str) -> str:
        return f"{self.prefix}/{call_id}"

    def call_json(self, call_id: str) -> str:
        return f"{self.base(call_id)}/call.json"

    def stt_raw(self, call_id: str, n: int, bucket: str, key: str) -> str:
        h = hashlib.sha1(f"{bucket}/{key}".encode()).hexdigest()[:8]
        return f"{self.base(call_id)}/stt/{n:02d}-{h}.json"

    def transcript_json(self, call_id: str) -> str:
        return f"{self.base(call_id)}/transcript/merged.json"

    def transcript_md(self, call_id: str) -> str:
        return f"{self.base(call_id)}/transcript/merged.md"

    def diary_md(self, call_id: str, prdlst_code: str) -> str:
        return f"{self.base(call_id)}/artifacts/diary/{prdlst_code}.md"

    def diary_json(self, call_id: str, prdlst_code: str) -> str:
        return f"{self.base(call_id)}/artifacts/diary/{prdlst_code}.json"

    # internal/ — 근거 포함 정본(내부 저장용). artifacts/diary/*.md·report.md 는 근거·코드·내부 메타를 뺀 전달용.
    # prefix 만 환경별(prod agents/voicecall, dev agents/voicecall-dev)이고 그 아래 레이아웃은 동일하다.
    def diary_md_internal(self, call_id: str, prdlst_code: str) -> str:
        return f"{self.base(call_id)}/artifacts/internal/diary/{prdlst_code}.md"

    def report_md(self, call_id: str) -> str:
        return f"{self.base(call_id)}/artifacts/report.md"

    def report_json(self, call_id: str) -> str:
        return f"{self.base(call_id)}/artifacts/report.json"

    def report_md_internal(self, call_id: str) -> str:
        return f"{self.base(call_id)}/artifacts/internal/report.md"

    def summary_md(self, call_id: str) -> str:
        return f"{self.base(call_id)}/artifacts/summary.md"

    def summary_json(self, call_id: str) -> str:
        return f"{self.base(call_id)}/artifacts/summary.json"

    def result_json(self, call_id: str) -> str:
        return f"{self.base(call_id)}/artifacts/result.json"

    # --- 날짜별(멀티콜) 영농일지 — `daily/` 하위로 분리해 call_id 네임스페이스와 충돌 차단 ----
    def daily_base(self, diary_id: str) -> str:
        return f"{self.prefix}/daily/{diary_id}"

    def daily_meta_json(self, diary_id: str) -> str:
        return f"{self.daily_base(diary_id)}/daily.json"

    def daily_transcript_json(self, diary_id: str) -> str:
        return f"{self.daily_base(diary_id)}/transcript/merged.json"

    def daily_transcript_md(self, diary_id: str) -> str:
        return f"{self.daily_base(diary_id)}/transcript/merged.md"

    def daily_diary_md(self, diary_id: str, prdlst_code: str) -> str:
        return f"{self.daily_base(diary_id)}/artifacts/diary/{prdlst_code}.md"

    def daily_diary_json(self, diary_id: str, prdlst_code: str) -> str:
        return f"{self.daily_base(diary_id)}/artifacts/diary/{prdlst_code}.json"

    def daily_diary_md_internal(self, diary_id: str, prdlst_code: str) -> str:
        return f"{self.daily_base(diary_id)}/artifacts/internal/diary/{prdlst_code}.md"

    def daily_result_json(self, diary_id: str) -> str:
        return f"{self.daily_base(diary_id)}/artifacts/result.json"


class S3Client:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self.keys = Keys(settings.s3_prefix)
        cfg: dict[str, Any] = dict(retries={"max_attempts": 3, "mode": "standard"},
                                   connect_timeout=10, read_timeout=120)
        if settings.s3_endpoint_url:
            # MinIO 등 커스텀 엔드포인트: 가상호스트 스타일 DNS 불필요(path-style 고정),
            # boto3>=1.36 기본 CRC 체크섬 전송을 필요 시에만(구버전 서버 호환).
            cfg["s3"] = {"addressing_style": "path"}
            cfg["request_checksum_calculation"] = "when_required"
            cfg["response_checksum_validation"] = "when_required"
        self._s3 = boto3.client(
            "s3",
            region_name=settings.aws_region,
            endpoint_url=settings.s3_endpoint_url or None,
            config=Config(**cfg),
        )

    # --- 읽기 -----------------------------------------------------------
    async def head(self, bucket: str, key: str) -> HeadInfo:
        def _do() -> HeadInfo:
            try:
                r = self._s3.head_object(Bucket=bucket, Key=key)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if code in ("404", "NoSuchKey", "NotFound") or status == 404:
                    raise S3Error("S3_OBJECT_NOT_FOUND", f"s3://{bucket}/{key} not found") from e
                if code in ("403", "AccessDenied") or status == 403:
                    raise S3Error("S3_ACCESS_DENIED", f"s3://{bucket}/{key} access denied") from e
                raise S3Error("S3_ERROR", str(e)) from e
            return HeadInfo(size=int(r.get("ContentLength") or 0), etag=r.get("ETag"),
                            content_type=r.get("ContentType"))
        return await asyncio.to_thread(_do)

    async def get_bytes(self, bucket: str, key: str) -> bytes:
        def _do() -> bytes:
            try:
                r = self._s3.get_object(Bucket=bucket, Key=key)
                return r["Body"].read()
            except ClientError as e:
                raise S3Error("S3_GET_FAILED", f"s3://{bucket}/{key}: {e}") from e
        return await asyncio.to_thread(_do)

    async def get_json(self, key: str, bucket: str | None = None) -> Any:
        data = await self.get_bytes(bucket or self.bucket, key)
        return json.loads(data.decode("utf-8"))

    # --- 쓰기 (우리 버킷/prefix 로만) --------------------------------------
    async def put_text(self, key: str, text: str, content_type: str = "text/markdown; charset=utf-8") -> str:
        body = text.encode("utf-8")

        def _do() -> None:
            self._s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)
        await asyncio.to_thread(_do)
        return key

    async def put_json(self, key: str, obj: Any) -> str:
        return await self.put_text(key, json.dumps(obj, ensure_ascii=False, indent=2, default=str),
                                   content_type="application/json; charset=utf-8")

    async def head_bucket(self) -> bool:
        def _do() -> bool:
            try:
                self._s3.head_bucket(Bucket=self.bucket)
                return True
            except Exception:  # noqa: BLE001
                return False
        return await asyncio.to_thread(_do)
