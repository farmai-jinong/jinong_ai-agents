#!/usr/bin/env bash
# 로컬 raw audio 파일로 E2E: 파일을 LOCAL_AUDIO_DIR 에 복사한 뒤 curl_flow.sh 를 bucket="local" 로 구동.
# 서버는 먼저 STORAGE_IMPL=local ./scripts/run_local.sh 로 띄워둘 것 (STT 게이트웨이 접근은 필요).
#   ./scripts/e2e_local.sh <audio-file> [call_id]
set -euo pipefail
FILE="${1:?audio file}"; CALL_ID="${2:-local-$(date +%Y%m%d%H%M%S)}"
cd "$(dirname "$0")/.."
AUDIO_DIR="${LOCAL_AUDIO_DIR:-./data/audio-in}"
mkdir -p "$AUDIO_DIR"
BASE="$(basename "$FILE")"
cp -f "$FILE" "$AUDIO_DIR/$BASE"
exec ./scripts/curl_flow.sh local "$BASE" "$CALL_ID"
