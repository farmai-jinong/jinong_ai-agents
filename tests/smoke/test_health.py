"""빠른 스모크(수 초) — 헬스·배포 커밋·인증·업스트림·설정 드리프트·목록/오류 계약. 배포마다 항상 실행."""

from __future__ import annotations

import pytest


def _code(r) -> str:
    d = r.json().get("detail")
    return d.get("code") if isinstance(d, dict) else str(d)


def test_healthz_ok_and_commit(anon, smoke_env):
    r = anon.get("/healthz")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "ok", f"degraded(DB ping 실패): {b}"
    assert b["worker"]["running"] is True, b
    assert all(isinstance(b["worker"][k], int) for k in ("pending_stt", "pending_gen", "pending_daily")), b
    if smoke_env["expect_commit"]:
        assert b["commit"] == smoke_env["expect_commit"], \
            f"배포본 불일치: 서버 commit={b['commit']} 기대={smoke_env['expect_commit']} (구 이미지가 떠 있음?)"


def test_auth_fail_closed(anon, smoke_env):
    if not smoke_env["api_key"]:
        pytest.skip("SMOKE_API_KEY 없음(무인증 로컬)")
    assert anon.get("/v1/calls?limit=1").status_code == 401
    assert anon.get("/v1/calls?limit=1", headers={"Authorization": "Bearer wrong-key"}).status_code == 401
    assert anon.get("/v1/calls?limit=1", headers={"X-API-Key": "wrong-key"}).status_code == 401


def test_upstream_reachable_and_config_matches_profile(client, smoke_env):
    r = client.get("/v1/upstream/health", timeout=60)
    assert r.status_code == 200, r.text
    b = r.json()
    bad = {k: b[k] for k in ("stt", "llm", "s3", "farmos") if not b[k].get("ok")}
    assert not bad, f"업스트림 도달 실패: {bad}"
    if "ap_backend" in b:
        assert b["ap_backend"].get("ok"), b["ap_backend"]

    prof = smoke_env["profile"]
    drift = {}
    if prof.get("pipeline") and b["pipeline"] != prof["pipeline"]:
        drift["pipeline"] = (b["pipeline"], prof["pipeline"])
    if prof.get("llm_provider") and b["llm"]["provider"] != prof["llm_provider"]:
        drift["llm.provider"] = (b["llm"]["provider"], prof["llm_provider"])
    cfg = b.get("config") or {}
    for k, want in prof["config"].items():
        if want is not None and cfg.get(k) != want:
            drift[f"config.{k}"] = (cfg.get(k), want)
    assert not drift, "설정 드리프트 {key: (서버값, 기대값)} — .env 또는 tests/smoke/profiles.py 정정: " + repr(drift)


@pytest.mark.parametrize("path", ["/v1/calls", "/v1/daily-diaries"])
def test_list_and_cursor_contract(client, path):
    r = client.get(path, params={"limit": 1})
    assert r.status_code == 200, r.text
    b = r.json()
    assert isinstance(b.get("items"), list) and "next_cursor" in b, b
    r = client.get(path, params={"limit": 1, "cursor": "not-a-cursor"})
    assert r.status_code == 422 and _code(r) == "INVALID_CURSOR", r.text
    assert client.get(path, params={"limit": 0}).status_code == 422


def test_not_found_and_invalid_view(client):
    r = client.get("/v1/calls/smoke-does-not-exist")
    assert r.status_code == 404 and _code(r) == "CALL_NOT_FOUND", r.text
    r = client.get("/v1/daily-diaries/smoke-does-not-exist")
    assert r.status_code == 404 and _code(r) == "DAILY_NOT_FOUND", r.text
    # view 검증은 DB 조회보다 먼저 — 존재하지 않는 call 로도 400
    r = client.get("/v1/calls/smoke-does-not-exist/artifacts/report", params={"view": "bogus"})
    assert r.status_code == 400 and _code(r) == "INVALID_VIEW", r.text
