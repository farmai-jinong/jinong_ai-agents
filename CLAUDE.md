# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**⑧ 서비스 앱 / AI Agent** of the 지농 AI system — the component map (①–⑨) is owned by
`~/dev/jinong/jinong_ai-gateway/CLAUDE.md` (SSOT). This service receives 팜스올 보이스톡(farmer↔consultant call)
events from the farmos backend (`kafka-gateway`, branch `livekit`), transcribes the recordings through the
**⑥ 게이트웨이**, and produces **작물별 영농일지 초안(markdown)** + **컨설팅 보고서 초안(markdown)** with a
LangGraph pipeline. It holds no model and no GPU. Product spec SSOT: farmos consent doc
`jinong_farmview/docs/notion-terms/voice-call-consent/02_AI_영농일지_컨설팅_보고서_작성_동의서.md` (§3/§4/§8) and
`~/Documents/영농일지_생성과정_설명서.md` (diary screen blocks, farmos diary API, pitfalls §3.7).

Deploy target: 지농서버(AWS EC2), Docker, host port **7003** (loopback) behind host nginx
`jinong-stt-report-generation.jinongservice.co.kr`. `ssh jinong_aws_office` (office, 22) / `ssh jinong_aws` (external, 7022).

## Hard rules

- **Never call GPU serving (①–⑤) directly.** STT (`/v1/audio/transcriptions`) and LLM (`/v1/chat/completions`) go
  through the gateway (`STT_BASE_URL`, `LLM_BASE_URL`). External providers — `LLM_PROVIDER=gemini` (Vertex AI
  `gemini-3.5-flash`, SA key via `GOOGLE_APPLICATION_CREDENTIALS`, current default) and `openai` — are temporary; the
  consent doc (§7 no external processing) requires switching to `LLM_PROVIDER=jinong` (gateway vLLM) once it is up.
- Secrets only via env/`.env` (never committed). Bucket names / prefixes / schemas of other repos are **referenced
  by SSOT path** in comments, never copied as truth.
- Storage is the backend-owned MinIO (`S3_ENDPOINT_URL=https://smart-minio.jinongservice.co.kr`, 2026-08 전환):
  input recordings are read from `voice-recordings` (caller bucket/key, read-only, never copied); our writes go to
  the MinIO bucket `jinong-agri-stt` under `S3_PREFIX` (`agents/voicecall/`) only — same name as, but distinct from,
  the AWS bucket (audio_labeler SSOT: `audio_labeler-web/config.prd.yaml`, stays on AWS). Key builders live only in
  `app/clients/s3.py:Keys`.
- The agent **never writes to farmos** (no `PUT /m/diary`); it only reads with the farmer JWT from the call-start
  payload and returns a `prefill` (PutDiaryDTO shape) for the farmer to confirm in the app.
- `farm_access_token` is never echoed in responses/logs; purged on terminal status.
- Docs and commit messages in Korean. No commit attribution footer.
- Single uvicorn worker (`--workers 1`): the poller/semaphores are process-local.

## Layout

```
app/main.py           create_app(): fail-closed auth, lifespan (db init, clients, worker)
app/config.py         pydantic-settings Settings (all env knobs; .env.example documents each)
app/auth.py           Bearer/X-API-Key, comma-separated multi-key
app/errors.py         install_error_handlers: {detail:{code,message}} 오류 형식
app/runtime.py        Runtime(settings, db, s3, stt, pipeline, worker) on app.state.rt
app/db/               SQLAlchemy 2 async + aiosqlite: models (calls/call_audio/artifacts/daily_diaries/daily_artifacts/job_events), repo (all SQL)
app/clients/          s3 (boto3 via to_thread + Keys), storage (Protocol + build_storage: STORAGE_IMPL=s3|local), local_storage (로컬 개발용 파일시스템), stt (gateway diarize + retry classifier), farmos (read-only), ap_backend (AP 백엔드 research API — 농가 JWT 없이 작물·품목코드 조회, read-only), llm (factory: ChatOpenAI for openai/jinong, ChatGoogleGenerativeAI(vertexai) for gemini; probe), callback
app/services/         calls (start/audio/end/regenerate transitions, idempotency), daily (날짜별 멀티콜 트리거/재생성), transcripts (merge + merge_calls + apply_speaker_map: 생성 후 농가/컨설턴트 역할 되먹임), artifacts (persist), results (views)
app/worker/           runner (poll+wake, semaphores), stt_job, generate_job, daily_job (날짜별 집계 생성), recovery (startup reset, deadline sweep)
app/routes/           health, calls (/v1/calls/*), daily (/v1/daily-diaries/* — 백엔드 트리거 날짜별 영농일지)
app/agents/           LangGraph pipeline: interface.py (contract), fake.py, graph.py + state/schemas/llm/deps, nodes/mapping/prompts/render (crop subgraph ends with `verify_diary` — an independent LLM pass that demotes a hollow draft to EMPTY), summarize.py (call summary for the backend callback — independent of the diary pipeline), tools/ (fake_farmos·fake_llm·transcript), run.py (dry-run CLI), eval.py, voice_eval/ (실녹음 평가 하네스: STT 정확도 + 영농일지 LLM judge + 회귀 게이트, optimize/ = 평가 결과로 프롬프트·매핑을 고치는 자가 개선 루프)
app/schemas/          calls (API), daily (daily-diaries API), transcript (MergedTranscript), pipeline (CallContext/PipelineResult contract)
tests/                pytest-asyncio + respx (STT/farmos) + moto (S3), FakePipeline; tests/agents/ for the pipeline, tests/agents/testcases/voice/ (대본·정답·임계값 — 녹음은 리포지토리 밖)
deploy/               deploy.sh (rsync + remote compose), nginx vhost, letsencrypt cert/renew
docs/                 api-reference.md (contract), architecture.md, ops.md (runbook), integration-briefing.md (내부), integration-handoff.md (백엔드 전달용), eval-journal.md/.jsonl (자가 개선 루프 기록), proposals/ (구조 개선 제안서 — 자동 적용 안 함)
scripts/              run_local.sh, curl_flow.sh, e2e_local.sh (로컬 파일 E2E), daily_flow.sh (날짜별 일지 스모크), smoke_remote.sh, farmos_login.py (농가 JWT 발급)
```

## Run / test / deploy

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate && uv pip install -r requirements-dev.txt
pytest -q                                   # unit + API + worker (no network)
./scripts/run_local.sh                      # ALLOW_NO_AUTH=1 PIPELINE_IMPL=fake, :7003
STORAGE_IMPL=local ./scripts/run_local.sh && ./scripts/e2e_local.sh <audio>   # S3 없이 로컬 파일 E2E (ops.md §4.1)
python -m app.agents.run --transcript tests/agents/fixtures/calls/<fixture>.json --out out/   # pipeline dry-run
python -m app.agents.voice_eval --audio-dir ~/Downloads/recordings   # 실녹음 5건 평가 → out/voice-eval/report.md (ops.md §4.2)
python -m app.agents.voice_eval.optimize --max-iters 3       # 자가 개선 루프 → docs/eval-journal.md, eval/auto-tune (ops.md §4.3)
./deploy/deploy.sh                          # to jinong_aws_office (see docs/ops.md for first-time DNS/TLS/.env)
```

## Contract pointers

- External API: `docs/api-reference.md` (start → audio(S3 ref) → end → GET; statuses NONE/PROCESSING/COMPLETED/EMPTY/FAILED).
  날짜별 멀티콜 집계 `/v1/daily-diaries` (backend-triggered, terminal call_ids → 병합 일지, report 없음) 포함.
  Backend-facing spec: `docs/integration-handoff.md` §3.7.
- Gateway STT contract: `jinong_ai-gateway/docs/api-reference.md` (diarized response = array of chunks; speaker letters per request; 429/413/415/502).
- Worker/state machine: `docs/architecture.md`.
