#!/usr/bin/env bash
# 완료된 call 들을 날짜별(멀티콜) 영농일지로 집계 → 완료까지 폴링 → 작물별 일지 md 출력.
#   ./scripts/daily_flow.sh <call_id> [call_id...]
#   AGENT_URL=http://127.0.0.1:7003 AGENT_API_KEY=... FARM_TOKEN=<농가 JWT> \
#     DIARY_ID=daily_farmer1_20260821 DIARY_DATE=2026-08-21 ./scripts/daily_flow.sh call-1 call-2
# 전제: call_ids 전부 terminal(COMPLETED/EMPTY)이고 1개 이상 COMPLETED — 아니면 409/422.
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: $0 <call_id> [call_id...]" >&2; exit 2; }
AGENT_URL="${AGENT_URL:-http://127.0.0.1:7003}"
DIARY_ID="${DIARY_ID:-daily-test-$(date +%Y%m%d%H%M%S)}"
DIARY_DATE="${DIARY_DATE:-$(date +%Y-%m-%d)}"
AUTH=(); [ -n "${AGENT_API_KEY:-}" ] && AUTH=(-H "Authorization: Bearer $AGENT_API_KEY")
JSON=(-H 'Content-Type: application/json')
CALL_IDS=$(printf '"%s",' "$@"); CALL_IDS="[${CALL_IDS%,}]"

echo "==> trigger $DIARY_ID ($DIARY_DATE, calls=$CALL_IDS)"
curl -fsS "${AUTH[@]}" "${JSON[@]}" -X POST "$AGENT_URL/v1/daily-diaries" -d @- <<EOF_JSON | head -c 400; echo
{"diary_id":"$DIARY_ID","diary_date":"$DIARY_DATE","call_ids":$CALL_IDS,
 "farm_access_token":"${FARM_TOKEN:-}"}
EOF_JSON

echo "==> polling"
for i in $(seq 1 240); do
  STATUS=$(curl -fsS "${AUTH[@]}" "$AGENT_URL/v1/daily-diaries/$DIARY_ID?inline=false" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["status"], d["generation"]["state"], d.get("error"))')
  echo "   [$i] $STATUS"
  case "$STATUS" in COMPLETED*|EMPTY*|FAILED*) break;; esac
  sleep 5
done

echo "==> merged transcript (head)"
curl -fsS "${AUTH[@]}" "$AGENT_URL/v1/daily-diaries/$DIARY_ID/transcript" | head -c 600 || true; echo

echo "==> diaries"
CODES=$(curl -fsS "${AUTH[@]}" "$AGENT_URL/v1/daily-diaries/$DIARY_ID?inline=false" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d.get("result") or {}; [print(x["prdlst_code"]) for x in r.get("diaries",[])]')
for CODE in $CODES; do
  echo "--- diary [$CODE] ---"
  curl -fsS "${AUTH[@]}" "$AGENT_URL/v1/daily-diaries/$DIARY_ID/artifacts/diary/$CODE" || true; echo
done
