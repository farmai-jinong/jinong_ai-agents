#!/usr/bin/env bash
# 배포된 인스턴스 스모크(tests/smoke) — deploy.sh 가 마지막 단계로 호출하고, 단독으로도 쓴다.
#
#   ./scripts/verify_deploy.sh dev              # 원격 .env 에서 BIND·키를 읽고 ssh 터널로 :7013 스모크 (+E2E 기본 on)
#   ./scripts/verify_deploy.sh prod             # :7003 빠른 스모크만 (E2E 는 SMOKE_E2E=1)
#   SMOKE_E2E=1 ./scripts/verify_deploy.sh prod
#   SMOKE_URL=https://jinong-stt-report-generation.jinongservice.co.kr SMOKE_API_KEY=… ./scripts/verify_deploy.sh prod   # 터널 없이 공개 URL
#   GIT_SHA=<sha> ./scripts/verify_deploy.sh dev   # /healthz commit 일치까지 판정 (deploy.sh 가 넘김)
#
# 기대 설정(환경별 .env 드리프트 판정)은 tests/smoke/profiles.py. 실녹음 좌표는 deploy/smoke.env.
set -euo pipefail

ENV="${1:-${ENV:-prod}}"
case "$ENV" in
  prod) DEFAULT_DIR=/home/ubuntu/apps/jinong_ai-agents;     DEFAULT_LPORT=17003 ;;
  dev)  DEFAULT_DIR=/home/ubuntu/apps/jinong_ai-agents-dev; DEFAULT_LPORT=17013 ;;
  *)    echo "usage: $0 [prod|dev]" >&2; exit 2 ;;
esac
REMOTE="${REMOTE:-jinong_aws_office}"
REMOTE_DIR="${REMOTE_DIR:-$DEFAULT_DIR}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$SRC_DIR/.venv/bin/python}"
[ -x "$PY" ] || { echo "!! python 없음: $PY (uv venv --python 3.12 .venv && uv pip install -r requirements-dev.txt)" >&2; exit 2; }

# E2E 기본: dev on / prod off
SMOKE_E2E="${SMOKE_E2E:-$([ "$ENV" = dev ] && echo 1 || echo 0)}"
if [ "$SMOKE_E2E" = 1 ]; then
  # shellcheck disable=SC1091
  [ -f "$SRC_DIR/deploy/smoke.env" ] && set -a && . "$SRC_DIR/deploy/smoke.env" && set +a
  [ -n "${SMOKE_AUDIO_BUCKET:-}" ] && [ -n "${SMOKE_AUDIO_KEY:-}" ] || {
    echo "!! SMOKE_E2E=1 인데 SMOKE_AUDIO_BUCKET/SMOKE_AUDIO_KEY 가 없음 — deploy/smoke.env 확인" >&2; exit 2; }
fi

SOCK=""
cleanup() { [ -n "$SOCK" ] && ssh -S "$SOCK" -O exit "$REMOTE" >/dev/null 2>&1 || true; }
trap cleanup EXIT

if [ -z "${SMOKE_URL:-}" ]; then
  # 원격 .env 에서 BIND·첫 API 키를 읽는다(값은 출력하지 않음) → 로컬 포트로 ssh 터널
  REMOTE_ENV="$(ssh "$REMOTE" "grep -E '^(AGENT_BIND|AGENT_API_KEY)=' '$REMOTE_DIR/.env'" 2>/dev/null || true)"
  BIND="$(printf '%s\n' "$REMOTE_ENV" | grep -E '^AGENT_BIND=' | cut -d= -f2- | tr -d '[:space:]')"
  BIND="${BIND:-127.0.0.1:7003}"
  SMOKE_API_KEY="${SMOKE_API_KEY:-$(printf '%s\n' "$REMOTE_ENV" | grep -E '^AGENT_API_KEY=' | cut -d= -f2- | cut -d, -f1 | tr -d '[:space:]')}"
  [ -n "$SMOKE_API_KEY" ] || { echo "!! 원격 .env 에 AGENT_API_KEY 가 없음 ($REMOTE:$REMOTE_DIR/.env)" >&2; exit 2; }
  LPORT="${LPORT:-$DEFAULT_LPORT}"
  SOCK="$(mktemp -u "${TMPDIR:-/tmp}/verify-deploy-$ENV.XXXXXX")"
  ssh -f -N -M -S "$SOCK" -o ExitOnForwardFailure=yes -L "127.0.0.1:$LPORT:$BIND" "$REMOTE"
  SMOKE_URL="http://127.0.0.1:$LPORT"
  echo "==> tunnel 127.0.0.1:$LPORT -> $REMOTE:$BIND"
fi

echo "==> smoke [$ENV] $SMOKE_URL  e2e=$SMOKE_E2E  expect_commit=${GIT_SHA:-<none>}"
cd "$SRC_DIR"
SMOKE_URL="$SMOKE_URL" SMOKE_ENV="$ENV" SMOKE_API_KEY="${SMOKE_API_KEY:-}" SMOKE_EXPECT_COMMIT="${GIT_SHA:-}" \
SMOKE_E2E="$SMOKE_E2E" SMOKE_AUDIO_BUCKET="${SMOKE_AUDIO_BUCKET:-}" SMOKE_AUDIO_KEY="${SMOKE_AUDIO_KEY:-}" \
SMOKE_FARM_TOKEN="${SMOKE_FARM_TOKEN:-}" \
  "$PY" -m pytest -m smoke tests/smoke -q -ra -p no:cacheprovider -s --tb=short
echo "==> smoke [$ENV] passed."
