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
  by SSOT path** in comments, never copied as truth (S3 bucket SSOT: `audio_labeler-web/config.prd.yaml`).
- Our S3 writes stay under `S3_PREFIX` (`agents/voicecall/`); input audio is read from the caller's bucket/key and
  never copied. Key builders live only in `app/clients/s3.py:Keys`.
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
app/runtime.py        Runtime(settings, db, s3, stt, pipeline, worker) on app.state.rt
app/db/               SQLAlchemy 2 async + aiosqlite: models (calls/call_audio/artifacts/job_events), repo (all SQL)
app/clients/          s3 (boto3 via to_thread + Keys), storage (Protocol + build_storage: STORAGE_IMPL=s3|local), local_storage (로컬 개발용 파일시스템), stt (gateway diarize + retry classifier), farmos (read-only), llm (factory: ChatOpenAI for openai/jinong, ChatGoogleGenerativeAI(vertexai) for gemini; probe), callback
app/services/         calls (start/audio/end/regenerate transitions, idempotency), transcripts (merge), artifacts (persist), results (views)
app/worker/           runner (poll+wake, semaphores), stt_job, generate_job, recovery (startup reset, deadline sweep)
app/routes/           health, calls (/v1/calls/*)
app/agents/           LangGraph pipeline: interface.py (contract), fake.py, graph.py + nodes/mapping/prompts/render, run.py (dry-run CLI)
app/schemas/          calls (API), transcript (MergedTranscript), pipeline (CallContext/PipelineResult contract)
tests/                pytest-asyncio + respx (STT/farmos) + moto (S3), FakePipeline; tests/agents/ for the pipeline
deploy/               deploy.sh (rsync + remote compose), nginx vhost, letsencrypt cert/renew
docs/                 api-reference.md (contract), architecture.md, ops.md (runbook)
scripts/              run_local.sh, curl_flow.sh, smoke_remote.sh
```

## Run / test / deploy

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate && uv pip install -r requirements-dev.txt
pytest -q                                   # unit + API + worker (no network)
./scripts/run_local.sh                      # ALLOW_NO_AUTH=1 PIPELINE_IMPL=fake, :7003
STORAGE_IMPL=local ./scripts/run_local.sh && ./scripts/e2e_local.sh <audio>   # S3 없이 로컬 파일 E2E (ops.md §4.1)
python -m app.agents.run --transcript tests/agents/fixtures/calls/<fixture>.json --out out/   # pipeline dry-run
./deploy/deploy.sh                          # to jinong_aws_office (see docs/ops.md for first-time DNS/TLS/.env)
```

## Contract pointers

- External API: `docs/api-reference.md` (start → audio(S3 ref) → end → GET; statuses NONE/PROCESSING/COMPLETED/EMPTY/FAILED).
- Gateway STT contract: `jinong_ai-gateway/docs/api-reference.md` (diarized response = array of chunks; speaker letters per request; 429/413/415/502).
- Worker/state machine: `docs/architecture.md`.
