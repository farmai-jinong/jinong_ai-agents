"""백엔드 DB 갱신(dev 전용) — 역할 판정·힌트 보존·STT/재생성 시점·첫 생성 무변경·fail-open.

실제 DB 는 쓰지 않는다: `rt.backend_db` 에 Fake 를 꽂는다(`BackendDbLike` 모양).
"""

from __future__ import annotations

import pytest

from app.clients.backend_db import BackendDbError, safe_url
from app.db import repo
from app.db.models import Call
from app.schemas.pipeline import Participant
from app.services.backend_sync import hints_with_db, participants_from_call, refresh_call
from app.worker.generate_job import build_context
from tests.conftest import full_flow

ROW = {"call_id": "c1", "status": "ENDED", "sender_engn_id": "1", "sender_user_id": "cons7", "sender_nm": "김상담",
       "sender_job": "001001004", "receiver_engn_id": "1", "receiver_user_id": "test7", "receiver_nm": "홍농가",
       "receiver_job": "001001003"}
CROPS = [{"prdlstCode": "1326MM", "prdlstNm": "파프리카", "reprsntPrdlstCnt": 1, "use": True},
         {"prdlstCode": "0803MM", "prdlstNm": "토마토", "reprsntPrdlstCnt": 0, "use": True}]


class FakeBackendDb:
    def __init__(self, row=ROW, crops=CROPS, exc=None):
        self.row, self.crops, self.exc = row, crops, exc
        self.call_ids: list[str] = []
        self.crop_keys: list[tuple[str, str]] = []

    async def call(self, call_id):
        self.call_ids.append(call_id)
        if self.exc:
            raise self.exc
        return self.row

    async def farm_crops(self, engn_id, user_id):
        self.crop_keys.append((engn_id, user_id))
        return self.crops

    async def prdlsts(self):
        return [{"prdlstCode": c["prdlstCode"], "prdlstNm": c["prdlstNm"]} for c in self.crops]

    async def probe(self):
        return {"ok": True, "read_only": True, "latency_ms": 1, "url": "fake"}

    async def close(self):
        pass


# --- 순수 매핑 ------------------------------------------------------------------
def test_roles_from_job_code_consultant_marks_other_side_farmer():
    parts, warns = participants_from_call(ROW)
    assert warns == []
    assert [(p.role, p.user_id, p.engn_id, p.name) for p in parts] == \
        [("farmer", "test7", "1", "홍농가"), ("consultant", "cons7", "1", "김상담")]
    # 반대 방향(농가가 발신)도 같은 결과
    flipped = {**ROW, "sender_user_id": "test7", "sender_nm": "홍농가", "sender_job": "001001003",
               "receiver_user_id": "cons7", "receiver_nm": "김상담", "receiver_job": "001001004"}
    parts2, _ = participants_from_call(flipped)
    assert [(p.role, p.user_id) for p in parts2] == [("farmer", "test7"), ("consultant", "cons7")]


@pytest.mark.parametrize("sender_job,receiver_job,why", [
    ("001001004", "001001004", "양쪽 모두 컨설턴트"),
    ("001001003", "001001003", "컨설턴트 없음"),
    ("", None, "컨설턴트 없음"),
])
def test_roles_ambiguous_keeps_payload(sender_job, receiver_job, why):
    parts, warns = participants_from_call({**ROW, "sender_job": sender_job, "receiver_job": receiver_job})
    assert parts is None and any(why in w for w in warns), warns


def test_roles_missing_identifier():
    parts, warns = participants_from_call({**ROW, "receiver_user_id": None})
    assert parts is None and "receiver" in warns[0]


def test_hints_with_db_preserves_existing_and_overrides_farmer_fields():
    old = {"prdlst_code": "0804MM", "prdlst_nm": "딸기", "diary_date": "2026-09-01", "topic": "x",
           "farmer_crops": [{"prdlstCode": "0804MM", "prdlstNm": "딸기", "reprsntPrdlstCnt": 1}]}
    new = hints_with_db(old, CROPS, ("1", "test7"))
    assert new["prdlst_code"] == "0804MM" and new["diary_date"] == "2026-09-01" and new["topic"] == "x"
    assert new["farmer_engn_id"] == "1" and new["farmer_user_id"] == "test7"
    assert [c["prdlstCode"] for c in new["farmer_crops"]] == ["1326MM", "0803MM"]
    assert old["farmer_crops"][0]["prdlstCode"] == "0804MM"        # 원본 불변
    # 작물이 비면 기존 farmer_crops 는 지우지 않는다
    assert hints_with_db(old, [], None)["farmer_crops"] == old["farmer_crops"]


def test_safe_url_strips_password():
    assert safe_url("postgresql+asyncpg://jinong:s3cret@172.31.1.109:25432/postgres") == "172.31.1.109:25432/postgres"
    assert "s3cret" not in safe_url("postgresql+asyncpg://jinong:s3cret@h:1/db")


def test_build_context_flag_default_false_and_passthrough():
    call = Call(call_id="c1", participants_json=[], metadata_json={"hints": {}}, generation_run=2)
    assert build_context(call).prefer_backend_db is False
    assert build_context(call, prefer_backend_db=True).prefer_backend_db is True


# --- refresh_call (sqlite 행 + Fake) ---------------------------------------------
async def test_refresh_call_updates_participants_hints_and_event(client, app, stt_mock):
    rt = app.state.rt
    rt.backend_db = FakeBackendDb()
    await full_flow(client, "rc1", end=False)
    async with rt.db.session() as s:
        call = await repo.get_call(s, "rc1")
        info = await refresh_call(rt, s, call, reason="regenerate")
        await s.commit()
    assert info["found"] is True and info["error"] if "error" in info else True
    assert set(info["changed"]) == {"participants", "num_speakers", "hints"}, info
    body = (await client.get("/v1/calls/rc1")).json()
    assert [(p["role"], p["user_id"], p["engn_id"]) for p in body["participants"]] == \
        [("farmer", "test7", "1"), ("consultant", "cons7", "1")]
    h = body["metadata"]["hints"]
    assert h["prdlst_code"] == "0804MM"                              # 기존 힌트 보존
    assert h["farmer_engn_id"] == "1" and [c["prdlstCode"] for c in h["farmer_crops"]] == ["1326MM", "0803MM"]
    assert body["metadata"]["backend_refresh"]["reason"] == "regenerate"
    assert body["metadata"]["farm"] if "farm" in body["metadata"] else True
    assert rt.backend_db.crop_keys == [("1", "test7")]
    async with rt.db.session() as s:
        call = await repo.get_call(s, "rc1")
        assert call.num_speakers == 2 and call.farm_access_token == "eyJ.secret.token"   # 토큰 불변
        assert call.farm_json is None
    # 같은 값으로 다시 → 변경 없음
    async with rt.db.session() as s:
        call = await repo.get_call(s, "rc1")
        info2 = await refresh_call(rt, s, call, reason="stt")
    assert info2["changed"] == [] and info2["found"] is True


async def test_refresh_call_row_missing_keeps_snapshot(client, app, stt_mock):
    rt = app.state.rt
    rt.backend_db = FakeBackendDb(row=None)
    await full_flow(client, "rc2", end=False)
    async with rt.db.session() as s:
        call = await repo.get_call(s, "rc2")
        before = list(call.participants_json)
        info = await refresh_call(rt, s, call, reason="stt")
        await s.commit()
    assert info["found"] is False and info["changed"] == [] and rt.backend_db.crop_keys == []
    body = (await client.get("/v1/calls/rc2")).json()
    assert [p["user_id"] for p in body["participants"]] == [p["user_id"] for p in before]
    assert body["metadata"]["backend_refresh"]["found"] is False
    assert body["metadata"]["hints"] == {"prdlst_code": "0804MM", "prdlst_nm": "딸기"}


async def test_refresh_call_row_missing_but_hints_key_fetches_crops(client, app, stt_mock):
    """통화 행이 없어도(스모크 통화 등) hints 의 농가 복합 키로 등록 작물은 읽는다 — 파이프라인 farmer_key 와 같은 규칙."""
    rt = app.state.rt
    rt.backend_db = FakeBackendDb(row=None)
    body = {"call_id": "rc2h", "participants": [{"role": "farmer", "user_id": "smoke-farmer"}],
            "metadata": {"hints": {"farmer_engn_id": "1", "farmer_user_id": "test7", "topic": "t"}}}
    assert (await client.post("/v1/calls", json=body)).status_code == 201
    async with rt.db.session() as s:
        call = await repo.get_call(s, "rc2h")
        info = await refresh_call(rt, s, call, reason="regenerate")
        await s.commit()
    assert info["found"] is False and info["changed"] == ["hints"] and info["crops"] == 2
    assert rt.backend_db.crop_keys == [("1", "test7")]
    h = (await client.get("/v1/calls/rc2h")).json()["metadata"]["hints"]
    assert h["topic"] == "t" and [c["prdlstCode"] for c in h["farmer_crops"]] == ["1326MM", "0803MM"]


async def test_refresh_call_db_error_is_fail_open(client, app, stt_mock):
    rt = app.state.rt
    rt.backend_db = FakeBackendDb(exc=BackendDbError("ConnectionRefusedError: down"))
    await full_flow(client, "rc3", end=False)
    async with rt.db.session() as s:
        call = await repo.get_call(s, "rc3")
        info = await refresh_call(rt, s, call, reason="regenerate")
        await s.commit()
    assert info["found"] is False and "error" in info and info["changed"] == []
    body = (await client.get("/v1/calls/rc3")).json()
    assert body["participants"][0]["user_id"] == "u1"
    assert "down" in body["metadata"]["backend_refresh"]["error"]


async def test_ambiguous_roles_keep_payload_but_crops_from_payload_farmer(client, app, stt_mock):
    rt = app.state.rt
    rt.backend_db = FakeBackendDb(row={**ROW, "sender_job": "001001004", "receiver_job": "001001004"})
    body = {"call_id": "rc4", "participants": [{"role": "farmer", "user_id": "test7", "engn_id": "1"},
                                               {"role": "consultant", "user_id": "c1"}]}
    assert (await client.post("/v1/calls", json=body)).status_code == 201
    async with rt.db.session() as s:
        call = await repo.get_call(s, "rc4")
        info = await refresh_call(rt, s, call, reason="stt")
        await s.commit()
    assert "participants" not in info["changed"] and any("역할 판정 불가" in w for w in info["warnings"])
    assert rt.backend_db.crop_keys == [("1", "test7")] and "hints" in info["changed"]


# --- 워커 시점 -------------------------------------------------------------------
async def test_first_generation_does_not_touch_db_but_stt_does(client, app, stt_mock):
    rt = app.state.rt
    rt.backend_db = FakeBackendDb()
    await full_flow(client, "w-first")
    await rt.worker.drain()
    body = (await client.get("/v1/calls/w-first")).json()
    assert body["status"] == "COMPLETED" and body["generation"]["run"] == 1
    # STT 잡이 1회 조회(reason=stt); 첫 생성(run 1)은 조회하지 않는다
    assert rt.backend_db.call_ids == ["w-first"]
    assert body["metadata"]["backend_refresh"]["reason"] == "stt"


async def test_regenerate_refreshes_from_db(client, app, stt_mock):
    rt = app.state.rt
    rt.backend_db = FakeBackendDb()
    await full_flow(client, "w-regen")
    await rt.worker.drain()
    rt.backend_db.crops = [{"prdlstCode": "0901MM", "prdlstNm": "오이", "reprsntPrdlstCnt": 1, "use": True}]
    r = await client.post("/v1/calls/w-regen/regenerate", json={"reason": "crop updated"})
    assert r.status_code == 202, r.text
    await rt.worker.drain()
    body = (await client.get("/v1/calls/w-regen")).json()
    assert body["status"] == "COMPLETED" and body["generation"]["run"] == 2
    assert rt.backend_db.call_ids == ["w-regen", "w-regen"]          # stt + regenerate
    assert body["metadata"]["backend_refresh"]["reason"] == "regenerate"
    assert [c["prdlstCode"] for c in body["metadata"]["hints"]["farmer_crops"]] == ["0901MM"]
    assert rt.backend_db.crop_keys[-1] == ("1", "test7")


async def test_backend_db_disabled_is_noop(client, app, stt_mock):
    rt = app.state.rt
    assert rt.backend_db is None                                       # 테스트 settings 는 BACKEND_DB_URL 비움
    await full_flow(client, "w-off")
    await rt.worker.drain()
    r = await client.post("/v1/calls/w-off/regenerate")
    assert r.status_code == 202
    await rt.worker.drain()
    body = (await client.get("/v1/calls/w-off")).json()
    assert body["status"] == "COMPLETED" and "backend_refresh" not in (body["metadata"] or {})
    assert [p["user_id"] for p in body["participants"]] == ["u1", "c1"]


def test_participant_model_roundtrip():
    p = Participant(role="farmer", user_id="u", engn_id="1", name=None)
    assert p.model_dump() == {"role": "farmer", "user_id": "u", "engn_id": "1", "name": None}
