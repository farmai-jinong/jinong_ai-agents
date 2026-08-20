# 아키텍처 — 상태 기계 · 워커 · 저장소

## 위치

지농 AI 시스템(①–⑨, SSOT: `jinong_ai-gateway/CLAUDE.md`)의 **⑧ 서비스 앱 / AI Agent**. 지농서버(AWS EC2)에서 Docker 로
돌고, STT/LLM 은 반드시 **⑥ 게이트웨이**를 경유한다(GPU 서빙 ①–⑤ 직접 호출 금지). 오디오는 호출자(kafka-gateway 보이스톡)가
S3 에 올린 뒤 bucket/key 참조만 보낸다.

```
kafka-gateway ──POST /v1/calls ──▶ ⑧ agent ──GET s3://bucket/key──▶ S3(jinong-agri-stt)
              ──POST …/audio  ──▶   │       ──POST /v1/audio/transcriptions(diarize)──▶ ⑥ gateway ─▶ GPU STT
              ──POST …/end    ──▶   │       ──LLM (gemini: Vertex AI / openai / jinong: ⑥ gateway vLLM — env 전환)
              ◀─GET /v1/calls/{id}──┘       ──GET /m/diary/* (farmos, 농가 JWT, 읽기 전용)
                                            ──PUT agents/voicecall/{call_id}/… (S3 산출물)
```

## 상태 기계

- `calls.state`: `OPEN` → `ENDED`(`/end`).
- `calls.status`: `NONE` → `PROCESSING`(`/end`) → `COMPLETED` | `EMPTY` | `FAILED`.
- `calls.gen_state`: `IDLE` → `QUEUED`(조건: ENDED ∧ 진행중 오디오 0 ∧ IDLE — `schedule_generation_if_ready`) → `RUNNING` → `IDLE`.
- `call_audio.status`: `PENDING` → `TRANSCRIBING` → `TRANSCRIBED` | `FAILED`; 재시도는 `PENDING` + `next_attempt_at`.

`schedule_generation_if_ready` 는 `/end`, 각 STT 종료, `/regenerate`, 기동 복구, deadline 스윕에서 호출된다.
그래서 `/end` 가 STT 중에 도착해도 마지막 STT 가 끝나는 순간 자연스럽게 생성이 큐잉된다.

## 워커 (`app/worker/`)

- 단일 프로세스(`--workers 1`), 이벤트루프 하나. `Worker._loop` 는 `WORKER_POLL_SEC` 마다 또는 라우트의 `wake()` 로 `run_once()`.
- `run_once`: 남은 세마포어 슬롯만큼 `claim_audio`(PENDING & due) / `claim_generation`(QUEUED & due) 를 rowcount 가드 UPDATE 로 클레임 → 태스크 spawn.
- STT job(`stt_job.run_stt`): S3 get → 게이트웨이 diarize → raw JSON S3(`stt/NN-sha8.json`) → 행 갱신(`segments_json` 경량 캐시) → 생성 스케줄 시도.
  분류: 429(Retry-After) · 5xx/timeout(백오프 min(300, 15·2ⁿ)) · 4xx 영구.
- Generation job(`generate_job.run_generate`): 정렬·오프셋 → `MergedTranscript` → S3 `transcript/merged.{json,md}` → 파이프라인
  (`app.agents.build_pipeline`, `asyncio.wait_for(GEN_TIMEOUT_SEC)`) → 산출물 S3 + `artifacts` 행 교체 → status → 토큰 purge → 콜백.
- 복구(`recovery.recover`): 기동 시 TRANSCRIBING→PENDING, RUNNING→QUEUED, ENDED 통화 재평가. 스윕(`recovery.sweep`): 종료 후 1h 미종료 오디오 → FAILED/STT_TIMEOUT.

## 저장소

- SQLite(`DB_PATH`, WAL): `calls` / `call_audio`(UNIQUE call_id,bucket,key) / `artifacts`(UNIQUE call_id,kind,prdlst_code) / `job_events`.
- S3 (`S3_BUCKET/S3_PREFIX` = `jinong-agri-stt/agents/voicecall`):
  ```
  agents/voicecall/{call_id}/call.json
  agents/voicecall/{call_id}/stt/{NN}-{sha8}.json          # 게이트웨이 raw 응답
  agents/voicecall/{call_id}/transcript/merged.json|.md
  agents/voicecall/{call_id}/artifacts/diary/{prdlst_code}.md|.json
  agents/voicecall/{call_id}/artifacts/report.md|.json
  agents/voicecall/{call_id}/artifacts/result.json
  ```
  키 규칙은 `app/clients/s3.py:Keys` 에만 있다. 입력 오디오는 복사하지 않는다.

## 파이프라인 계약

`app/agents/interface.py` — `DiaryReportPipeline.run(MergedTranscript, CallContext) -> PipelineResult`
(`app/schemas/transcript.py`, `app/schemas/pipeline.py`). `PipelineEmpty` → `EMPTY/NO_CONTENT`. `PIPELINE_IMPL=fake` 는 LLM 없이 배선 확인용.
