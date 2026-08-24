"""런타임 설정 — 환경변수(12-factor)에서 로드. jinong_ai-gateway `app/config.py` 패턴.

이 서비스(⑧ AI Agent)는 모델/GPU를 갖지 않는다. STT·LLM 호출은 전부 ⑥ 게이트웨이를 경유하고,
오디오는 S3 참조(bucket/key)로만 받는다. 값의 SSOT는 `.env.example` 주석 참조.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # --- auth (fail-closed; 게이트웨이와 동일) --------------------------------
    agent_api_key: str = ""            # 콤마 구분 다중 키 (클라이언트별 발급/회수)
    allow_no_auth: bool = False        # 로컬 개발 전용 탈출구

    # --- server ---------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"
    public_base_url: str = "https://jinong-stt-report-generation.jinongservice.co.kr"   # 콜백 result_url 조립용

    # --- storage --------------------------------------------------------------
    db_path: str = "/data/agent.db"
    # 산출물 버킷 — 백엔드 MinIO(S3_ENDPOINT_URL)에 동명 생성(2026-08). AWS 동명 버킷(audio_labeler SSOT:
    # audio_labeler-web/config.prd.yaml)과는 별개 인스턴스. 입력 녹음은 호출자 참조(voice-recordings)로만 읽는다.
    s3_bucket: str = "jinong-agri-stt"
    s3_prefix: str = "agents/voicecall"
    aws_region: str = "ap-northeast-2"
    s3_endpoint_url: str | None = None
    s3_head_on_ingest: bool = True
    max_audio_mb: int = 200            # 게이트웨이 MAX_UPLOAD_MB 와 정합
    storage_impl: str = "s3"           # s3 | local — local 은 로컬 개발 전용(boto3 미사용, 파일시스템)
    local_storage_dir: str = "./data/storage"   # local: 산출물 루트 (S3_PREFIX 레이아웃 그대로 미러)
    local_audio_dir: str = "./data/audio-in"    # local: 입력 오디오 루트 (bucket != S3_BUCKET 인 참조)

    # --- STT (⑥ 게이트웨이) --------------------------------------------------
    stt_base_url: str = "https://jinong-stt.jinongservice.co.kr"
    stt_api_key: str = ""
    stt_timeout: float = 900.0         # 게이트웨이 upstream_timeout(1200) 보다 아래
    stt_connect_timeout: float = 10.0
    stt_max_concurrency: int = 2       # capacity.md: 단일 GPU 실질 ~2
    stt_max_attempts: int = 4

    # --- LLM (provider 전환은 env 몇 개) ---------------------------------------
    # openai | jinong : OpenAI 호환(ChatOpenAI) — LLM_BASE_URL/LLM_API_KEY/LLM_MODEL
    # gemini          : Vertex AI(ChatGoogleGenerativeAI(vertexai=True)) — LLM_MODEL + GCP_PROJECT_ID/GCP_LOCATION + GOOGLE_APPLICATION_CREDENTIALS(SA 키)
    llm_provider: str = "openai"       # openai | jinong | gemini
    llm_base_url: str = "https://api.openai.com/v1"   # jinong: https://jinong-stt.jinongservice.co.kr/v1 (gemini 는 미사용)
    llm_api_key: str = ""              # gemini 는 미사용(SA 키 파일)
    llm_model: str = "gpt-4.1"         # jinong: exaone45 / gemini: gemini-3.5-flash
    llm_extra_body: str = ""           # JSON(OpenAI 호환 전용). jinong 기본 {"chat_template_kwargs":{"enable_thinking":false}}
    gcp_project_id: str = ""           # gemini: Vertex 프로젝트(예: jinong-lab-llm). 비우면 SA 키의 프로젝트
    gcp_location: str = "global"       # gemini: Vertex location
    google_application_credentials: str = ""   # gemini: SA 키 경로. .env 로만 주면 팩토리가 os.environ 에 주입(ADC 용)
    gemini_thinking_level: str = ""    # gemini 3+: minimal|low|medium|high. 비우면 모델 기본(동적). 비용/지연 조절용
    llm_structured_mode: str = "auto"  # auto | json_schema | json_mode | prompt
    llm_temperature: float = 0.0
    llm_seed: int = 42
    llm_timeout: float = 120.0
    llm_max_retries: int = 2

    # --- generation / pipeline ------------------------------------------------
    pipeline_impl: str = "langgraph"   # langgraph | fake
    gen_max_concurrency: int = 2
    gen_max_attempts: int = 2
    gen_timeout_sec: float = 600.0
    node_timeout_s: float = 180.0
    extract_max_input_tokens: int = 14000
    chunk_tokens: int = 8000
    chunk_overlap_turns: int = 6
    prompt_dump_dir: str = ""          # 비어있지 않으면 프롬프트/응답 덤프 (디버그)

    # --- farmos (kafka-gateway) 읽기 전용 -------------------------------------
    farmos_base_url: str = "https://dev.jinongservice.co.kr"
    farmos_timeout: float = 10.0

    # --- callback (선택) -----------------------------------------------------
    callback_enabled: bool = False
    callback_api_key: str = ""
    callback_timeout: float = 10.0

    # --- worker ---------------------------------------------------------------
    worker_poll_sec: float = 5.0
    end_stt_deadline_sec: int = 3600
    shutdown_grace_sec: float = 20.0
    audio_autocreate_call: bool = True
    token_purge_on_terminal: bool = True
    result_inline_max_kb: int = 512
    tz: str = "Asia/Seoul"

    @field_validator("llm_provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        v = (v or "openai").strip().lower()
        if v not in ("openai", "jinong", "gemini"):
            raise ValueError("LLM_PROVIDER must be openai|jinong|gemini")
        return v

    @field_validator("storage_impl")
    @classmethod
    def _storage(cls, v: str) -> str:
        v = (v or "s3").strip().lower()
        if v not in ("s3", "local"):
            raise ValueError("STORAGE_IMPL must be s3|local")
        return v

    @property
    def api_keys(self) -> list[str]:
        return [k.strip() for k in self.agent_api_key.split(",") if k.strip()]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

    @property
    def llm_extra_body_dict(self) -> dict[str, Any] | None:
        if self.llm_provider == "gemini":
            return None
        if self.llm_extra_body.strip():
            return json.loads(self.llm_extra_body)
        if self.llm_provider == "jinong":
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return None

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
