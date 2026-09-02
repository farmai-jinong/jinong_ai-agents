# jinong_ai-agents — 통화 기반 영농일지·컨설팅 보고서 생성 에이전트 (⑧)

팜스올 보이스톡(농가↔컨설턴트 통화) 녹음을 받아 **작물별 영농일지 초안(markdown)** 과 **컨설팅 보고서 초안(markdown)** 을
만들어 돌려주는 서비스. STT·LLM 은 ⑥ 지농 AI 게이트웨이를 경유하고, 오디오는 S3 참조(bucket/key)로만 받는다.

## 흐름

1. `POST /v1/calls` — 전화 시작(call_id, 참가자, 농가 JWT)
2. `POST /v1/calls/{call_id}/audio` — 녹음파일 수신(S3 bucket/key, 통화당 여러 개) → 즉시 게이트웨이 화자분리 STT
3. `POST /v1/calls/{call_id}/end` — 전화 종료 → 202, 전사가 모두 모이면 백그라운드 생성
4. `GET /v1/calls/{call_id}` — 상태(`PROCESSING|COMPLETED|EMPTY|FAILED`) 와 결과: 작물별 `{prdlst_code, prdlst_nm, markdown, structured(prefill), s3_key}` + 보고서
5. (선택) `POST /v1/daily-diaries` — 하루 통화가 여러 건이면 terminal 된 call_id 목록으로 **날짜별 영농일지**(멀티콜 병합, 일지만·보고서 없음)를 트리거 → `GET /v1/daily-diaries/{diary_id}`

자세한 계약은 `docs/api-reference.md`, 구조는 `docs/architecture.md`, 배포는 `docs/ops.md`.

## 로컬 실행

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -r requirements-dev.txt
pytest -q
./scripts/run_local.sh                    # 무인증 + fake 파이프라인, http://127.0.0.1:7003
cp .env.example .env                      # 실제 STT/LLM/S3 를 쓰려면 채운다
PIPELINE_IMPL=langgraph ./scripts/run_local.sh
./scripts/curl_flow.sh jinong-agri-stt raw/<sample>.wav

# S3 없이 로컬 raw audio 파일로 E2E (자세히: docs/ops.md §4.1)
STORAGE_IMPL=local ./scripts/run_local.sh
./scripts/e2e_local.sh <audio-file>
```

## 배포 (지농서버, 포트 7003, nginx TLS `jinong-stt-report-generation.jinongservice.co.kr`)

```bash
./deploy/deploy.sh          # prod: rsync + 원격 docker compose up -d --build + health 확인
./deploy/deploy.sh dev      # dev : 같은 호스트 apps/jinong_ai-agents-dev, 포트 7013, jinong-stt-report-generation-dev.jinongservice.co.kr
```
최초 1회(DNS·nginx vhost·인증서·`.env`)는 `docs/ops.md`(dev 는 §7).

브랜치: 기본 작업 브랜치는 **`dev`**(→ dev 인스턴스). `prod` 는 7003 운영 인스턴스에 올라간 것(`dev` → `prod` 머지로 승격, prod 배포는 `prod` 에서만). `main` 은 안정 이력.

## 설계 요점

- 상태 어휘는 앱(`sttStatus`)과 동일: `NONE / PROCESSING / COMPLETED / EMPTY / FAILED`. 같은 `call_id`·같은 오디오 재전송은 멱등.
- LLM 은 env 로 provider 전환: `gemini`(Vertex AI `gemini-3.5-flash`, 서비스 계정 키 — 현재 기본) / `openai` / `jinong`(게이트웨이 vLLM, EXAONE). gemini·openai 는 외부 처리라 동의서 §7(외부 위탁 없음) 충족은 게이트웨이(jinong) 전환이 최종 목표.
- 에이전트는 farmos 에 쓰지 않는다. 영농일지는 앱의 5개 블록(주요 농작업·기타 기록사항·병해충·방제이력·사진)에 맞춰 markdown + `PutDiaryDTO` 형태 prefill 로 돌려주고, 농가가 앱에서 확인 후 저장한다. 마크다운의 `##` 섹션 집합은 항상 고정이며(빈 섹션은 `언급 없음` 한 줄), 맨 위 `> 📝 통화 요약 / > 💬 격려` 인용 블록은 섹션이 아니라 읽는 사람용 머리말이라 prefill 에 들어가지 않는다.
- 날짜별 일지는 별도 리소스(`/v1/daily-diaries`, `diary_id` 멱등 키)로, 백엔드가 명시적으로 트리거한다. 통화 전사를 시간순으로 병합해 같은 파이프라인을 1회 실행하고 보고서 노드는 스킵한다.
