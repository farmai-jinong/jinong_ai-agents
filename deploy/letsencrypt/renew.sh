#!/bin/bash
# 인증서 갱신(cron). 갱신 후 호스트 nginx reload. 서버 /srv/$INSTANCE/ 에서 실행. 기본값은 prod(jinong-agent).
# cron 등록(지농서버, root crontab):
#   57 4 * * * /srv/jinong-agent/deploy/letsencrypt/renew.sh >> /srv/jinong-agent/logs/letsencrypt.log 2>&1
#   58 4 * * * INSTANCE=jinong-agent-dev /srv/jinong-agent-dev/deploy/letsencrypt/renew.sh >> /srv/jinong-agent-dev/logs/letsencrypt.log 2>&1
set -euo pipefail

INSTANCE="${INSTANCE:-jinong-agent}"
LETSENCRYPT_DIR="${LETSENCRYPT_DIR:-/srv/$INSTANCE/letsencrypt}"

docker run --rm --name "$INSTANCE-certbot-renew" \
    -v "$LETSENCRYPT_DIR/etc":/etc/letsencrypt \
    -v "$LETSENCRYPT_DIR/lib":/var/lib/letsencrypt \
    -v "$LETSENCRYPT_DIR/data":/data/letsencrypt \
    -v "$LETSENCRYPT_DIR/log":/var/log/letsencrypt \
    certbot/certbot renew --webroot --webroot-path=/data/letsencrypt

sudo nginx -t && sudo systemctl reload nginx
