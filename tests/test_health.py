"""헬스 응답 계약 — `/healthz` 의 `commit`, `/v1/upstream/health` 의 `config`(비밀 아닌 유효 설정) 블록.

tests/smoke 가 이 필드로 배포본 일치·환경 드리프트를 판정하므로 키 이름은 계약이다.
"""

from __future__ import annotations

import re

import httpx
import respx

from app.routes.health import effective_config

from .conftest import STT_URL


async def test_healthz_exposes_commit(client, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc123def456-dirty")
    body = (await client.get("/healthz")).json()
    assert body["status"] == "ok" and body["commit"] == "abc123def456-dirty"
    assert set(body["worker"]) == {"running", "pending_stt", "pending_gen", "pending_daily"}


async def test_healthz_commit_defaults_unknown(client, monkeypatch):
    monkeypatch.delenv("GIT_SHA", raising=False)
    assert (await client.get("/healthz")).json()["commit"] == "unknown"


async def test_upstream_health_config_block(client, app):
    st = app.state.rt.settings
    st.stt_api_key = "stt-secret-key-xyz"
    st.llm_provider, st.llm_api_key = "openai", "llm-secret-key-xyz"     # 로컬 .env 의 gemini 를 덮어 respx 로 프로브
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{STT_URL}/healthz").mock(return_value=httpx.Response(200))
        router.get("https://llm.test/v1/models").mock(return_value=httpx.Response(200, json={"data": []}))
        router.get("https://farmos.test/m/diary/user/prdlsts/list").mock(return_value=httpx.Response(401))
        r = await client.get("/v1/upstream/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stt"]["ok"] and body["llm"]["ok"] and body["s3"]["ok"] and body["farmos"]["ok"], body
    assert body["pipeline"] == "fake"
    assert body["config"] == effective_config(st)
    assert body["config"]["api_markdown_view"] == "public" and body["config"]["s3_prefix"] == "agents/voicecall"
    assert body["config"]["summary_callback_set"] is False
    assert body["config"]["summary_callback_url"] == ""
    # 비밀값은 어디에도 없어야 한다
    assert "secret-key-xyz" not in r.text and "farm_access_token" not in r.text


def test_effective_config_has_no_secret_keys(settings):
    cfg = effective_config(settings)
    for k in cfg:
        assert not re.search(r"(^|_)(api_key|secret|token|password|credentials?)($|_)", k), k
