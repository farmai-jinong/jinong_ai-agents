#!/usr/bin/env bash
# 로컬 실행. 기본은 무인증 + fake 파이프라인(LLM/farmos 불필요). 실제 LLM 은 .env 의 LLM_* 를 채우고
# PIPELINE_IMPL=langgraph 로 실행.
#   ./scripts/run_local.sh                       # fake
#   PIPELINE_IMPL=langgraph ./scripts/run_local.sh
#   STORAGE_IMPL=local ./scripts/run_local.sh    # S3 없이 로컬 파일 스토리지 (scripts/e2e_local.sh 와 사용)
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] && source .venv/bin/activate
mkdir -p data
export ALLOW_NO_AUTH="${ALLOW_NO_AUTH:-1}"
export PIPELINE_IMPL="${PIPELINE_IMPL:-fake}"
export DB_PATH="${DB_PATH:-./data/agent.db}"
export STORAGE_IMPL="${STORAGE_IMPL:-s3}"
export LOCAL_STORAGE_DIR="${LOCAL_STORAGE_DIR:-./data/storage}"
export LOCAL_AUDIO_DIR="${LOCAL_AUDIO_DIR:-./data/audio-in}"
export PORT="${PORT:-7003}"
exec uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --reload
