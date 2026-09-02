#!/usr/bin/env bash
# 배포된 서비스 스모크: 헬스 → 업스트림 → (키 주어지면) 전체 플로우.
#   AGENT_API_KEY=... ./scripts/smoke_remote.sh [bucket key]
#   dev: AGENT_URL=https://jinong-stt-report-generation-dev.jinongservice.co.kr AGENT_API_KEY=<dev 키> ./scripts/smoke_remote.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export AGENT_URL="${AGENT_URL:-https://jinong-stt-report-generation.jinongservice.co.kr}"
echo "==> healthz"; curl -fsS "$AGENT_URL/healthz"; echo
if [ -n "${AGENT_API_KEY:-}" ]; then
  echo "==> upstream"; curl -fsS -H "Authorization: Bearer $AGENT_API_KEY" "$AGENT_URL/v1/upstream/health"; echo
  if [ $# -ge 2 ]; then exec ./scripts/curl_flow.sh "$1" "$2"; fi
fi
