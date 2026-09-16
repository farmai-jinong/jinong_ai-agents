"""실녹음 1건 E2E — start → audio(S3 참조) → end → COMPLETED 폴링 → 산출물·두 벌(view)·S3 prefix·토큰 미노출.

SMOKE_E2E=1 일 때만(STT ~3.5분 + LLM 비용). dev 배포는 기본 on, prod 는 옵션(scripts/verify_deploy.sh).
artifacts/* 조회는 S3 객체를 읽어 반환하므로 200 자체가 산출물 존재 증명이다(자격증명 불필요).
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

POLL_SEC = 5
MAX_WAIT_SEC = 15 * 60
EVIDENCE_MARKERS = ("## 근거 발화", "(근거:")
SECRET_TOKEN_MARK = "smoke-secret-token"


@pytest.fixture(scope="module")
def e2e(smoke_env):
    if not smoke_env["e2e"]:
        pytest.skip("SMOKE_E2E=1 아님")
    if not (smoke_env["audio_bucket"] and smoke_env["audio_key"]):
        pytest.fail("SMOKE_E2E=1 인데 SMOKE_AUDIO_BUCKET/SMOKE_AUDIO_KEY 가 없음 — deploy/smoke.env 참조")
    return smoke_env


@pytest.fixture(scope="module")
def completed_call(client, e2e) -> dict:
    """한 번만 돌리고 모듈 내 테스트가 결과를 공유한다. SMOKE_REUSE_CALL_ID 가 있으면 새 통화 없이 그 call 로 판정만(진단용)."""
    reuse = os.environ.get("SMOKE_REUSE_CALL_ID", "")
    if reuse:
        detail = client.get(f"/v1/calls/{reuse}", params={"inline": "false"}).json()
        assert detail.get("status") == "COMPLETED", detail
        return {"call_id": reuse, "detail": detail, "token": SECRET_TOKEN_MARK}
    now = datetime.now(UTC)
    call_id = f"smoke-{e2e['env']}-{now.strftime('%Y%m%d%H%M%S')}"
    token = e2e["farm_token"] or f"{SECRET_TOKEN_MARK}.{now.timestamp()}"
    body = {
        "call_id": call_id, "started_at": (now - timedelta(minutes=10)).isoformat(),
        "participants": [{"role": "farmer", "user_id": "smoke-farmer", "name": "스모크농가"},
                         {"role": "consultant", "user_id": "smoke-cons", "name": "스모크컨설턴트"}],
        "farm_access_token": token, "metadata": {"hints": {}},
    }
    r = client.post("/v1/calls", json=body)
    assert r.status_code == 201, r.text
    r = client.post(f"/v1/calls/{call_id}/audio", json={"bucket": e2e["audio_bucket"], "key": e2e["audio_key"], "seq": 1})
    assert r.status_code == 202, r.text
    assert r.json()["stt_progress"]["total"] == 1, r.text
    r = client.post(f"/v1/calls/{call_id}/end", json={"ended_at": now.isoformat()})
    assert r.status_code == 202, r.text

    deadline = time.monotonic() + MAX_WAIT_SEC
    detail = None
    while time.monotonic() < deadline:
        detail = client.get(f"/v1/calls/{call_id}", params={"inline": "false"}).json()
        if detail["status"] in ("COMPLETED", "EMPTY", "FAILED"):
            break
        time.sleep(POLL_SEC)
    assert detail is not None and detail["status"] != "PROCESSING", f"{MAX_WAIT_SEC}s 내 종료 안 됨: {detail}"
    assert detail["status"] == "COMPLETED", (
        f"status={detail['status']} error={detail.get('error')} "
        f"audio_errors={[a.get('last_error') for a in detail.get('audio', [])]} "
        f"gen={detail.get('generation')}")
    return {"call_id": call_id, "detail": detail, "token": token}


def test_result_shape_and_s3_isolation(client, completed_call, e2e):
    d = completed_call["detail"]
    res = d["result"]
    assert res["diaries"], d
    assert res["report"]["s3_key_md"] and res["transcript_key"] and res["result_key"], res
    prefix = (e2e["profile"]["config"].get("s3_prefix") or "").rstrip("/")
    if prefix:
        want = f"{prefix}/{completed_call['call_id']}/"
        keys = [res["transcript_key"], res["result_key"], res["report"]["s3_key_md"], res["report"]["s3_key_md_internal"]]
        keys += [k for x in res["diaries"] for k in (x["s3_key_md"], x["s3_key_json"], x["s3_key_md_internal"])]
        bad = [k for k in keys if not k.startswith(want)]
        assert not bad, f"S3 prefix 격리 위반(기대 {want}): {bad}"
    for x in res["diaries"]:
        assert x["status"] in ("OK", "PARTIAL", "EMPTY", "UNRESOLVED_CROP"), x
    print(f"[smoke] call={completed_call['call_id']} diaries={[(x['prdlst_code'], x['status']) for x in res['diaries']]} "
          f"callback_status={d.get('callback_status')} gen={d['generation'].get('model')}")


def test_farm_token_never_echoed(client, completed_call):
    call_id = completed_call["call_id"]
    for path in (f"/v1/calls/{call_id}", f"/v1/calls/{call_id}?inline=false", f"/v1/calls/{call_id}/transcript"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        assert completed_call["token"] not in r.text, path
        assert SECRET_TOKEN_MARK not in r.text, path


def test_markdown_variants(client, completed_call, e2e):
    call_id = completed_call["call_id"]
    code = completed_call["detail"]["result"]["diaries"][0]["prdlst_code"]
    for base in (f"/v1/calls/{call_id}/artifacts/diary/{code}", f"/v1/calls/{call_id}/artifacts/report"):
        internal = client.get(base, params={"view": "internal"})
        public = client.get(base, params={"view": "public"})
        default = client.get(base)
        for r in (internal, public, default):
            assert r.status_code == 200, (base, r.status_code, r.text[:200])
        assert any(m in internal.text for m in EVIDENCE_MARKERS), f"{base} internal 에 근거 없음"
        assert not any(m in public.text for m in EVIDENCE_MARKERS), f"{base} public 에 근거 노출"
        if "/diary/" in base:       # 영농일지만 H1 없음(2026-09-10 형식 결정, 앱 노출) — 보고서는 H1 유지
            assert not public.text.lstrip().startswith("# "), f"{base} public 첫 줄이 H1"
        want = e2e["profile"]["config"].get("api_markdown_view")
        if want:
            assert default.text == (internal.text if want == "internal" else public.text), \
                f"{base} 기본 view 가 프로필({want}) 과 다름"
        assert client.get(base, params={"format": "json"}).status_code == 200
    # inline markdown 도 기본 view 를 따른다
    inline = client.get(f"/v1/calls/{call_id}").json()["result"]["diaries"][0]["markdown"]
    want = e2e["profile"]["config"].get("api_markdown_view")
    if want:
        has_evidence = any(m in inline for m in EVIDENCE_MARKERS)
        assert has_evidence == (want == "internal"), f"inline markdown 근거 유무가 프로필({want}) 과 다름"


def test_summary_and_transcript_available(client, completed_call):
    call_id = completed_call["call_id"]
    r = client.get(f"/v1/calls/{call_id}/transcript")
    assert r.status_code == 200 and r.json()["segments"], r.text[:200]
    # 전사에 판정 작물 동봉 — result.diaries 와 같은 (코드, 이름, 상태)
    want = [(d["prdlst_code"], d["prdlst_nm"], d["status"]) for d in completed_call["detail"]["result"]["diaries"]]
    got = [(c["prdlst_code"], c["prdlst_nm"], c["status"]) for c in r.json().get("crops", [])]
    assert got == want, f"transcript.crops {got} != result.diaries {want}"
    r = client.get(f"/v1/calls/{call_id}/artifacts/summary")
    assert r.status_code in (200, 404), r.text[:200]      # 실질 내용 없는 일지면 요약 생략(NOT_READY) 허용


def _poll_daily(client, diary_id: str) -> dict:
    deadline = time.monotonic() + 10 * 60
    detail = None
    while time.monotonic() < deadline:
        detail = client.get(f"/v1/daily-diaries/{diary_id}", params={"inline": "false"}).json()
        if detail["status"] in ("COMPLETED", "EMPTY", "FAILED"):
            return detail
        time.sleep(POLL_SEC)
    raise AssertionError(f"daily {diary_id} 10분 내 종료 안 됨: {detail}")


def test_fixed_crop_daily(client, completed_call, e2e):
    """작물 고정 모드: 스모크 통화(딸기·파프리카 2작물)를 딸기로 고정 → 일지 1건, crop 에코, 전사 crops 1건, 다른 작물 재-POST 422.

    LLM 1회 추가(약 1분). 상단 fixture 의 COMPLETED 통화를 멤버로 쓴다.
    """
    call_id = completed_call["call_id"]
    diaries = completed_call["detail"]["result"]["diaries"]
    fixed_nm = next((d["prdlst_nm"] for d in diaries if d["prdlst_nm"] == "딸기"), diaries[0]["prdlst_nm"])
    other_nm = next((d["prdlst_nm"] for d in diaries if d["prdlst_nm"] != fixed_nm), None)
    started = completed_call["detail"].get("started_at") or datetime.now(UTC).isoformat()
    diary_date = started[:10]
    diary_id = f"{call_id}-fixed"
    body = {"diary_id": diary_id, "diary_date": diary_date, "call_ids": [call_id],
            "crop": {"prdlst_nm": fixed_nm}, "metadata": {"hints": {}}}
    r = client.post("/v1/daily-diaries", json=body)
    assert r.status_code in (200, 201), r.text
    assert r.json()["crop"] == {"prdlst_code": None, "prdlst_nm": fixed_nm}, r.json()["crop"]
    t0 = time.monotonic()
    d = _poll_daily(client, diary_id)
    assert d["status"] == "COMPLETED", f"status={d['status']} error={d.get('error')} gen={d.get('generation')}"
    assert d["crop"]["prdlst_nm"] == fixed_nm
    res = d["result"]
    assert len(res["diaries"]) == 1, [(x["prdlst_nm"], x["status"]) for x in res["diaries"]]
    only = res["diaries"][0]
    assert only["prdlst_nm"] == fixed_nm and only["diary_date"] == diary_date, only
    assert only["status"] in ("OK", "PARTIAL", "EMPTY"), only
    warns = d["generation"]["warnings"]
    if other_nm:
        assert any("작물 고정" in w for w in warns), f"타작물({other_nm}) 제외 경고 없음: {warns}"
    tr = client.get(f"/v1/daily-diaries/{diary_id}/transcript").json()
    assert [(c["prdlst_nm"], c["status"]) for c in tr["crops"]] == [(only["prdlst_nm"], only["status"])], tr.get("crops")
    # 같은 diary_id 에 다른 작물 → 422 CROP_MISMATCH
    r = client.post("/v1/daily-diaries", json={**body, "crop": {"prdlst_nm": other_nm or "가지"}})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "CROP_MISMATCH", r.text
    print(f"[smoke] fixed-crop daily={diary_id} crop={fixed_nm} code={only['prdlst_code']} status={only['status']} "
          f"elapsed={time.monotonic() - t0:.0f}s warnings={warns}")
