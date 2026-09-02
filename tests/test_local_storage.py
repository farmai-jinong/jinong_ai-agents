"""LocalStorageClient 단위 테스트 — moto 불필요 (순수 파일시스템)."""

from __future__ import annotations

import pytest

from app.clients.local_storage import LocalStorageClient
from app.clients.s3 import S3Error
from app.config import Settings
from tests.conftest import BUCKET


@pytest.fixture
def storage(tmp_path) -> LocalStorageClient:
    settings = Settings(
        storage_impl="local", s3_bucket=BUCKET, s3_prefix="agents/voicecall",
        local_storage_dir=str(tmp_path / "storage"), local_audio_dir=str(tmp_path / "audio"),
    )
    return LocalStorageClient(settings)


@pytest.mark.asyncio
async def test_head_and_get_bytes_from_audio_dir(storage, tmp_path):
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "sample.wav").write_bytes(b"RIFF" + b"\x00" * 100)

    info = await storage.head("local", "sample.wav")
    assert info.size == 104
    assert info.etag
    assert info.content_type in ("audio/x-wav", "audio/wav")

    assert await storage.get_bytes("local", "sample.wav") == b"RIFF" + b"\x00" * 100


@pytest.mark.asyncio
async def test_missing_file_error_codes(storage):
    with pytest.raises(S3Error) as e:
        await storage.head("local", "nope.wav")
    assert e.value.code == "S3_OBJECT_NOT_FOUND"

    with pytest.raises(S3Error) as e:
        await storage.get_bytes("local", "nope.wav")
    assert e.value.code == "S3_GET_FAILED"


@pytest.mark.asyncio
async def test_path_traversal_rejected(storage, tmp_path):
    (tmp_path / "secret.txt").write_text("shh")
    with pytest.raises(S3Error) as e:
        await storage.head("local", "../secret.txt")
    assert e.value.code == "S3_ACCESS_DENIED"


@pytest.mark.asyncio
async def test_own_bucket_roundtrip(storage, tmp_path):
    key = storage.keys.result_json("c1")
    assert await storage.put_json(key, {"ok": True, "n": 1}) == key
    assert (tmp_path / "storage" / "agents/voicecall/c1/artifacts/result.json").is_file()
    kint = storage.keys.diary_md_internal("c1", "0804MM")
    await storage.put_text(kint, "# internal")
    assert (tmp_path / "storage" / "agents/voicecall/c1/artifacts/internal/diary/0804MM.md").is_file()

    assert await storage.get_json(key) == {"ok": True, "n": 1}
    assert b'"ok": true' in await storage.get_bytes(BUCKET, key)


@pytest.mark.asyncio
async def test_head_bucket_creates_dirs(storage, tmp_path):
    assert await storage.head_bucket() is True
    assert (tmp_path / "storage").is_dir() and (tmp_path / "audio").is_dir()
