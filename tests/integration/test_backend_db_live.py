"""백엔드 PostgreSQL 라이브 읽기 전용 검증 — `BACKEND_DB_URL` 이 있을 때만(`pytest -m backend_db tests/integration`).

Mac 에서는 공인 IP(43.201.131.246), 지농서버에서는 사설 IP(172.31.1.109). 값의 SSOT 는 Hatchery_serving/.env.
읽기 전용 보장(INSERT 실패)은 계정이 superuser 라 **우리 세션 설정**만이 방어선이므로 반드시 단언한다.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.clients.backend_db import BackendDbClient, BackendDbError

pytestmark = [pytest.mark.backend_db, pytest.mark.asyncio]

URL = os.environ.get("BACKEND_DB_URL", "")
# 실측 좌표(2026-09-22): 테스트 농가 engn 1 / test7 — 등록 품종 080301·080401·090100·132600 → M 그룹 4종
TEST_FARMER = ("1", "test7")
EXPECT_CODES = {"0803MM", "0804MM", "0901MM", "1326MM"}


@pytest.fixture
async def db():
    if not URL:
        pytest.skip("BACKEND_DB_URL 미설정")
    c = BackendDbClient(URL, timeout=10)
    try:
        yield c
    finally:
        await c.close()


async def test_probe_is_read_only(db):
    p = await db.probe()
    assert p["ok"] and p["read_only"] is True, p
    assert "@" not in p["url"] and ":" in p["url"]


async def test_insert_is_rejected(db):
    with pytest.raises(BackendDbError) as ei:
        await db._rows(text("INSERT INTO voicetalk.tb_voice_talk_consultant(engn_id, user_id) VALUES (0, '__ro_probe__')"))
    assert "read-only" in str(ei.value).lower(), ei.value


async def test_farm_crops_shape_and_names(db):
    rows = await db.farm_crops(*TEST_FARMER)
    codes = {r["prdlstCode"] for r in rows}
    assert EXPECT_CODES <= codes, rows
    assert all(r["prdlstNm"] and r["use"] is True and r["reprsntPrdlstCnt"] in (0, 1) for r in rows), rows
    assert sum(r["reprsntPrdlstCnt"] for r in rows) <= 1        # 대표 작물은 최대 1
    assert rows[0]["reprsntPrdlstCnt"] == 1 if any(r["reprsntPrdlstCnt"] for r in rows) else True


async def test_prdlsts_is_large_and_cached(db):
    a = await db.prdlsts()
    assert len(a) >= 2000 and all(r["prdlstCode"].endswith("MM") for r in a)
    assert {r["prdlstCode"]: r["prdlstNm"] for r in a}["0803MM"] == "토마토"
    assert await db.prdlsts() is a


async def test_call_roles_resolvable(db):
    row = await db._rows(text("SELECT call_id FROM voicetalk.tb_voice_talk_history WHERE status='ENDED' "
                              "AND stt_status='COMPLETED' ORDER BY created_at DESC LIMIT 1"))
    if not row:
        pytest.skip("ENDED 통화 없음")
    from app.services.backend_sync import participants_from_call
    r = await db.call(row[0]["call_id"])
    assert r is not None
    parts, warns = participants_from_call(r)
    assert parts is not None and {p.role for p in parts} == {"farmer", "consultant"}, (r, warns)
    assert await db.call("no-such-call") is None
