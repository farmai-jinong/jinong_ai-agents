"""환경별 기대 설정 — `/v1/upstream/health` 의 `config`·`pipeline`·`llm.provider` 와 비교해 `.env` 드리프트를 잡는다.

규칙: 원격 `.env` 를 의도적으로 바꾸면 여기도 같이 바꾼다(관측값을 그대로 베끼지 말 것 — 불일치가 나면 먼저
'의도한 값' 이 무엇인지 확인). 값의 SSOT 는 docs/ops.md §0/§7 과 memory(diary-markdown-variant-gap).
`None` 은 검사하지 않음.
"""

from __future__ import annotations

PROFILES: dict[str, dict] = {
    "dev": {
        "pipeline": "langgraph",
        "llm_provider": "gemini",
        "config": {
            "s3_prefix": "agents/voicecall-dev",
            "storage_impl": "s3",
            "api_markdown_view": "public",
            "callback_include_artifact_keys": True,
            "public_base_url": "https://jinong-stt-report-generation-dev.jinongservice.co.kr",
            # dev 인스턴스는 백엔드 dev. 를 계속 본다
            "summary_callback_url": "https://dev.jinongservice.co.kr/voicetalk/public/call-summary-callback",
        },
    },
    "prod": {
        "pipeline": "langgraph",
        "llm_provider": "gemini",
        "config": {
            "s3_prefix": "agents/voicecall",
            "storage_impl": "s3",
            # 2026-09-07 사용자 지시: 백엔드가 두 벌(view=public 명시, A안) 전환을 받기 전까지 응답 markdown 은 근거
            # 포함본 유지. A안 반영 확인 후 `.env` 와 여기를 함께 "public" 으로.
            "api_markdown_view": "internal",
            "callback_include_artifact_keys": False,
            "callback_enabled": True,
            "public_base_url": "https://jinong-stt-report-generation.jinongservice.co.kr",
            # 2026-09-17 백엔드 통보: 통화요약 웹훅 dev. → data. 전환. agent-callback 은 요청 body 의 callback_url 이라 무관.
            "summary_callback_url": "https://data.jinongservice.co.kr/voicetalk/public/call-summary-callback",
        },
    },
    # 로컬 셀프테스트(STORAGE_IMPL=local ./scripts/run_local.sh) — 스위트 자체를 검증하는 용도, fake 파이프라인만 고정
    "local": {
        "pipeline": "fake",
        "llm_provider": None,
        "config": {},
    },
}
