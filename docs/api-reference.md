# API 레퍼런스 — 지농 AI Agent (⑧)

- Base URL: `https://jinong-stt-report-generation.jinongservice.co.kr` (지농서버 내부: `http://127.0.0.1:7003`)
- 인증: `/healthz` 를 제외한 모든 요청에 `Authorization: Bearer <AGENT_API_KEY>` (또는 `X-API-Key: <KEY>`). 실패 → `401 {"detail":"invalid or missing API key"}`
- 오류 형식: `{"detail": {"code": "<CODE>", "message": "..."}}` (검증 오류는 FastAPI 기본 422)
- 시각은 ISO-8601(타임존 포함). 오디오는 **바이트가 아니라 S3 참조(bucket/key)** 로 받는다.

## 상태 어휘

| 필드 | 값 | 뜻 |
|---|---|---|
| `state` | `OPEN` → `ENDED` | 통화 수명주기(종료 이벤트 수신 여부) |
| `status` | `NONE` / `PROCESSING` / `COMPLETED` / `EMPTY` / `FAILED` | 결과 상태. 앱의 `sttStatus`/`aiSummaryStatus` 와 같은 어휘 |
| `audio[].status` | `PENDING` / `TRANSCRIBING` / `TRANSCRIBED` / `FAILED` | 녹음별 STT 상태 |
| `error.code` | `NO_AUDIO` `NO_TRANSCRIPT` `NO_CONTENT` `STT_FAILED` `GENERATION_FAILED` | terminal 사유 (`STT_TIMEOUT` 은 call 레벨이 아니라 `audio[].last_error` 에만 나타남) |

흐름: `POST /v1/calls` → `POST /v1/calls/{id}/audio` ×N (즉시 STT 큐잉) → `POST /v1/calls/{id}/end` (202, `PROCESSING`) → 모든 STT 완료 시 생성 → `GET /v1/calls/{id}` 로 `COMPLETED` 확인.

## `POST /v1/calls` — 전화 시작

```json
{
  "call_id": "20260819_Qmf1D0X",
  "started_at": "2026-08-19T10:12:00+09:00",
  "participants": [
    {"role": "farmer", "user_id": "u123", "engn_id": "18", "name": "홍길동"},
    {"role": "consultant", "user_id": "c9", "name": "김상담"}
  ],
  "farm_access_token": "eyJ…",
  "farm": {"farm_id": "f1", "farm_nm": "…"},
  "num_speakers": 2,
  "language": "ko",
  "callback_url": "https://…/agent-callback",
  "metadata": {"hints": {"prdlst_code": "0804MM", "prdlst_nm": "딸기"}}
}
```

- `call_id`: `[A-Za-z0-9_.:-]{1,128}` — kafka-gateway 의 callId 그대로.
- `participants[]`: `{role, user_id, engn_id, name}` — 농가 구분은 `engn_id`(영농체 ID)+`user_id` **복합 키**(user_id 단독 식별 금지). farmer 항목의 `engn_id`는 daily FARM_MISMATCH 검사에 사용.
- `farm_access_token`: 농가 JWT. 영농일지 작성용 farmos **읽기** 조회(`/m/diary/*`)에만 사용, 응답/로그에 절대 노출하지 않고 terminal 시 삭제. 없으면 farmos 조회 없이 전사만으로 생성.
- `metadata.hints` (선택): `prdlst_code`, `prdlst_nm`, `farmer_crops[]`, `diary_date`, `topic` — farmos 조회 실패 시 대체.
- 응답: `201` 신규 / `200` 재전송(참가자·토큰·메타 upsert; terminal 후엔 변경 없이 `note`). 본문은 `CallDetail`.

## `POST /v1/calls/{call_id}/audio` — 녹음파일 수신

```json
{"bucket": "jinong-agri-stt", "key": "voicetalk/2026/08/19/….ogg", "seq": 1,
 "recorded_at": "2026-08-19T10:12:05+09:00", "duration_sec": 183.4, "speaker_hint": null, "force": false}
```

- 서버가 `HEAD s3://bucket/key` 검증: 없음 → `422 S3_OBJECT_NOT_FOUND`, 권한 → `422 S3_ACCESS_DENIED`, `MAX_AUDIO_MB`(200) 초과 → `422 AUDIO_TOO_LARGE`, 그 밖의 S3 오류 → `422 S3_ERROR`.
- 멱등 `(call_id, bucket, key)`: 신규 `202` / 진행·완료 `200`(no-op, `note`) / FAILED 또는 `force:true` `202` 재큐.
- 통화가 아직 없으면 자동 생성(`AUDIO_AUTOCREATE_CALL`). 종료·확정된 통화에 오면 큐잉만 하고 `stale:true` (재생성 필요).
- 순서: `seq` → `recorded_at` → 도착순. 오프셋은 병합 시 앞 파일 길이 누적.
- 응답 `AudioAck`: `{call_id, audio:{id,bucket,key,seq,status,attempts,...}, call_status, call_state, stt_progress:{total,transcribed,failed,pending}, note}`

## `POST /v1/calls/{call_id}/end` — 전화 종료

Body(선택) `{"ended_at": "...", "duration_sec": 900}` → `202` (`state=ENDED, status=PROCESSING`). 이미 종료면 `200`. 미존재 `404 CALL_NOT_FOUND`. 오디오 0건이면 곧 `EMPTY/NO_AUDIO`.

## `GET /v1/calls/{call_id}` — 상태/결과 (`CallDetail`)

`?inline=false` 로 markdown/structured 본문 생략(폴링용).

```json
{
  "call_id": "…", "state": "ENDED", "status": "COMPLETED",
  "started_at": "…", "ended_at": "…", "duration_sec": 900.0, "created_at": "…", "updated_at": "…",
  "participants": [...], "farm": {...}, "metadata": {...}, "stale": false, "note": null,
  "stt_progress": {"total": 2, "transcribed": 2, "failed": 0, "pending": 0},
  "audio": [{"id": 17, "bucket": "…", "key": "…", "seq": 1, "status": "TRANSCRIBED", "attempts": 1,
             "duration_sec": 183.4, "stt_seconds": 183.0, "offset_sec": 0.0,
             "stt_raw_key": "agents/voicecall/<id>/stt/17-ab12cd34.json", "segments_count": 42, "last_error": null}],
  "generation": {"run": 1, "attempts": 1, "state": "IDLE", "started_at": "…", "finished_at": "…",
                 "model": "gemini-3.5-flash", "warnings": [], "usage": {...}},
  "error": null,
  "result": {
    "transcript_key": "agents/voicecall/<id>/transcript/merged.json",
    "speaker_map": {"f0:A": "farmer", "f0:B": "consultant"},
    "diaries": [{"prdlst_code": "0804MM", "prdlst_nm": "딸기", "diary_date": "2026-08-19", "status": "OK",
                 "markdown": "# 영농일지 — 딸기 (2026-08-19)…",
                 "structured": {"prefill": {"diaryId": null, "diaryDate": "2026-08-19", "prdlstCode": "0804MM", "...": "PutDiaryDTO"},
                                "prefill_ready": true, "mapping": {...}, "gsNm": "…", "warnings": []},
                 "s3_key_md": "agents/voicecall/<id>/artifacts/diary/0804MM.md",
                 "s3_key_json": "agents/voicecall/<id>/artifacts/diary/0804MM.json"}],
    "report": {"markdown": "# 컨설팅 보고서 …", "structured": {"summary": "…", "keywords": [], "action_items": [], "sections": {}},
               "s3_key_md": "agents/voicecall/<id>/artifacts/report.md",
               "s3_key_json": "agents/voicecall/<id>/artifacts/report.json"},
    "result_key": "agents/voicecall/<id>/artifacts/result.json"
  },
  "callback_status": null
}
```

- `result` 는 `COMPLETED` 일 때만. `diaries[]` 는 통화에서 다룬 **작물별** 1건씩(`prdlst_code` = farmos 품목코드; 확정 불가 시 `null`, S3 키는 `unresolved` — 한 결과에 미확정이 여럿이면 두 번째부터 `unresolved-2`, `unresolved-3` …). `status` ∈ `OK|PARTIAL|EMPTY|UNRESOLVED_CROP`.
- 에이전트는 farmos 에 **저장하지 않는다**. `structured.prefill` 은 앱 `PUT /m/diary` 의 `fields` 와 같은 모양의 초안 — 농가 확인 후 저장용.
- 마크다운 본문이 `RESULT_INLINE_MAX_KB` 를 넘으면 인라인 생략(S3 키만).

## 산출물 · 목록 · 재생성

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/calls/{id}/transcript` | 병합 전사 JSON(`MergedTranscript`). 미준비 `404 NOT_READY` |
| GET | `/v1/calls/{id}/artifacts/report[?format=json]` | 보고서 `text/markdown` / JSON |
| GET | `/v1/calls/{id}/artifacts/diary/{prdlst_code}[?format=json]` | 작물별 영농일지 md / JSON (`unresolved`, 다건이면 `unresolved-2` … 가능) |
| GET | `/v1/calls?status=&state=&limit=50&cursor=` | 운영용 목록 `{items:[{call_id,state,status,updated_at,stt_progress}], next_cursor}` (`limit` 1..200, 기본 50) |
| POST | `/v1/calls/{id}/regenerate` | `{"retranscribe": false, "reason": "…", "farm_access_token": "…"}` → `202`. 토큰을 주면 purge 된 농가 JWT 재공급(daily 와 동일 계약). `409 CALL_NOT_ENDED` / `409 ALREADY_PROCESSING`. 산출물 같은 S3 키에 덮어쓰기, `generation.run` +1 |
| GET | `/healthz` | 무인증 `{status: "ok"\|"degraded", version, worker:{running,pending_stt,pending_gen,pending_daily}}`. DB ping 실패 시 `status:"degraded"` 이고 `pending_*` 는 모두 `null` |
| GET | `/v1/upstream/health` | STT / LLM(openai·jinong: `/models`, gemini: Vertex publisher model 조회) / S3(head_bucket) / farmos 도달성 |

목록 커서(`/v1/calls` · `/v1/daily-diaries` 공통): 최신 생성순(`created_at DESC, id DESC`) keyset.
`next_cursor` 는 **불투명 토큰** — 그대로 `cursor=` 로 되돌리면 다음 페이지, `null` 이면 마지막.
형식이 깨진 커서는 `422 INVALID_CURSOR`.

## 날짜별 영농일지 `/v1/daily-diaries` (멀티콜 집계)

특정 날짜에 통화가 여러 건일 때, **백엔드가 call_id 목록을 지정해 명시적으로 트리거**하면 그 통화들의
전사를 합쳐 **하나의 날짜별 영농일지(작물별)** 를 생성한다. 기존 통화별 산출물과 **별도 리소스로 공존**하며
(통화별 플로우는 그대로), 산출물은 작물별 일지뿐이다(컨설팅 보고서 없음 — 보고서는 통화 단위 유지).

### `POST /v1/daily-diaries` — 트리거

```json
{"diary_id": "daily_u1_20260820", "diary_date": "2026-08-20",
 "call_ids": ["c1", "c2", "c3"],
 "farm_access_token": "eyJ…",          // 선택 — 매 트리거마다 새로 보내야 함(아래 참고)
 "callback_url": "https://…", "language": "ko",
 "metadata": {"hints": {"prdlst_code": "…"}}}
```

- **멱등성**: `diary_id` 가 멱등성 키다(형식은 call_id 와 동일 `[A-Za-z0-9_.:-]{1,128}`). 백엔드가
  농가 복합 키/날짜에서 결정적으로 만들어 보낸다 — 확정 규칙 `daily_{engnId}_{userId}_{yyyyMMdd}`
  (예: `daily_18_u123_20260819`, 2026-08-25 백엔드 확정). 재전송이 안전하다.
  신규 → `201` + 생성 큐잉. 재-POST: 진행 중 → `200 "already processing"`, terminal →
  `200 "… use regenerate"` (재생성은 `/regenerate` 로만).
- **`farm_access_token` 은 매 트리거·재생성마다 새로 보내야 한다**: 멤버 call 들의 토큰은 terminal 시
  purge 되므로 재사용할 수 없다. 생략하면 farmos 조회 없이(기존 일지 참조 없이) 전사·힌트만으로 생성한다.
  이 토큰도 daily terminal 시 purge 된다.
- **검증(동기)**: 멤버 call 은 전부 terminal `COMPLETED`/`EMPTY` 여야 한다(같은 farmer 인지는 백엔드 책임).
  | 코드 | HTTP | 조건 |
  |---|---|---|
  | `CALLS_NOT_FOUND` | 422 | 존재하지 않는 call 포함 |
  | `CALLS_NOT_READY` | 409 | `NONE`/`PROCESSING`/`FAILED` call 포함 (FAILED 는 먼저 call regenerate) |
  | `NO_TRANSCRIBED_CALLS` | 422 | `COMPLETED` call 이 하나도 없음 |
  | `FARM_MISMATCH` | 422 | call 들의 `farm.farm_id` 또는 farmer 복합 키 `(engn_id, user_id)` 가 2개 이상 서로 다름 (값이 있는 콜만 검사 — `engn_id` 없는 farmer 는 복합 키 검사에서 제외) |
- 병합 전사: 통화를 `started_at` 순으로 이어붙인다. 시간축은 각 통화 길이의 누적(통화 사이 실제 공백은
  표현하지 않음). `transcript.files[].call_id` 로 원본 통화를 식별한다.
- 산출물 날짜는 요청의 `diary_date` 로 고정된다.

### 조회·산출물·재생성

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/daily-diaries/{diary_id}[?inline=false]` | 상태/결과 (`DailyDiaryDetail` — `result.diaries` 만, report 없음) |
| GET | `/v1/daily-diaries/{diary_id}/transcript` | 병합 전사 JSON. 미준비 `404 NOT_READY` |
| GET | `/v1/daily-diaries/{diary_id}/artifacts/diary/{prdlst_code}[?format=json]` | 작물별 일지 md / JSON |
| GET | `/v1/daily-diaries?diary_date=&status=&limit=50&cursor=` | 목록 `{items:[{diary_id,diary_date,status,updated_at}], next_cursor}` (`limit` 1..200, 기본 50) |
| POST | `/v1/daily-diaries/{diary_id}/regenerate` | `{"farm_access_token": "…", "reason": "…"}` → `202`. `409 ALREADY_PROCESSING` / `404 DAILY_NOT_FOUND`. 같은 S3 키 덮어쓰기, `generation.run` +1 |

`DailyDiaryDetail` 은 `CallDetail` 보다 단순하다 — `state`/`stale`/`stt_progress`/`audio`/`participants`/`farm`/`started_at`/`duration_sec` 필드가 **없다**(STT 는 멤버 call 에서 이미 끝난 상태이므로):

```json
{
  "diary_id": "daily_u1_20260820", "diary_date": "2026-08-20", "status": "COMPLETED",
  "call_ids": ["c1", "c2", "c3"], "created_at": "…", "updated_at": "…",
  "metadata": {...}, "note": null,
  "generation": {"run": 1, "attempts": 1, "state": "IDLE", "started_at": "…", "finished_at": "…",
                 "model": "gemini-3.5-flash", "warnings": [], "usage": {...}},
  "error": null,
  "result": {"transcript_key": "agents/voicecall/daily/<diary_id>/transcript/merged.json",
             "speaker_map": {...},
             "diaries": [{"prdlst_code": "0804MM", "...": "통화별 diaries[] 와 동일 모양"}],
             "result_key": "…/artifacts/result.json"},
  "callback_status": null
}
```

목록 아이템(`items[]`)은 `{diary_id, diary_date, status, updated_at}` 뿐이다.

상태 어휘·재시도 정책은 통화와 동일(`NONE/PROCESSING/COMPLETED/EMPTY/FAILED`, 생성 실패 60s 후 재시도).
S3 산출물은 `{S3_PREFIX}/daily/{diary_id}/…` 아래에 저장된다.

### 콜백

terminal 시 (형식은 통화 콜백과 동일한 전송 규칙):

```json
{"daily_diary_id": "daily_u1_20260820", "diary_date": "2026-08-20", "status": "COMPLETED", "error": null,
 "call_ids": ["c1", "c2", "c3"],
 "result_url": "https://jinong-stt-report-generation.jinongservice.co.kr/v1/daily-diaries/daily_u1_20260820",
 "generation_run": 1}
```

## 콜백(선택)

`callback_url` 이 있고 `CALLBACK_ENABLED=true` 면 terminal 상태에서 `POST callback_url` (헤더 `X-API-Key: CALLBACK_API_KEY`, 타임아웃 10s, 최대 3회 시도 — 실패 시 10s·30s 뒤 재시도):

```json
{"call_id": "…", "status": "COMPLETED", "error": null,
 "result_url": "https://jinong-stt-report-generation.jinongservice.co.kr/v1/calls/…", "generation_run": 1}
```

## 타이밍·재시도

- STT 는 게이트웨이 동기 호출(≈10s/오디오-분). 클라이언트 타임아웃 900s, 동시 2. `429` → `Retry-After`, `5xx/timeout` → 지수 백오프(15·2ⁿ ≤ 300s), `413/415/4xx` → 즉시 실패. `STT_MAX_ATTEMPTS`(4) 후 FAILED. 일부 실패 시 나머지로 생성 + `generation.warnings`.
- 종료 후 `END_STT_DEADLINE_SEC`(1h) 넘도록 STT 미종료 → 해당 오디오 `STT_TIMEOUT` 처리 후 부분 생성.
- 생성 실패는 60s 후 1회 재시도(`GEN_MAX_ATTEMPTS`) 후 `FAILED/GENERATION_FAILED`.

## cURL

```bash
K=…; B=https://jinong-stt-report-generation.jinongservice.co.kr
curl -X POST $B/v1/calls -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"call_id":"c1","participants":[{"role":"farmer","user_id":"u1","name":"홍길동"},{"role":"consultant","user_id":"c9","name":"김상담"}],"farm_access_token":"eyJ…"}'
curl -X POST $B/v1/calls/c1/audio -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"bucket":"jinong-agri-stt","key":"voicetalk/….ogg","seq":1}'
curl -X POST $B/v1/calls/c1/end -H "Authorization: Bearer $K" -H 'Content-Type: application/json' -d '{}'
curl $B/v1/calls/c1?inline=false -H "Authorization: Bearer $K"
curl $B/v1/calls/c1/artifacts/report -H "Authorization: Bearer $K"
```
