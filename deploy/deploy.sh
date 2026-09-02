#!/usr/bin/env bash
# 지농서버(AWS)에 rsync + 원격 docker compose 빌드/기동. jinong_ai-gateway/deploy/deploy.sh 패턴.
#
#   ./deploy/deploy.sh                    # prod: jinong_aws_office (사무실망) → apps/jinong_ai-agents (:7003)
#   ./deploy/deploy.sh dev                # dev : 같은 호스트 apps/jinong_ai-agents-dev (:7013, docs/ops.md §7)
#   REMOTE=jinong_aws ./deploy/deploy.sh  # 외부망(7022)
#
# 원격 .env 는 절대 덮어쓰지 않는다(rsync exclude). 없으면 .env.example 을 복사만 하고 경고 → 앱은 fail-closed 로
# 기동 거부하므로 키를 채운 뒤 재실행. 헬스 폴링 포트는 원격 .env 의 AGENT_BIND 에서 읽는다(기본 127.0.0.1:7003).
set -euo pipefail

ENV="${1:-${ENV:-prod}}"
case "$ENV" in
  prod) DEFAULT_DIR=/home/ubuntu/apps/jinong_ai-agents ;;
  dev)  DEFAULT_DIR=/home/ubuntu/apps/jinong_ai-agents-dev ;;
  *)    echo "usage: $0 [prod|dev]" >&2; exit 2 ;;
esac
REMOTE="${REMOTE:-jinong_aws_office}"
REMOTE_DIR="${REMOTE_DIR:-$DEFAULT_DIR}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BRANCH="$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
if [ "$BRANCH" != "$ENV" ]; then
  echo "!! 현재 브랜치 '$BRANCH' 가 대상 환경 '$ENV' 와 다릅니다 — rsync 는 워킹트리 기준이므로 그대로 올라갑니다. (5초 후 진행, Ctrl-C 로 중단)" >&2
  sleep 5
fi
echo "==> deploying [$ENV] $SRC_DIR (branch $BRANCH)  ->  $REMOTE:$REMOTE_DIR"
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
  if [ "$ENV" = dev ]; then
    echo "!! dev 는 prod .env 에서 파생해 PUBLIC_BASE_URL/S3_PREFIX/AGENT_BIND/AGENT_API_KEY 만 바꾸고" >&2
    echo "!! AGENT_CONTAINER_NAME/AGENT_IMAGE_TAG 를 덧붙인다 — docs/ops.md §7 의 명령 블록 참조." >&2
  fi
fi

BIND=\$(grep -E '^AGENT_BIND=' .env | cut -d= -f2- | tr -d '[:space:]')
BIND=\${BIND:-127.0.0.1:7003}

docker compose up -d --build

echo "==> waiting for health (\$BIND)..."
for i in \$(seq 1 30); do
  if curl -fsS "http://\$BIND/healthz" >/dev/null 2>&1; then
    echo "==> agent healthy:"; curl -s "http://\$BIND/healthz"; echo
    KEY=\$(grep -E '^AGENT_API_KEY=' .env | cut -d= -f2- | cut -d, -f1 | tr -d '[:space:]')
    if [ -n "\$KEY" ]; then
      echo "==> upstream reachability:"; curl -s -H "Authorization: Bearer \$KEY" "http://\$BIND/v1/upstream/health"; echo
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
