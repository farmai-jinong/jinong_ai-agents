#!/usr/bin/env bash
# 지농서버(AWS)에 rsync + 원격 docker compose 빌드/기동. jinong_ai-gateway/deploy/deploy.sh 패턴.
#
#   ./deploy/deploy.sh                    # jinong_aws_office (사무실망)
#   REMOTE=jinong_aws ./deploy/deploy.sh  # 외부망(7022)
#
# 원격 .env 는 절대 덮어쓰지 않는다(rsync exclude). 없으면 .env.example 을 복사만 하고 경고 → 앱은 fail-closed 로
# 기동 거부하므로 키를 채운 뒤 재실행.
set -euo pipefail

REMOTE="${REMOTE:-jinong_aws_office}"
REMOTE_DIR="${REMOTE_DIR:-/home/ubuntu/apps/jinong_ai-agents}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> deploying $SRC_DIR  ->  $REMOTE:$REMOTE_DIR"
ssh "$REMOTE" "mkdir -p '$REMOTE_DIR'"

rsync -az --delete \
  --exclude '.git' --exclude '.env' --exclude '__pycache__' \
  --exclude '.venv' --exclude 'venv' --exclude 'data' --exclude 'out' \
  --exclude '.ruff_cache' --exclude '.mypy_cache' --exclude '.pytest_cache' \
  "$SRC_DIR/" "$REMOTE:$REMOTE_DIR/"

ssh "$REMOTE" bash -se <<REMOTE_SCRIPT
set -euo pipefail
cd "$REMOTE_DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "!! created .env from .env.example — AGENT_API_KEY 등이 비어 있어 기동이 거부됩니다(fail-closed)." >&2
  echo "!! .env 를 채운 뒤(AGENT_API_KEY, STT_API_KEY, LLM_*, AWS_*) 다시 deploy.sh 를 실행하세요." >&2
fi

docker compose up -d --build

echo "==> waiting for health..."
for i in \$(seq 1 30); do
  if curl -fsS http://127.0.0.1:7003/healthz >/dev/null 2>&1; then
    echo "==> agent healthy:"; curl -s http://127.0.0.1:7003/healthz; echo
    KEY=\$(grep -E '^AGENT_API_KEY=' .env | cut -d= -f2- | cut -d, -f1 | tr -d '[:space:]')
    if [ -n "\$KEY" ]; then
      echo "==> upstream reachability:"; curl -s -H "Authorization: Bearer \$KEY" http://127.0.0.1:7003/v1/upstream/health; echo
    fi
    exit 0
  fi
  sleep 2
done
echo "!! agent did not become healthy; recent logs:" >&2
docker compose logs --tail=60 agent >&2
exit 1
REMOTE_SCRIPT

echo "==> done."
