import pytest

from app.db import repo
from tests.conftest import BUCKET, full_flow


@pytest.mark.asyncio
async def test_start_idempotent_and_token_hidden(client):
    body = {"call_id": "c1", "participants": [{"role": "farmer", "user_id": "u", "name": "홍"}],
            "farm_access_token": "SECRET"}
    r = await client.post("/v1/calls", json=body)
    assert r.status_code == 201
    assert r.json()["state"] == "OPEN" and r.json()["status"] == "NONE"
    assert "SECRET" not in r.text
    r = await client.post("/v1/calls", json=body)
    assert r.status_code == 200
    r = await client.post("/v1/calls", json={"call_id": "bad id!"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_start_participant_engn_id_roundtrip(client):
    # 농가 복합 키(engn_id + user_id) — 수신·저장·에코 확인
    body = {"call_id": "c-engn", "participants": [
        {"role": "farmer", "user_id": "u123", "engn_id": "18", "name": "홍길동"},
        {"role": "consultant", "user_id": "c9", "name": "김상담"}]}
    r = await client.post("/v1/calls", json=body)
    assert r.status_code == 201
    got = (await client.get("/v1/calls/c-engn")).json()["participants"]
    assert got[0]["engn_id"] == "18" and got[1]["engn_id"] is None


@pytest.mark.asyncio
async def test_audio_validation_and_idempotency(client):
    await client.post("/v1/calls", json={"call_id": "c2"})
    r = await client.post("/v1/calls/c2/audio", json={"bucket": BUCKET, "key": "raw/missing.wav"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "S3_OBJECT_NOT_FOUND"
    r = await client.post("/v1/calls/c2/audio", json={"bucket": BUCKET, "key": "raw/sample1.wav", "seq": 1})
    assert r.status_code == 202 and r.json()["audio"]["status"] == "PENDING"
    r = await client.post("/v1/calls/c2/audio", json={"bucket": BUCKET, "key": "raw/sample1.wav", "seq": 1})
    assert r.status_code == 200 and r.json()["note"] == "already queued"
    assert r.json()["stt_progress"]["total"] == 1
    # 미생성 통화에 오디오 → 자동 생성
    r = await client.post("/v1/calls/auto1/audio", json={"bucket": BUCKET, "key": "raw/sample2.wav"})
    assert r.status_code == 202
    assert (await client.get("/v1/calls/auto1")).status_code == 200


@pytest.mark.asyncio
async def test_end_and_get_flow(client, app, stt_mock):
    r = await full_flow(client, "c3")
    assert r.status_code == 202 and r.json()["status"] == "PROCESSING" and r.json()["state"] == "ENDED"
    r = await client.post("/v1/calls/c3/end")
    assert r.status_code == 200 and r.json()["note"] == "already ended"

    await app.state.rt.worker.drain()
    r = await client.get("/v1/calls/c3")
    body = r.json()
    assert body["status"] == "COMPLETED", body
    assert body["stt_progress"] == {"total": 1, "transcribed": 1, "failed": 0, "pending": 0}
    res = body["result"]
    assert res["diaries"][0]["prdlst_code"] == "0804MM"
    assert res["diaries"][0]["markdown"].startswith("> 📝") and "## 주요 농작업" in res["diaries"][0]["markdown"]
    assert res["diaries"][0]["s3_key_md"] == "agents/voicecall/c3/artifacts/diary/0804MM.md"
    # 기본 전달용(markdown / s3_key_md)에는 근거가 없고, 근거 포함 정본은 internal 키로만 가리킨다
    assert "## 근거 발화" not in res["diaries"][0]["markdown"] and "(0804MM)" not in res["diaries"][0]["markdown"]
    assert res["diaries"][0]["s3_key_md_internal"] == "agents/voicecall/c3/artifacts/internal/diary/0804MM.md"
    assert res["report"]["markdown"].startswith("# 컨설팅 보고서") and "## 근거 발화" not in res["report"]["markdown"]
    assert res["report"]["s3_key_md_internal"] == "agents/voicecall/c3/artifacts/internal/report.md"
    assert res["transcript_key"] == "agents/voicecall/c3/transcript/merged.json"
    assert "secret" not in r.text
    # inline=false → 본문 생략
    r = await client.get("/v1/calls/c3?inline=false")
    assert r.json()["result"]["diaries"][0]["markdown"] is None
    # 산출물 raw 엔드포인트
    r = await client.get("/v1/calls/c3/artifacts/report")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    r = await client.get("/v1/calls/c3/artifacts/diary/0804MM?format=json")
    assert r.status_code == 200 and r.json()["prdlst_code"] == "0804MM"
    # view=internal → 근거 포함 정본 (기본 view=public 은 근거 없음)
    r = await client.get("/v1/calls/c3/artifacts/diary/0804MM")
    assert r.status_code == 200 and "## 근거 발화" not in r.text
    r = await client.get("/v1/calls/c3/artifacts/diary/0804MM?view=internal")
    assert r.status_code == 200 and "## 근거 발화" in r.text and r.headers["content-type"].startswith("text/markdown")
    r = await client.get("/v1/calls/c3/artifacts/report?view=internal")
    assert r.status_code == 200 and "## 근거 발화" in r.text
    r = await client.get("/v1/calls/c3/artifacts/diary/0804MM?view=bogus")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "INVALID_VIEW"
    r = await client.get("/v1/calls/c3/artifacts/diary/0804MM?format=json&view=bogus")   # json 은 view 무시
    assert r.status_code == 200
    r = await client.get("/v1/calls/c3/transcript")
    assert r.status_code == 200 and len(r.json()["segments"]) == 4
    # 목록
    r = await client.get("/v1/calls?status=COMPLETED")
    assert r.json()["items"][0]["call_id"] == "c3"
    # S3 산출물 존재
    keys = {o["Key"] for o in app.state.rt.s3._s3.list_objects_v2(Bucket=BUCKET, Prefix="agents/voicecall/c3/")["Contents"]}
    assert "agents/voicecall/c3/artifacts/result.json" in keys and any(k.startswith("agents/voicecall/c3/stt/") for k in keys)


@pytest.mark.asyncio
async def test_end_without_audio_is_empty(client, app):
    await client.post("/v1/calls", json={"call_id": "c4"})
    r = await client.post("/v1/calls/c4/end")
    assert r.status_code == 202
    await app.state.rt.worker.drain()
    body = (await client.get("/v1/calls/c4")).json()
    assert body["status"] == "EMPTY" and body["error"]["code"] == "NO_AUDIO"


@pytest.mark.asyncio
async def test_end_unknown_404_and_regenerate_409(client):
    r = await client.post("/v1/calls/nope/end")
    assert r.status_code == 404
    await client.post("/v1/calls", json={"call_id": "c5"})
    r = await client.post("/v1/calls/c5/regenerate")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CALL_NOT_ENDED"


@pytest.mark.asyncio
async def test_regenerate_overwrites(client, app, stt_mock):
    await full_flow(client, "c6")
    await app.state.rt.worker.drain()
    run1 = (await client.get("/v1/calls/c6")).json()["generation"]["run"]
    r = await client.post("/v1/calls/c6/regenerate", json={"retranscribe": True})
    assert r.status_code == 202 and r.json()["status"] == "PROCESSING"
    await app.state.rt.worker.drain()
    body = (await client.get("/v1/calls/c6")).json()
    assert body["status"] == "COMPLETED" and body["generation"]["run"] == run1 + 1
    assert body["audio"][0]["attempts"] == 1        # 재전사됨(리셋 후 1회)
    assert len(body["result"]["diaries"]) == 1


@pytest.mark.asyncio
async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_regenerate_accepts_farm_token_in_body(client, app, stt_mock):
    """terminal 후 purge 된 농가 JWT 를 regenerate body 한 호출로 재공급 (daily 와 동일 계약).

    회귀: 기존 문서의 'regenerate → POST /v1/calls 로 토큰 업서트' 2단계는 regenerate 가
    즉시 워커를 깨워 토큰을 스냅샷하므로 경합이 있었다.
    """
    rt = app.state.rt
    await full_flow(client, "c-regen-token")
    await rt.worker.drain()
    async with rt.db.session() as s:
        call = await repo.get_call(s, "c-regen-token")
        assert call.status == "COMPLETED" and call.farm_access_token is None   # terminal purge

    r = await client.post("/v1/calls/c-regen-token/regenerate",
                          json={"farm_access_token": "eyJ.new.token", "reason": "re-supply"})
    assert r.status_code == 202
    async with rt.db.session() as s:
        call = await repo.get_call(s, "c-regen-token")
        assert call.farm_access_token == "eyJ.new.token"                        # 스케줄 전에 반영
    await rt.worker.drain()
    body = (await client.get("/v1/calls/c-regen-token")).json()
    assert body["status"] == "COMPLETED" and body["generation"]["run"] == 2
    assert "farm_access_token" not in (body.get("result") or {})                # 응답 미노출
    async with rt.db.session() as s:
        call = await repo.get_call(s, "c-regen-token")
        assert call.farm_access_token is None                                   # terminal 재-purge


# --- terminal 통화 재등록으로 식별자 보정 (백엔드 문서 §2) --------------------
@pytest.mark.asyncio
async def test_terminal_call_reregister_updates_identifiers(client, app, stt_mock):
    """COMPLETED 된 통화에 engn_id 를 채워 재-POST 하면 참가자·메타가 갱신된다(산출물은 그대로)."""
    rt = app.state.rt
    await full_flow(client, "idc1")
    await rt.worker.drain()
    before = (await client.get("/v1/calls/idc1")).json()
    assert before["status"] == "COMPLETED"
    assert before["participants"][0]["engn_id"] is None
    run_before = before["generation"]["run"]

    r = await client.post("/v1/calls", json={
        "call_id": "idc1", "started_at": before["started_at"],
        "participants": [{"role": "farmer", "user_id": "u1", "engn_id": "18", "name": "홍길동"},
                         {"role": "consultant", "user_id": "c1", "engn_id": "18", "name": "김상담"}],
        "metadata": {"hints": {"farmer_engn_id": "18", "farmer_user_id": "u1"}},
    })
    assert r.status_code == 200, r.text
    assert "updated" in (r.json().get("note") or "")

    after = (await client.get("/v1/calls/idc1")).json()
    assert after["participants"][0]["engn_id"] == "18"
    assert after["metadata"]["hints"]["farmer_engn_id"] == "18"
    assert after["status"] == "COMPLETED"                     # 상태·산출물은 그대로
    assert after["generation"]["run"] == run_before
    assert after["result"]["diaries"] == before["result"]["diaries"]


@pytest.mark.asyncio
async def test_terminal_reregister_without_changes_is_noop(client, app, stt_mock):
    rt = app.state.rt
    await full_flow(client, "idc2")
    await rt.worker.drain()
    body = (await client.get("/v1/calls/idc2")).json()
    r = await client.post("/v1/calls", json={"call_id": "idc2", "started_at": body["started_at"],
                                             "participants": body["participants"]})
    assert r.status_code == 200
    assert r.json()["note"] == "call already finalized"
