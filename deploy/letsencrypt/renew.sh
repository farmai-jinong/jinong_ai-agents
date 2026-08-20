#!/bin/bash
# 인증서 갱신(cron). 갱신 후 호스트 nginx reload. 서버 /srv/jinong-agent/ 에서 실행.
# cron 등록(지농서버, ubuntu): 57 4 * * * /srv/jinong-agent/deploy/letsencrypt/renew.sh >> /srv/jinong-agent/logs/letsencrypt.log 2>&1
set -euo pipefail

LETSENCRYPT_DIR=/srv/jinong-agent/letsencrypt

docker run --rm --name jinong-agent-certbot-renew \
    -v "$LETSENCRYPT_DIR/etc":/etc/letsencrypt \
    -v "$LETSENCRYPT_DIR/lib":/var/lib/letsencrypt \
    -v "$LETSENCRYPT_DIR/data":/data/letsencrypt \
    -v "$LETSENCRYPT_DIR/log":/var/log/letsencrypt \
    certbot/certbot renew --webroot --webroot-path=/data/letsencrypt

sudo nginx -t && sudo systemctl reload nginx
