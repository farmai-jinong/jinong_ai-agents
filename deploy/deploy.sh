#!/usr/bin/env bash
# 지농서버(AWS)에 rsync + 원격 docker compose 빌드/기동 + 배포 후 스모크. jinong_ai-gateway/deploy/deploy.sh 패턴.
#
#   ./deploy/deploy.sh                    # prod: jinong_aws_office (사무실망) → apps/jinong_ai-agents (:7003)
#   ./deploy/deploy.sh dev                # dev : 같은 호스트 apps/jinong_ai-agents-dev (:7013, docs/ops.md §7)
#   REMOTE=jinong_aws ./deploy/deploy.sh  # 외부망(7022)
#
# 순서: 브랜치 가드 → 배포 전 게이트(ruff + pytest) → rsync → 원격 compose(GIT_SHA 주입) → /healthz status=ok·commit 일치
#       → scripts/verify_deploy.sh (tests/smoke: 업스트림·설정 드리프트·계약, dev 는 실녹음 E2E 까지). 어느 단계든 실패 = 배포 실패.
# 스위치: FORCE=1(브랜치≠환경 허용) SKIP_TESTS=1(게이트 생략) SKIP_SMOKE=1(스모크 생략) SMOKE_E2E=0|1(E2E 강제)
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
PY="${PYTHON:-$SRC_DIR/.venv/bin/python}"

# --- 브랜치 가드: rsync 는 워킹트리 기준이므로 dev 트리가 prod 로 올라가는 사고를 막는다
BRANCH="$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
if [ "$BRANCH" != "$ENV" ]; then
  if [ "${FORCE:-0}" = 1 ]; then
    echo "!! 현재 브랜치 '$BRANCH' ≠ 대상 환경 '$ENV' — FORCE=1 이라 진행합니다." >&2
  else
    echo "!! 현재 브랜치 '$BRANCH' 가 대상 환경 '$ENV' 와 다릅니다. 의도한 배포면 FORCE=1 ./deploy/deploy.sh $ENV" >&2
    exit 3
  fi
fi
GIT_SHA="$(git -C "$SRC_DIR" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
if [ -n "$(git -C "$SRC_DIR" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  echo "!! 워킹트리에 커밋되지 않은 변경이 있습니다 — 그대로 올라가며 commit 은 '${GIT_SHA}-dirty' 로 기록됩니다." >&2
  GIT_SHA="${GIT_SHA}-dirty"
fi

# --- 배포 전 게이트: CI 와 같은 명령(네트워크 없음)
if [ "${SKIP_TESTS:-0}" != 1 ]; then
  [ -x "$PY" ] || { echo "!! python 없음: $PY — SKIP_TESTS=1 로 생략 가능하나 권장하지 않음" >&2; exit 2; }
  echo "==> gate: ruff + pytest"
  (cd "$SRC_DIR" && "$PY" -m ruff check app tests && "$PY" -m pytest -q -p no:cacheprovider)
fi

echo "==> deploying [$ENV] $SRC_DIR (branch $BRANCH, commit $GIT_SHA)  ->  $REMOTE:$REMOTE_DIR"
ssh "$REMOTE" "mkdir -p '$REMOTE_DIR'"

rsync -az --delete \
  --exclude '.git' --exclude '.github' --exclude '.env' --exclude '__pycache__' \
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

GIT_SHA="$GIT_SHA" docker compose up -d --build

echo "==> waiting for health (\$BIND, commit $GIT_SHA)..."
for i in \$(seq 1 30); do
  if BODY=\$(curl -fsS "http://\$BIND/healthz" 2>/dev/null); then
    RESULT=\$(printf '%s' "\$BODY" | python3 -c '
import sys, json
d = json.load(sys.stdin); want = sys.argv[1]
if d.get("status") != "ok": print("degraded"); sys.exit(0)
if d.get("commit") != want: print("stale:" + str(d.get("commit"))); sys.exit(0)
print("ok")' "$GIT_SHA")
    case "\$RESULT" in
      ok) echo "==> agent healthy: \$BODY"; exit 0 ;;
      degraded) echo "!! healthz status=degraded (DB ping 실패): \$BODY" >&2 ;;
      stale:*) [ \$i -ge 15 ] && echo "!! 아직 구 이미지(commit \${RESULT#stale:}) 응답 중..." >&2 ;;
    esac
  fi
  sleep 2
done
echo "!! agent did not become healthy with commit $GIT_SHA; recent logs:" >&2
docker compose logs --tail=60 agent >&2
exit 1
REMOTE_SCRIPT

if [ "${SKIP_SMOKE:-0}" = 1 ]; then
  echo "==> SKIP_SMOKE=1 — 스모크 생략. 수동: GIT_SHA=$GIT_SHA ./scripts/verify_deploy.sh $ENV"
else
  if ! GIT_SHA="$GIT_SHA" REMOTE="$REMOTE" REMOTE_DIR="$REMOTE_DIR" "$SRC_DIR/scripts/verify_deploy.sh" "$ENV"; then
    echo "!! 배포 후 스모크 실패 [$ENV] — 최근 로그:" >&2
    ssh "$REMOTE" "cd '$REMOTE_DIR' && docker compose logs --tail=80 agent" >&2 || true
    exit 1
  fi
fi
echo "==> done [$ENV] commit $GIT_SHA."
