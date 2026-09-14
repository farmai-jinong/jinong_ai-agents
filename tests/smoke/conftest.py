"""스모크 설정은 env 로만 받는다(비밀은 인자로 넘기지 않음).

  SMOKE_URL            대상 base URL (필수, 없으면 전체 skip)         예: http://127.0.0.1:17013
  SMOKE_ENV            dev | prod | local (필수) — tests/smoke/profiles.py 의 기대 설정 선택
  SMOKE_API_KEY        AGENT_API_KEY (local 무인증이면 생략)
  SMOKE_EXPECT_COMMIT  /healthz commit 기대값(deploy.sh 가 GIT_SHA 로 넘김; 생략 시 미검사)
  SMOKE_E2E=1          실녹음 E2E(test_e2e.py) 실행 — SMOKE_AUDIO_BUCKET/SMOKE_AUDIO_KEY 필요
  SMOKE_FARM_TOKEN     (옵션) 농가 JWT — 주면 farmos 조회 포함 경로로 E2E
  SMOKE_REUSE_CALL_ID  (진단용) 새 통화 없이 이미 COMPLETED 인 call 로 E2E 판정만 재실행(STT·LLM 비용 없음)
"""

from __future__ import annotations

import logging
import os

import httpx
import pytest

from .profiles import PROFILES

logging.getLogger("httpx").setLevel(logging.WARNING)      # 요청 1건마다 INFO 한 줄 — 스모크 출력에서는 잡음


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "tests/smoke" in str(item.fspath) or "tests\\smoke" in str(item.fspath):
            item.add_marker(pytest.mark.smoke)


@pytest.fixture(scope="session")
def smoke_env() -> dict:
    url = os.environ.get("SMOKE_URL", "").rstrip("/")
    env = os.environ.get("SMOKE_ENV", "")
    if not url or not env:
        pytest.skip("SMOKE_URL/SMOKE_ENV 미설정 — scripts/verify_deploy.sh 로 실행")
    if env not in PROFILES:
        pytest.fail(f"SMOKE_ENV={env!r} 는 profiles.py 에 없음 ({', '.join(PROFILES)})")
    return {
        "url": url, "env": env, "profile": PROFILES[env],
        "api_key": os.environ.get("SMOKE_API_KEY", ""),
        "expect_commit": os.environ.get("SMOKE_EXPECT_COMMIT", ""),
        "e2e": os.environ.get("SMOKE_E2E", "") == "1",
        "audio_bucket": os.environ.get("SMOKE_AUDIO_BUCKET", ""),
        "audio_key": os.environ.get("SMOKE_AUDIO_KEY", ""),
        "farm_token": os.environ.get("SMOKE_FARM_TOKEN", ""),
    }


@pytest.fixture(scope="session")
def client(smoke_env) -> httpx.Client:
    headers = {"Authorization": f"Bearer {smoke_env['api_key']}"} if smoke_env["api_key"] else {}
    with httpx.Client(base_url=smoke_env["url"], headers=headers, timeout=30) as c:
        yield c


@pytest.fixture(scope="session")
def anon(smoke_env) -> httpx.Client:
    """인증 헤더 없는 클라이언트 — fail-closed 확인용."""
    with httpx.Client(base_url=smoke_env["url"], timeout=30) as c:
        yield c
