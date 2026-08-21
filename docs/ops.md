# 운영 런북 — 배포 · TLS · 검증

호스트: 지농서버(AWS EC2 `13.125.70.226`), `ssh jinong_aws_office`(사무실, 22) / `ssh jinong_aws`(외부, 7022), 사용자 `ubuntu`.
포트: **7003**(loopback) — 7001 hatchery-serving, 7002 jinong-ai-gateway 와 나란히. 외부는 호스트 nginx(443) 로만.

## 0. 현재 상태 (2026-08-21)

- 컨테이너 `jinong-ai-agents` 배포 완료(`127.0.0.1:7003`, `.env` 채움, `/v1/upstream/health` stt/llm/s3/farmos 모두 ok). 서버 내부 스모크(`scripts/curl_flow.sh`, 실제 raw 녹음 1건) COMPLETED 확인.
- 게이트웨이 0.2.0 배포: `/v1/chat/completions` 프록시 + 이 서비스 전용 STT 키 발급(`GATEWAY_API_KEY` 2번째 키). vLLM 터널은 호스트 **8200**(8000 은 nginx 점유) — GPU 박스에서 `serve/llm-up.sh` 전까지 `llm_ok:false`/502 가 정상.
- 2026-08-20 도메인 변경(`jinong-agent` → `jinong-stt-report-generation.jinongservice.co.kr`) 서버 반영 완료:
  새 vhost 설치(HTTPS 블록 주석 상태), 구 conf 는 `sites.d/jinong-agent….conf.bak-20260820-rename` 으로 백업,
  도메인 46자 때문에 `nginx.conf` http 블록에 `server_names_hash_bucket_size 128;` 추가(백업 `nginx.conf.bak-20260820`),
  서버 `.env` `PUBLIC_BASE_URL` 갱신. 새 도메인 경유(nginx) E2E 스모크 COMPLETED 확인(2026-08-20, `e2e-newdomain-smoke`).
- 2026-08-21 공개 HTTPS 개통 완료: DNS A 레코드(13.125.70.226) 반영 확인 → `cert.sh` dry-run·실발급(서버
  `/srv/jinong-agent/deploy/letsencrypt/` 의 구도메인 스크립트를 레포 최신본으로 교체 후) → vhost HTTPS 블록
  주석 해제 설치 → `nginx -t`/reload → `/etc/hosts` 임시 매핑 제거(백업 `.bak-20260821-dnsdone`) →
  갱신 cron 등록(root crontab `57 4 * * * /srv/jinong-agent/deploy/letsencrypt/renew.sh`). 외부에서
  `https://…/healthz` ok, HTTP 는 301→HTTPS.
- LLM 기본은 Gemini(Vertex AI, `LLM_PROVIDER=gemini / LLM_MODEL=gemini-3.5-flash / GCP_PROJECT_ID=jinong-lab-llm / GCP_LOCATION=global / GEMINI_THINKING_LEVEL=low`). 인증은 서비스 계정 키 — 서버 `/home/ubuntu/dev/deploy_setting_files/jinong_vertexai_service_key.json` 을 compose 가 `/app/auth/jinong_vertexai_service_key.json` 로 read-only 마운트(`GOOGLE_APPLICATION_CREDENTIALS`). hatchery_serving 과 같은 키·프로젝트. 이전 OpenAI(`gpt-4.1`) 는 `LLM_PROVIDER=openai` + `LLM_BASE_URL/LLM_API_KEY` 로 복귀 가능.
- 동의서 §7 충족을 위해 vLLM 기동 후 `.env` 의 `LLM_PROVIDER=jinong / LLM_BASE_URL=https://jinong-stt.jinongservice.co.kr/v1 / LLM_API_KEY=<STT_API_KEY 와 동일 게이트웨이 키> / LLM_MODEL=exaone45` 로 전환 후 `docker compose up -d`.
- 자격증명 메모: AWS 키는 audio-labeler 워커와 같은 IAM 사용자(정적 키, `arn:aws:iam::996450911403:user/audio-labeler-s3` — sts get-caller-identity 로 확인 2026-08-21)를 재사용 — 전용 IAM 사용자로 분리 권장. 이 ARN 이 백엔드 녹음 버킷 정책에 허용할 principal(연동 명세 §6 '별도 전달' 값)이며, 분리 시 백엔드 버킷 정책도 새 ARN 으로 갱신 필요. Vertex SA 키·프로젝트(jinong-lab-llm)는 hatchery_serving 과 공용, OpenAI 키는 briefing_serving 과 공용.
- 2026-08-21 날짜별(멀티콜) 영농일지 `/v1/daily-diaries` 추가(커밋 `a376d8a`) — 신규 env 없음, DB 는 기동 시
  `create_all` 로 테이블 자동 생성. 같은 날 배포·스모크 완료: 통화 E2E 2건(`smoke-20260821-a/b`, raw wav) COMPLETED →
  `daily-smoke-20260821`(멀티콜) COMPLETED, keyset 커서·`INVALID_CURSOR` 422 실서버 확인. 이 과정에서 미확정 작물
  다건 산출물 코드 충돌(`unresolved-N` 픽스, 커밋 `c9a1f19`)을 발견·수정 후 재배포함.

## 1. 최초 1회

1. DNS A `jinong-stt-report-generation.jinongservice.co.kr → 13.125.70.226` (jinong-stt 때와 같은 경로로 요청).
2. 서버 디렉터리: `sudo mkdir -p /srv/jinong-agent/{logs,letsencrypt,deploy}` (deploy/letsencrypt 스크립트를 `/srv/jinong-agent/deploy/letsencrypt/` 로 복사).
3. nginx vhost: `deploy/nginx/jinong-stt-report-generation.jinongservice.co.kr.conf` → `/etc/nginx/sites.d/` (**HTTPS 블록 주석**) → `sudo nginx -t && sudo systemctl reload nginx`.
4. 인증서: `/srv/jinong-agent` 에서 `bash deploy/letsencrypt/cert.sh --dry-run` → 성공 시 `bash deploy/letsencrypt/cert.sh` → vhost HTTPS 블록 주석 해제 → reload. cron: `57 4 * * * /srv/jinong-agent/deploy/letsencrypt/renew.sh >> /srv/jinong-agent/logs/letsencrypt.log 2>&1`.
5. 원격 `.env` (`/home/ubuntu/apps/jinong_ai-agents/.env`, `.env.example` 참고): `AGENT_API_KEY`(발급해 kafka-gateway 팀 전달), `STT_API_KEY`(게이트웨이 `.env` `GATEWAY_API_KEY` 에 콤마로 추가한 이 서비스 전용 키 → 게이트웨이 `docker compose up -d`), `LLM_*`, `AWS_ACCESS_KEY_ID/SECRET`(audio_labeler 와 같은 IAM 사용자 방식), `FARMOS_BASE_URL`.

## 2. 배포

```bash
./deploy/deploy.sh                 # rsync(.env 제외) → 원격 docker compose up -d --build → :7003/healthz 폴링 → upstream health
REMOTE=jinong_aws ./deploy/deploy.sh
```
로그: `ssh jinong_aws_office 'cd apps/jinong_ai-agents && docker compose logs -f --tail=100 agent'`

## 3. 검증

```bash
curl https://jinong-stt-report-generation.jinongservice.co.kr/healthz   # status:"ok" 확인 — "degraded" 면 DB ping 실패(pending_* 는 null)
AGENT_API_KEY=… ./scripts/smoke_remote.sh                       # + upstream health (stt/llm/s3/farmos 모두 ok)
AGENT_API_KEY=… ./scripts/smoke_remote.sh jinong-agri-stt raw/<sample>.wav   # 전체 플로우 (FARM_TOKEN 주면 farmos 조회 포함)
aws s3 ls s3://jinong-agri-stt/agents/voicecall/<call_id>/ --recursive
```

날짜별(멀티콜) 영농일지 스모크 — 위 플로우로 **terminal 된 call_id** 들을 넘긴다:

```bash
AGENT_URL=https://jinong-stt-report-generation.jinongservice.co.kr AGENT_API_KEY=… FARM_TOKEN=… \
  ./scripts/daily_flow.sh <call_id_1> [call_id_2 ...]           # 트리거 → 폴링 → 작물별 일지 md 출력
aws s3 ls s3://jinong-agri-stt/agents/voicecall/daily/<diary_id>/ --recursive
```

## 4. 로컬

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate && uv pip install -r requirements-dev.txt
pytest -q
./scripts/run_local.sh                       # fake 파이프라인, 무인증, :7003
./scripts/curl_flow.sh jinong-agri-stt raw/<sample>.wav   # AWS 자격증명 + STT_API_KEY 필요
./scripts/daily_flow.sh <call_id_1> <call_id_2>           # 위 플로우로 terminal 된 call 들을 날짜별 일지로 집계
docker compose up --build                    # 컨테이너 검증(.env 필요)
```

### 4.1 로컬 파일로 E2E (S3 없이)

로컬 raw audio 파일 하나로 전체 흐름을 검증할 때 (AWS 자격증명 불필요, HTTPS 노출 불필요):

```bash
# 터미널 1 — 파일시스템 스토리지로 서버 기동 (.env 에 STT_BASE_URL/STT_API_KEY 는 필요)
STORAGE_IMPL=local ./scripts/run_local.sh                          # PIPELINE_IMPL=fake 기본 (LLM 불필요)
STORAGE_IMPL=local PIPELINE_IMPL=langgraph ./scripts/run_local.sh  # 실제 LLM 까지 (LLM_PROVIDER 설정 필요)

# 터미널 2 — 파일을 LOCAL_AUDIO_DIR 로 복사하고 bucket="local" 로 플로우 구동
./scripts/e2e_local.sh ~/Downloads/sample.m4a

# 산출물은 S3 레이아웃 그대로 로컬에 미러됨
find ./data/storage/agents/voicecall -type f
```

주의: STT(게이트웨이)·LLM 은 여전히 원격 호출이다 — 로컬 머신에서 `STT_BASE_URL` 접근이 가능해야 한다.
`STORAGE_IMPL=local` 은 로컬 개발 전용이며 배포 환경에서는 항상 `s3`.

## 5. 대안: 같은 호스트에서 게이트웨이 내부 호출

기본은 공개 HTTPS(`STT_BASE_URL=https://jinong-stt.jinongservice.co.kr`). 헤어핀을 피하려면 두 compose 를 같은 docker network 에
붙이고 `STT_BASE_URL=http://jinong-ai-gateway:8080` 로 바꾼다(게이트웨이 compose 에 `networks:` 추가 필요 — 게이트웨이 레포 변경).

## 6. 문제 해결

| 증상 | 확인 |
|---|---|
| 기동 거부 `AGENT_API_KEY is empty` | `.env` 키 채움 |
| `/healthz` 가 `status:"degraded"` | DB ping 실패 — `agent_data` 볼륨/`DB_PATH` 권한, 디스크, `docker compose logs agent` 확인 (이때 `pending_*` 는 `null`) |
| `status` 가 계속 `PROCESSING` | `GET /v1/calls/{id}` 의 `audio[].last_error`, `job_events`(sqlite `/data/agent.db`) — STT 429/5xx 재시도 중이거나 게이트웨이 다운. 1h 후 deadline 스윕이 부분 생성 |
| `FAILED/GENERATION_FAILED` | LLM 키/모델/도달성(`/v1/upstream/health` llm — gemini 는 SA 키 파일·토큰 발급·publisher model 조회), `docker compose logs agent` 의 트레이스백 → `POST …/regenerate` |
| S3 422 | 호출자 버킷 권한(IAM 사용자에 해당 버킷 read) |
| daily 트리거 `409 CALLS_NOT_READY` | 멤버 call 이 아직 terminal 아님(또는 `FAILED` 포함) — call 들이 `COMPLETED`/`EMPTY` 될 때까지 대기, `FAILED` 는 먼저 call `/regenerate` |
| daily 트리거 `422 NO_TRANSCRIBED_CALLS` / `FARM_MISMATCH` | `COMPLETED` call 0건 / call 들의 `farm.farm_id` 상이 — call_ids 구성 재확인 |
| daily 재생성에 farmos 조회가 빠짐 | 토큰은 terminal 마다 purge — `/regenerate` body 에 새 `farm_access_token` 을 매번 포함해야 함 |
| daily 산출물 확인 | `GET /v1/daily-diaries/{id}` 또는 `aws s3 ls s3://jinong-agri-stt/agents/voicecall/daily/<diary_id>/ --recursive` |
