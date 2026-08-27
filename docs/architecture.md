# 아키텍처 — 상태 기계 · 워커 · 저장소

## 위치

지농 AI 시스템(①–⑨, SSOT: `jinong_ai-gateway/CLAUDE.md`)의 **⑧ 서비스 앱 / AI Agent**. 지농서버(AWS EC2)에서 Docker 로
돌고, STT/LLM 은 반드시 **⑥ 게이트웨이**를 경유한다(GPU 서빙 ①–⑤ 직접 호출 금지). 오디오는 호출자(kafka-gateway 보이스톡)가
S3 에 올린 뒤 bucket/key 참조만 보낸다.

```
kafka-gateway ──POST /v1/calls ──▶ ⑧ agent ──GET s3://bucket/key──▶ S3(jinong-agri-stt)
              ──POST …/audio  ──▶   │       ──POST /v1/audio/transcriptions(diarize)──▶ ⑥ gateway ─▶ GPU STT
              ──POST …/end    ──▶   │       ──LLM (gemini: Vertex AI / openai / jinong: ⑥ gateway vLLM — env 전환)
              ──POST /v1/daily-diaries ▶│   ──GET /m/diary/* (farmos, 농가 JWT, 읽기 전용)
                                        │   ──GET /voicetalk/public/research/* (AP 백엔드, X-API-Key, 읽기 전용)
              ◀─GET /v1/calls/{id}──┘       ──PUT agents/voicecall/{call_id}/… · …/daily/{diary_id}/… (S3 산출물)
```

## 상태 기계

- `calls.state`: `OPEN` → `ENDED`(`/end`).
- `calls.status`: `NONE` → `PROCESSING`(`/end`) → `COMPLETED` | `EMPTY` | `FAILED`.
- `calls.gen_state`: `IDLE` → `QUEUED`(조건: ENDED ∧ 진행중 오디오 0 ∧ IDLE — `schedule_generation_if_ready`) → `RUNNING` → `IDLE`.
- `call_audio.status`: `PENDING` → `TRANSCRIBING` → `TRANSCRIBED` | `FAILED`; 재시도는 `PENDING` + `next_attempt_at`.

`schedule_generation_if_ready` 는 `/end`, 각 STT 종료, `/regenerate`, 기동 복구, deadline 스윕에서 호출된다.
그래서 `/end` 가 STT 중에 도착해도 마지막 STT 가 끝나는 순간 자연스럽게 생성이 큐잉된다.

날짜별(멀티콜) 일지 `daily_diaries` 는 통화와 같은 어휘를 쓰되 `state` 가 없다(트리거 즉시 시작):

- `daily_diaries.status`: `POST /v1/daily-diaries` 즉시 `PROCESSING` → `COMPLETED` | `EMPTY`(`NO_TRANSCRIPT`/`NO_CONTENT`) | `FAILED`(`GENERATION_FAILED`).
- `daily_diaries.gen_state`: `IDLE` → `QUEUED`(트리거/`/regenerate`) → `RUNNING` → `IDLE`. STT 단계가 없으므로
  큐잉 조건은 동기 검증(멤버 call 전부 terminal ∧ ≥1 COMPLETED ∧ 단일 farm — `services/daily.py`)뿐이다.
- terminal 시 `farm_access_token` purge 는 통화와 동일. 재생성은 `gen_state == IDLE` 일 때만(409 `ALREADY_PROCESSING`).

## 워커 (`app/worker/`)

- 단일 프로세스(`--workers 1`), 이벤트루프 하나. `Worker._loop` 는 `WORKER_POLL_SEC` 마다 또는 라우트의 `wake()` 로 `run_once()`.
- `run_once`: 남은 세마포어 슬롯만큼 `claim_audio`(PENDING & due) / `claim_generation`(QUEUED & due) 를 rowcount 가드 UPDATE 로 클레임 → 태스크 spawn.
- STT job(`stt_job.run_stt`): S3 get → 게이트웨이 diarize → raw JSON S3(`stt/NN-sha8.json`) → 행 갱신(`segments_json` 경량 캐시) → 생성 스케줄 시도.
  분류: 429(Retry-After) · 5xx/timeout(백오프 min(300, 15·2ⁿ)) · 4xx 영구.
- Generation job(`generate_job.run_generate`): 정렬·오프셋 → `MergedTranscript` → S3 `transcript/merged.{json,md}` → 파이프라인
  (`app.agents.build_pipeline`, `asyncio.wait_for(GEN_TIMEOUT_SEC)`) → **통화 단순요약**(아래) → 산출물 S3 + `artifacts` 행 교체 → status → 토큰 purge → 콜백.
- **통화 단순요약(`agents/summarize.py`)** — 일지 파이프라인과 분리된 LLM 패스(`build_summarizer`, 같은 `PIPELINE_IMPL` 스위치).
  일지가 실질 내용을 가질 때만(`has_diary_content`) 녹취문을 다시 읽어 주제/조치/후속 불릿을 만든다. 긴 통화는
  `chunk_turns` 로 구간 요약 후 통합 1회. 산출물은 `artifacts/summary.md|.json` + `result.summary`, 그리고 백엔드
  통화요약 콜백의 `content` 가 된다. 실패는 fail-open — 보고서 요약으로 폴백하고 warning 만 남긴다.
- Daily job(`daily_job.run_daily_generate`): 세 번째 잡. 멤버 call 들의 TRANSCRIBED 오디오를 `merge_calls`
  (`services/transcripts.py` — started_at 순 이어붙임, file_index/offset 재베이스, speaker_key 전역 재부여) 로 합쳐
  같은 파이프라인을 1회 실행. `result.report` 는 버린다(daily 는 일지만). 생성 세마포어를 call 생성과 **공유**하며
  call 클레임이 우선(`runner.py`: `daily_room = gen_room - len(call_ids)` — call 생성이 포화면 daily 는 대기).
  실패 시 60s 백오프 재큐(`generation_run` 은 성공 실행에만 증가 — 실패 시 롤백), `GEN_MAX_ATTEMPTS` 소진 → `FAILED/GENERATION_FAILED`.
- 복구(`recovery.recover`): 기동 시 TRANSCRIBING→PENDING, RUNNING→QUEUED(call·daily 각각 — `daily_reset`), ENDED 통화 재평가. 스윕(`recovery.sweep`): 종료 후 1h 미종료 오디오 → FAILED/STT_TIMEOUT.

## 저장소

- SQLite(`DB_PATH`, WAL): `calls` / `call_audio`(UNIQUE call_id,bucket,key) / `artifacts`(UNIQUE call_id,kind,prdlst_code) / `job_events`
  / `daily_diaries`(PK diary_id, call_ids_json 순서 보존) / `daily_artifacts`(UNIQUE diary_id,kind,prdlst_code — run 마다 전량 교체, 미버저닝).
- S3 (`S3_BUCKET/S3_PREFIX` = `jinong-agri-stt/agents/voicecall`):
  ```
  agents/voicecall/{call_id}/call.json
  agents/voicecall/{call_id}/stt/{NN}-{sha8}.json          # 게이트웨이 raw 응답
  agents/voicecall/{call_id}/transcript/merged.json|.md
  agents/voicecall/{call_id}/artifacts/diary/{prdlst_code}.md|.json
  agents/voicecall/{call_id}/artifacts/report.md|.json
  agents/voicecall/{call_id}/artifacts/summary.md|.json     # 통화 단순요약(콜백 content)
  agents/voicecall/{call_id}/artifacts/result.json
  agents/voicecall/daily/{diary_id}/daily.json             # 트리거 스냅샷
  agents/voicecall/daily/{diary_id}/transcript/merged.json|.md
  agents/voicecall/daily/{diary_id}/artifacts/diary/{prdlst_code}.md|.json
  agents/voicecall/daily/{diary_id}/artifacts/result.json  # report 없음
  ```
  키 규칙은 `app/clients/s3.py:Keys` 에만 있다. daily 는 `daily/` 하위로 분리해 call_id 네임스페이스와
  충돌하지 않는다. 입력 오디오는 복사하지 않는다.

## 조회 표면 (요약)

- 목록(`GET /v1/calls` · `GET /v1/daily-diaries`)은 `created_at DESC, id DESC` keyset 커서 페이지네이션
  (`next_cursor` 불투명 토큰, `repo.make_list_cursor`; 형식 불량은 `422 INVALID_CURSOR`).
- `GET /healthz` 는 DB ping 결과로 `ok`/`degraded` 를 판정하고 `worker.pending_stt/gen/daily` 를 노출한다
  (`degraded` 면 `pending_*` 는 `null` — `routes/health.py`).

## 파이프라인 계약

`app/agents/interface.py` — `DiaryReportPipeline.run(MergedTranscript, CallContext) -> PipelineResult`
(`app/schemas/transcript.py`, `app/schemas/pipeline.py`). `PipelineEmpty` → `EMPTY/NO_CONTENT`. `PIPELINE_IMPL=fake` 는 LLM 없이 배선 확인용.

그래프: `prepare_transcript → {load_farm_context ‖ assign_speaker_roles} → extract_facts → select_crops
→ Send(build_crop_diary)×작물 ‖ build_report → finalize`.
작물 서브그래프: `fetch_refs → map_facts → [disambiguate] → write_content → render_diary → verify_diary`.

**일지 검수(`nodes/crop_diary/verify_diary.py`)** — `render_diary` 의 규칙 판정(`CropFacts.is_empty()`)은
"추출된 사실이 하나라도 있는가"만 보므로, 잡담에서 사실 1건이 잘못 뽑히면 모든 칸이 `언급 없음` 인 빈
템플릿이 `OK` 로 나간다. 검수 노드는 **렌더된 초안을 독립 LLM 패스로 다시 읽고**(작물당 1콜,
`VERIFY_DIARY_ENABLED`) 실질 내용이 없으면 `EMPTY` 로 **강등만** 한다(승격 없음, 애매하면 유지).
강등 시 `diary_empty.md.j2` 로 재렌더하고 `prefill` 을 회수한다. 판정은 `structured.verify` 에 남는다.
LLM 실패는 fail-open(규칙 판정 유지 + warning). `VERIFY_DIARY_MIN_CONFIDENCE` 미만 확신이면 강등하지 않는다.

`LangGraphPipeline.run` 의 EMPTY 롤업: 전 작물이 `EMPTY|UNRESOLVED_CROP` 이고 보고서도 비면 `PipelineEmpty`.
단 추출/보고서/검수 노드가 실패한 경우(degraded)는 제외한다 — 노드 장애를 "내용 없음" 으로 오보하지 않기 위함.

daily 는 그래프 변경 없이 같은 계약을 쓴다: `daily_job.build_daily_context` 가 **합성 CallContext**
(`call_id = diary_id`) 를 만들고, ① `hints.diary_date` 를 요청 날짜로 고정(노드의 날짜 결정 최우선 소비),
② `metadata["daily"] = {diary_date, call_ids, call_count}` 플래그를 심는다 — report 노드는 이 플래그를 보고
LLM 호출 없이 스킵한다(`nodes/report.py`). 병합 전사의 시간축은 통화 길이 누적이며 통화 간 실제 공백은
표현되지 않는다(`files[].call_id` 로 원본 통화 식별).
