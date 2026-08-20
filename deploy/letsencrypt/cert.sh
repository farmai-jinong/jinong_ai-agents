#!/bin/bash
# 최초 TLS 인증서 발급(webroot) — 지농서버 입주사 공통 패턴(certbot docker). 서버 /srv/jinong-agent/ 에서 실행.
# 선행: ① DNS A 레코드(jinong-agent.jinongservice.co.kr → 13.125.70.226) ② 호스트 nginx vhost 가
#   포트 80 의 /.well-known/acme-challenge 를 /srv/jinong-agent/letsencrypt/data 로 서빙(HTTPS 블록은 발급 전 주석).
# ⚠️ Let's Encrypt 는 rate-limit 대상 — 최초 검증은 --dry-run 권장: bash cert.sh --dry-run
set -euo pipefail

LETSENCRYPT_DIR=/srv/jinong-agent/letsencrypt
DOMAIN_NAME=jinong-agent.jinongservice.co.kr
ADMIN_EMAIL=ailab.jinong@gmail.com

mkdir -p "$LETSENCRYPT_DIR"/{etc,lib,data,log}

docker run --rm --name jinong-agent-certbot \
    -v "$LETSENCRYPT_DIR/etc":/etc/letsencrypt \
    -v "$LETSENCRYPT_DIR/lib":/var/lib/letsencrypt \
    -v "$LETSENCRYPT_DIR/data":/data/letsencrypt \
    -v "$LETSENCRYPT_DIR/log":/var/log/letsencrypt \
    certbot/certbot \
    certonly --webroot \
    --email "$ADMIN_EMAIL" --agree-tos --no-eff-email \
    --webroot-path=/data/letsencrypt \
    --domain "$DOMAIN_NAME" \
    "$@"
