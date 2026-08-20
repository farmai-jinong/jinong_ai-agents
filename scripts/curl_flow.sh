#!/usr/bin/env bash
# 전화 시작 → 녹음(S3 참조) → 종료 → 완료까지 폴링 → 보고서 md 출력.
#   ./scripts/curl_flow.sh <bucket> <key> [call_id]
#   AGENT_URL=http://127.0.0.1:7003 AGENT_API_KEY=... FARM_TOKEN=<농가 JWT> ./scripts/curl_flow.sh jinong-agri-stt raw/xxx.wav
set -euo pipefail
BUCKET="${1:?bucket}"; KEY="${2:?key}"; CALL_ID="${3:-test-$(date +%Y%m%d%H%M%S)}"
AGENT_URL="${AGENT_URL:-http://127.0.0.1:7003}"
AUTH=(); [ -n "${AGENT_API_KEY:-}" ] && AUTH=(-H "Authorization: Bearer $AGENT_API_KEY")
JSON=(-H 'Content-Type: application/json')

echo "==> start $CALL_ID"
curl -fsS "${AUTH[@]}" "${JSON[@]}" -X POST "$AGENT_URL/v1/calls" -d @- <<EOF_JSON | head -c 400; echo
{"call_id":"$CALL_ID","started_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)",
 "participants":[{"role":"farmer","user_id":"${FARMER_ID:-farmer1}","name":"${FARMER_NAME:-농가}"},
                 {"role":"consultant","user_id":"${CONSULTANT_ID:-cons1}","name":"${CONSULTANT_NAME:-컨설턴트}"}],
 "farm_access_token":"${FARM_TOKEN:-}",
 "metadata":{"hints":{"prdlst_code":"${PRDLST_CODE:-}","prdlst_nm":"${PRDLST_NM:-}"}}}
EOF_JSON

echo "==> audio s3://$BUCKET/$KEY"
curl -fsS "${AUTH[@]}" "${JSON[@]}" -X POST "$AGENT_URL/v1/calls/$CALL_ID/audio" \
  -d "{\"bucket\":\"$BUCKET\",\"key\":\"$KEY\",\"seq\":1}" | head -c 400; echo

echo "==> end"
curl -fsS "${AUTH[@]}" "${JSON[@]}" -X POST "$AGENT_URL/v1/calls/$CALL_ID/end" -d '{}' | head -c 300; echo

echo "==> polling"
for i in $(seq 1 240); do
  STATUS=$(curl -fsS "${AUTH[@]}" "$AGENT_URL/v1/calls/$CALL_ID?inline=false" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["status"], d["stt_progress"], d.get("error"))')
  echo "   [$i] $STATUS"
  case "$STATUS" in COMPLETED*|EMPTY*|FAILED*) break;; esac
  sleep 5
done

echo "==> report"
curl -fsS "${AUTH[@]}" "$AGENT_URL/v1/calls/$CALL_ID/artifacts/report" || true
echo; echo "==> diaries"
curl -fsS "${AUTH[@]}" "$AGENT_URL/v1/calls/$CALL_ID?inline=false" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d.get("result") or {}; [print(x["prdlst_code"], x["prdlst_nm"], x["status"], x["s3_key_md"]) for x in r.get("diaries",[])]'
