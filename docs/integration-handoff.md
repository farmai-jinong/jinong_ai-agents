# 지농 AI Agent 연동 명세 (백엔드 전달용)

> 팜스올 보이스톡 백엔드(kafka-gateway) → 지농 AI Agent(⑧) 연동 문서.
> 통화 이벤트를 보내주시면, 통화 녹음을 전사해 **작물별 영농일지 초안 + 컨설팅 보고서 초안**을 생성해 돌려드립니다.
> 하루에 통화가 여러 건이면, 통화들이 끝난 뒤 **날짜별 영농일지**(여러 통화를 합쳐 하나의 일지)를 별도로
> 트리거하실 수 있습니다(§3.7).

## 1. 접속 정보 · 인증

| 항목 | 값 |
|---|---|
| Base URL | `https://jinong-stt-report-generation.jinongservice.co.kr` |
| 인증 | 모든 요청에 `Authorization: Bearer <AGENT_API_KEY>` 또는 `X-API-Key: <AGENT_API_KEY>` (`GET /healthz`만 무인증) |
| API 키 | 저희가 발급해 전달 (클라이언트별 발급/폐기 가능) |
| Content-Type | `application/json` — 요청 본문 최대 4MB |
| 오디오 전송 | **바이트를 보내지 않습니다.** S3에 업로드 후 `bucket`/`key` 참조만 전달 |
| 시각 형식 | ISO-8601, 타임존 포함 권장 (없으면 UTC로 해석) |

인증 실패: `401 {"detail": "invalid or missing API key"}` — 이때 `detail`은 **문자열**입니다.
도메인 에러는 `{"detail": {"code": "<CODE>", "message": "..."}}` **객체**, 요청 검증 실패는 FastAPI 기본 `422`
리스트입니다. 에러 파서가 세 가지 모양을 모두 처리해야 합니다. 저희 API는 429(rate limit)를 반환하지 않습니다.

## 2. 연동 시퀀스 (권장 흐름)

```
(1) POST /v1/calls                      통화 시작 — call_id, 참가자, 농가 JWT, callback_url
(2) POST /v1/calls/{id}/audio  ×N       녹음 생길 때마다 — S3 bucket/key 참조 (즉시 STT 큐잉)
(3) POST /v1/calls/{id}/end             통화 종료 — STT 진행 중이어도 즉시 보내도 안전
(4) 콜백 수신  또는  GET /v1/calls/{id}?inline=false 를 5초 간격 폴링
    → status 가 COMPLETED / EMPTY / FAILED 가 되면 종료
(5) GET /v1/calls/{id} 로 결과(result) 조회, 필요 시 artifact 엔드포인트로 md/json 직접 조회
```

- 모든 이벤트는 즉시 응답(200/201/202)하고 처리는 백그라운드로 진행됩니다.
- **이벤트 순서가 꼬여도 전송을 포기하지 마세요.** `audio`가 `start`보다 먼저 도착하면 통화를 자동
  생성합니다(404 아님). 다만 그 통화에는 참가자·JWT가 없으므로 `start` 이벤트도 반드시 보내주세요.

**날짜별 영농일지 (선택 — 하루 통화가 여러 건일 때)**:

```
(6) 그날의 통화들이 모두 terminal(COMPLETED/EMPTY)이 된 뒤
    POST /v1/daily-diaries              diary_id + diary_date + call_ids[] + 농가 JWT
(7) 콜백 수신  또는  GET /v1/daily-diaries/{diary_id}?inline=false 폴링
(8) GET /v1/daily-diaries/{diary_id} 로 작물별 일지 조회
```

- 트리거 주체는 백엔드입니다(예: 하루 마감 배치, 또는 마지막 통화 terminal 콜백 수신 시).
- 통화별 산출물과 **별도 리소스로 공존**합니다 — 통화별 플로우(1)~(5)는 그대로 유지됩니다.

## 3. 엔드포인트 명세

### 3.1 `POST /v1/calls` — 통화 시작

```json
{
  "call_id": "20260819_Qmf1D0X",
  "started_at": "2026-08-19T10:12:00+09:00",
  "participants": [
    {"role": "farmer", "user_id": "u123", "name": "홍길동"},
    {"role": "consultant", "user_id": "c9", "name": "김상담"}
  ],
  "farm_access_token": "<농가 JWT>",
  "farm": {"farm_id": "f1", "farm_nm": "..."},
  "num_speakers": 2,
  "language": "ko",
  "callback_url": "https://<backend>/agent-callback",
  "metadata": {"hints": {"prdlst_code": "0804MM", "prdlst_nm": "딸기"}}
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `call_id` | string | **필수** | `[A-Za-z0-9_.:-]{1,128}`. kafka-gateway의 `callId` 그대로 사용 — 저희 쪽 기본키 |
| `started_at` | datetime | 선택 | 통화 시작 시각 |
| `participants` | array(≤4) | 선택 | `{role, user_id, name}`. `role`은 `"farmer"` 또는 `"consultant"`만 허용(그 외 422). 화자 수 추정에도 사용 |
| `farm_access_token` | string | 선택 | **농가 JWT.** farmos 영농일지 관련 **읽기 조회에만** 사용. 응답·로그에 절대 노출되지 않고 terminal 시 삭제. 없으면 farmos 조회 없이 전사+힌트만으로 생성 |
| `farm` | object | 선택 | 자유 형식(`farm_id`, `farm_nm` 등). 응답에 그대로 반환 |
| `num_speakers` | int 1..8 | 선택 | STT 화자 분리 힌트 |
| `language` | string | 선택 | 기본 `"ko"` |
| `callback_url` | string | 선택 | terminal 알림을 받을 URL — **콜백 등록은 이 필드로, 통화별로** 합니다 (§5) |
| `metadata` | object | 선택 | 자유 형식. `metadata.hints`는 특별 취급(아래) |

`metadata.hints` (선택 — farmos 조회가 없거나 실패할 때 대체 사용):
`prdlst_code`, `prdlst_nm`, `farmer_crops[]`(`[{prdlstCode, prdlstNm, reprsntPrdlstCnt}]`), `diary_date`(`yyyy-MM-dd`), `topic`.

**응답**: `201`(신규) / `200`(재전송) — 본문은 `CallDetail`(§3.5).

**재전송(업서트) 규칙**:
- 같은 `call_id`로 다시 보내면 보낸 필드만 갱신됩니다(부분 업서트). **JWT가 만료되면 새
  `farm_access_token`으로 이 엔드포인트를 다시 호출해 갱신할 수 있습니다.**
- 단, 통화가 이미 terminal(`COMPLETED|EMPTY|FAILED`)이면 **아무것도 변경하지 않고** `200` +
  `note: "call already finalized"`를 반환합니다. terminal 후 토큰을 갱신하려면 §3.4의 순서를 따르세요.

### 3.2 `POST /v1/calls/{call_id}/audio` — 녹음 수신

```json
{
  "bucket": "<녹음이 올라간 버킷>",
  "key": "voicetalk/2026/08/19/xxx.ogg",
  "seq": 1,
  "recorded_at": "2026-08-19T10:12:05+09:00",
  "duration_sec": 183.4,
  "speaker_hint": null,
  "force": false
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `bucket` | string 3..128 | **필수** | 녹음이 있는 S3 버킷 (백엔드 소유) |
| `key` | string 1..1024 | **필수** | S3 객체 키 |
| `seq` | int | 선택 | 통화 내 순번 — 전사 병합 순서 1순위 |
| `recorded_at` | datetime | 선택 | 병합 순서 2순위 (없으면 도착순) |
| `duration_sec` | float ≥0 | 선택 | 병합 시 시간 오프셋 계산에 사용 |
| `speaker_hint` | string | 선택 | 전사 메타데이터로 전달 |
| `force` | bool | 선택 | `true`면 이미 전사 완료된 파일도 재전사 큐잉 |

수신 즉시 `HEAD s3://bucket/key`로 검증합니다:

| 상황 | 응답 |
|---|---|
| 객체 없음 | `422 S3_OBJECT_NOT_FOUND` |
| 저희 IAM에 읽기 권한 없음 | `422 S3_ACCESS_DENIED` — §6의 권한 부여 필요 |
| 200MB 초과 | `422 AUDIO_TOO_LARGE` |

**멱등성**: 키는 `(call_id, bucket, key)`. 같은 참조를 다시 보내도 안전합니다.

| 기존 상태 | HTTP | `note` | 효과 |
|---|---|---|---|
| 신규 | `202` | – | 등록 + STT 즉시 큐잉 |
| 대기/전사 중 | `200` | `already queued` | 메타(`seq` 등)만 갱신, 재큐 없음 |
| 전사 완료, `force=false` | `200` | `already transcribed` | no-op |
| 실패했거나 `force=true` | `202` | `re-queued` | 재시도 카운터 리셋 후 재큐 |

- 통화가 아직 없으면 자동 생성됩니다(404 아님).
- 통화가 이미 terminal인데 새/재큐 오디오가 오면 큐잉은 하되 `stale: true`로 표시만 합니다 —
  **결과에 반영하려면 `/regenerate`(§3.4)를 호출해야 합니다.**
- 응답 `AudioAck`: `{call_id, audio: {id, bucket, key, seq, status, attempts, ...}, call_status, call_state, stt_progress: {total, transcribed, failed, pending}, note}`

### 3.3 `POST /v1/calls/{call_id}/end` — 통화 종료

Body 선택: `{"ended_at": "...", "duration_sec": 900}` — 빈 body(`{}`)도 됩니다.

- `202` — `state=ENDED`, `status=PROCESSING`. **STT가 진행 중이어도 즉시 보내세요** — 마지막 STT가
  끝나는 순간 생성이 자동 시작됩니다.
- `200` — 이미 종료된 통화(`note: "already ended"`).
- `404 CALL_NOT_FOUND` — 미존재 통화.
- 오디오가 0건이면 곧 `status=EMPTY`, `error.code=NO_AUDIO`로 종결됩니다.

### 3.4 `POST /v1/calls/{call_id}/regenerate` — 재생성

Body 선택: `{"retranscribe": false, "reason": "...", "farm_access_token": "<새 JWT>"}`.

- `202` — 재생성 큐잉. 산출물은 **같은 S3 키에 덮어쓰기**되고 `generation.run`이 +1 됩니다.
- `retranscribe: true` — 모든 오디오를 STT부터 다시 수행.
- `409 CALL_NOT_ENDED` — 아직 `/end` 전. / `409 ALREADY_PROCESSING` — 이미 생성/전사 진행 중.

**⚠️ 농가 JWT 재공급**: terminal 시 JWT는 저희 DB에서 삭제됩니다. 재생성에 farmos 조회를 포함하려면
**이 body에 새 `farm_access_token`을 함께 보내주세요** (§3.7 daily 재생성과 동일한 계약). 생략하면
전사+힌트만으로 재생성됩니다(실패는 아님). terminal 상태에선 `POST /v1/calls` 업서트가 무시되므로
토큰 재공급은 이 방법뿐입니다.

### 3.5 `GET /v1/calls/{call_id}` — 상태/결과 조회

쿼리: `inline` (기본 `true`). **폴링에는 `?inline=false`를 사용하세요** — markdown/structured 본문이
생략되어 응답이 가볍습니다.

응답 `CallDetail` (주요 필드):

```json
{
  "call_id": "smoke-20260821-33min", "state": "ENDED", "status": "COMPLETED",
  "started_at": "2026-08-21T07:15:18Z", "ended_at": "2026-08-21T07:15:18.836848Z",
  "duration_sec": null, "created_at": "2026-08-21T07:15:18.534627Z", "updated_at": "2026-08-21T07:21:57.594384Z",
  "participants": [{"role": "farmer", "user_id": "farmer1", "name": "농가"},
                   {"role": "consultant", "user_id": "cons1", "name": "컨설턴트"}],
  "farm": null, "metadata": {"hints": {"prdlst_code": "", "prdlst_nm": ""}},
  "stale": false, "note": null,
  "stt_progress": {"total": 1, "transcribed": 1, "failed": 0, "pending": 0},
  "audio": [{"id": 7, "bucket": "jinong-agri-stt", "key": "raw/9c1024d3-….wav", "seq": 1,
             "recorded_at": null, "status": "TRANSCRIBED", "attempts": 1,
             "duration_sec": null, "stt_seconds": 2009.0, "offset_sec": 0.0,
             "stt_raw_key": "agents/voicecall/smoke-20260821-33min/stt/07-18afe26c.json",
             "segments_count": 738, "last_error": null}],
  "generation": {"run": 1, "attempts": 1, "state": "IDLE",
                 "started_at": "2026-08-21T07:21:38Z", "finished_at": "2026-08-21T07:21:57Z",
                 "model": "gemini-3.5-flash",
                 "warnings": ["farmos 미사용(토큰 없음) — 힌트/전사만으로 생성",
                              "작물 미특정 항목이 있어 대표 작물에 배정"],
                 "usage": {"calls": 4, "prompt_tokens": 25831, "completion_tokens": 3109,
                           "total_tokens": 28940, "by_call": ["…speaker_roles/extract/report/diary 호출별 상세…"]}},
  "error": null,
  "result": {
    "transcript_key": "agents/voicecall/smoke-20260821-33min/transcript/merged.json",
    "speaker_map": {"f0:A": "consultant", "f0:B": "farmer"},
    "diaries": [{
      "prdlst_code": null, "prdlst_nm": "마늘", "diary_date": "2026-08-21", "status": "PARTIAL",
      "markdown": "# 영농일지 — 마늘 (2026-08-21)\n\n| 항목 | 값 |\n…(총 1,791자)",
      "structured": {
        "prdlst_code": null, "prdlst_nm": "마늘", "diary_date": "2026-08-21", "status": "PARTIAL",
        "schema_version": "1",
        "prefill": null, "prefill_ready": false,
        "mapping": {"farmworks": [],
                    "pests": [{"item_id": "pest0", "family": "pest", "source": "노균병",
                               "status": "no_refs", "code": null, "name": null,
                               "evidence": [397], "needs_verification": false,
                               "payload": {"step_index": 1, "kind": "병", "status": "발생"},
                               "warnings": ["발생 정도 언급 없음 — 단계 확인 필요"]}],
                    "products": []},
        "gsNm": null, "growingSeasonStartDe": null, "existing_diary_id": null,
        "content": "[AI 초안·통화 기반]\n- 비 오기 전에 살충제 및 살균제 입제 농약(확인 필요) 살포 계획임.\n…(총 215자)",
        "warnings": ["farmos 표준 코드 매핑 없이 생성됨(prefill 불가)",
                     "노균병: 발생 정도 언급 없음 — 단계 확인 필요"],
        "evidence": [3, 5, 7, 9, 102, 103, 104, 234, 240, 371, 397, 399]
      },
      "s3_key_md": "agents/voicecall/smoke-20260821-33min/artifacts/diary/unresolved.md",
      "s3_key_json": "agents/voicecall/smoke-20260821-33min/artifacts/diary/unresolved.json"
    }, {
      "prdlst_code": null, "prdlst_nm": "양파", "diary_date": "2026-08-21", "status": "EMPTY",
      "markdown": "…(언급만 되고 기록할 내용이 없는 작물 — 빈 골격 450자)",
      "structured": {"…": "위와 동일 모양, mapping/evidence 비어 있음"},
      "s3_key_md": "agents/voicecall/smoke-20260821-33min/artifacts/diary/unresolved-2.md",
      "s3_key_json": "agents/voicecall/smoke-20260821-33min/artifacts/diary/unresolved-2.json"
    }],
    "report": {
      "markdown": "# 컨설팅 보고서 — 2026-08-21 …(총 4,313자)",
      "structured": {
        "summary": "마늘과 양파를 재배하는 대농가에서 입제 농약의 살포 시기, 칼슘제 및 미생물 제제와의 혼용에 따른 약효 저하 문제를 문의하여 원칙적인 관리 방안을 안내함",
        "keywords": ["마늘", "양파", "입제농약", "살충제", "살균제", "칼슘제", "혼용", "관주처리"],
        "action_items": [],
        "sections": {
          "farm_status": [{"text": "경남 창녕 지역에서 마늘과 양파를 재배함", "evidence": [15, 23, 24, 151], "needs_verification": false}, "…"],
          "issues": [{"text": "입제 농약(살충제, 살균제)을 비 오기 3~4일 전에 미리 살포해도 …", "evidence": [3, 5, 7, 31, 32], "needs_verification": true}, "…"],
          "advice": [{"text": "[병해충관리] 입제 농약은 비 오기 2~3일 전에 살포하고 비를 흠뻑 맞추면 …", "evidence": [12, 33], "needs_verification": true}, "…"],
          "farmer_actions": [], "follow_ups": []
        },
        "speaker_map": [{"file_index": 0, "roles": [{"letter": "A", "role": "consultant"}, {"letter": "B", "role": "farmer"}],
                         "confidence": 1.0, "rationale": "화자A는 농약 사용법과 원칙을 설명하고 …"}],
        "needs_verification": ["…약효/혼용 관련 항목 7건…"]
      },
      "s3_key_md": "agents/voicecall/smoke-20260821-33min/artifacts/report.md",
      "s3_key_json": "agents/voicecall/smoke-20260821-33min/artifacts/report.json"
    },
    "result_key": "agents/voicecall/smoke-20260821-33min/artifacts/result.json"
  },
  "callback_status": null
}
```

> 위는 **실제 스모크 응답**(33분 상담 통화, 2026-08-21)을 긴 본문만 축약한 것입니다. 이 통화는
> `farm_access_token` 없이 처리한 사례라 `prefill: null`·`prdlst_code: null`(→`unresolved` 키)입니다 —
> **토큰을 주시면** farmos 작물 목록으로 `prdlst_code`가 확정되고 `structured.prefill`(PutDiaryDTO 초안)이
> 채워집니다. `evidence`의 숫자는 병합 전사(`transcript_key`)의 세그먼트 인덱스입니다.

- `result`는 **`status=COMPLETED`일 때만** 채워집니다.
- `diaries[]`는 통화에서 다룬 **작물별** 1건씩. `prdlst_code`는 farmos 품목코드이며, 작물을 확정하지
  못하면 `null`(S3 키·artifact 경로는 `unresolved`; 한 결과에 미확정 작물이 여럿이면 두 번째부터 `unresolved-2`, `unresolved-3` …). 건별 `status` ∈ `OK|PARTIAL|EMPTY|UNRESOLVED_CROP`.
- **저희는 farmos에 저장하지 않습니다.** `structured.prefill`은 앱 `PUT /m/diary`의 `fields`와 같은
  모양의 **초안**이고, 농가가 앱에서 확인 후 저장하는 용도입니다.
- markdown 본문이 512KB를 넘으면 인라인이 생략됩니다(`markdown: null`) — S3 키 또는 §3.6으로 조회.
- `farm_access_token`은 이 응답을 포함해 어떤 응답에도 포함되지 않습니다.
- 미존재 통화: `404 CALL_NOT_FOUND`.

### 3.6 산출물 · 목록 · 헬스

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/calls/{id}/transcript` | 병합 전사 JSON. 미준비 시 `404 NOT_READY` |
| GET | `/v1/calls/{id}/artifacts/report?format=md\|json` | 컨설팅 보고서 (기본 `text/markdown`) |
| GET | `/v1/calls/{id}/artifacts/diary/{prdlst_code}?format=md\|json` | 작물별 영농일지. 미확정 작물은 `{prdlst_code}` 자리에 `unresolved`(다건이면 `unresolved-2` …) |
| GET | `/v1/calls?status=&state=&limit=50&cursor=` | 운영/디버그용 목록 |
| GET | `/healthz` | 무인증 헬스체크 `{status: "ok"\|"degraded", version, worker: {running, pending_stt, pending_gen, pending_daily}}` — DB 이상 시 `degraded` + `pending_*` 는 `null` |

목록의 `next_cursor`는 **불투명 토큰**입니다(내용을 해석하지 마시고 그대로 되돌려 주세요). 정렬은 최신
생성순이며, `next_cursor`가 `null`이면 마지막 페이지입니다.

### 3.7 `POST /v1/daily-diaries` — 날짜별(멀티콜) 영농일지 트리거

특정 날짜의 통화 여러 건을 합쳐 **하나의 날짜별 영농일지(작물별)** 를 생성합니다. 산출물은 일지뿐이며
**컨설팅 보고서는 없습니다**(보고서는 통화 단위 유지).

```json
{
  "diary_id": "daily_u123_20260819",
  "diary_date": "2026-08-19",
  "call_ids": ["20260819_Qmf1D0X", "20260819_Rx2kP9Y"],
  "farm_access_token": "<농가 JWT>",
  "callback_url": "https://<backend>/agent-callback",
  "language": "ko",
  "metadata": {"hints": {"prdlst_code": "0804MM"}}
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `diary_id` | string | **필수** | `[A-Za-z0-9_.:-]{1,128}`. **멱등성 키** — 백엔드가 farmer/날짜에서 결정적으로 생성해 주세요(예: `daily_{farmerId}_{yyyyMMdd}`). 재전송이 안전해집니다 |
| `diary_date` | string | **필수** | `yyyy-MM-dd`. 산출물 일지의 날짜는 이 값으로 고정됩니다 |
| `call_ids` | array 1..50 | **필수** | 합칠 통화들. 중복 불가. **전부 terminal(`COMPLETED`/`EMPTY`)이어야 하고 1건 이상 `COMPLETED`여야 합니다.** 같은 농가의 통화만 묶는 것은 백엔드 책임입니다 |
| `farm_access_token` | string | 선택 | **매 트리거·재생성마다 새로 보내주세요** — 통화 때 받은 JWT는 통화 terminal 시 저희 DB에서 삭제되어 재사용되지 않습니다. 생략하면 farmos 조회 없이 전사+힌트만으로 생성합니다. 이 토큰도 daily terminal 시 삭제됩니다 |
| `callback_url` / `language` / `metadata` | – | 선택 | §3.1과 동일한 의미(`metadata.hints` 포함) |

**응답**: `201`(신규 — 생성 큐잉) / `200`(같은 `diary_id` 재-POST — 진행 중이면 `note: "already
processing"`, terminal이면 `note`에 regenerate 안내). 본문은 `DailyDiaryDetail`(§3.8). 재-POST는
재생성을 일으키지 않습니다 — 다시 만들려면 `/regenerate`를 호출하세요.

**동기 검증 실패**:

| HTTP | code | 조건 |
|---|---|---|
| 422 | `CALLS_NOT_FOUND` | 존재하지 않는 call 포함 |
| 409 | `CALLS_NOT_READY` | terminal이 아닌(`NONE`/`PROCESSING`) 또는 `FAILED` call 포함 — `FAILED`는 먼저 해당 통화를 `/regenerate` 하세요 |
| 422 | `NO_TRANSCRIBED_CALLS` | `COMPLETED` call이 하나도 없음 |
| 422 | `FARM_MISMATCH` | call들의 `farm.farm_id`가 서로 다름 |

**병합 방식**: 통화를 `started_at` 순으로 이어붙입니다. 병합 전사의 시간축은 각 통화 길이의
누적이며 **통화 사이 실제 공백은 표현되지 않습니다.** 전사의 `files[].call_id`로 원본 통화를 식별할 수
있습니다.

### 3.8 날짜별 일지 — 조회 · 산출물 · 재생성

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/daily-diaries/{diary_id}?inline=false` | 상태/결과 폴링 (§3.5와 같은 요령) |
| GET | `/v1/daily-diaries/{diary_id}/transcript` | 병합 전사 JSON. 미준비 시 `404 NOT_READY` |
| GET | `/v1/daily-diaries/{diary_id}/artifacts/diary/{prdlst_code}?format=md\|json` | 작물별 일지 (미확정 작물은 `unresolved`, 다건이면 `unresolved-2` …) |
| GET | `/v1/daily-diaries?diary_date=&status=&limit=50&cursor=` | 목록 (커서 규칙은 §3.6과 동일) |
| POST | `/v1/daily-diaries/{diary_id}/regenerate` | `{"farm_access_token": "<새 JWT>", "reason": "..."}` → `202` |

- 응답 `DailyDiaryDetail`은 `CallDetail`(§3.5)과 유사하되 `call_ids`/`diary_date`가 추가되고,
  `result`에 **`report`가 없습니다**(`diaries`만). `status`/`generation` 어휘는 통화와 동일합니다.
  실제 스모크 응답 예시(통화 2건 병합, 축약):

```json
{
  "diary_id": "daily-smoke-20260821", "diary_date": "2026-08-21", "status": "COMPLETED",
  "call_ids": ["smoke-20260821-a", "smoke-20260821-b"],
  "created_at": "2026-08-21T05:43:50Z", "updated_at": "2026-08-21T05:48:39Z",
  "metadata": null, "note": null,
  "generation": {"run": 3, "attempts": 1, "state": "IDLE", "model": "gemini-3.5-flash",
                 "warnings": ["farmos 미사용(토큰 없음) — 힌트/전사만으로 생성"],
                 "usage": {"calls": 5, "prompt_tokens": 28004, "completion_tokens": 3769, "total_tokens": 31773}},
  "error": null,
  "result": {
    "transcript_key": "agents/voicecall/daily/daily-smoke-20260821/transcript/merged.json",
    "speaker_map": {"f0:A": "consultant", "f0:B": "farmer", "f1:A": "consultant", "f1:B": "farmer"},
    "diaries": [
      {"prdlst_code": null, "prdlst_nm": "벼", "diary_date": "2026-08-21", "status": "PARTIAL",
       "markdown": "# 영농일지 — 벼 (2026-08-21) …(총 5,592자)",
       "structured": {"…": "통화별 diaries[].structured 와 동일 모양"},
       "s3_key_md": "agents/voicecall/daily/daily-smoke-20260821/artifacts/diary/unresolved.md",
       "s3_key_json": "agents/voicecall/daily/daily-smoke-20260821/artifacts/diary/unresolved.json"},
      {"prdlst_code": null, "prdlst_nm": "콩", "diary_date": "2026-08-21", "status": "EMPTY",
       "markdown": "…(빈 골격)", "structured": {"…": "…"},
       "s3_key_md": "agents/voicecall/daily/daily-smoke-20260821/artifacts/diary/unresolved-2.md",
       "s3_key_json": "agents/voicecall/daily/daily-smoke-20260821/artifacts/diary/unresolved-2.json"}
    ],
    "result_key": "agents/voicecall/daily/daily-smoke-20260821/artifacts/result.json"
  },
  "callback_status": null
}
```

  (병합 전사의 `speaker_map`은 통화별 파일 인덱스 `f0:`/`f1:` 접두로 화자를 구분합니다.)
- 미존재: `404 DAILY_NOT_FOUND`.
- **재생성**: 진행 중이면 `409 ALREADY_PROCESSING`. farmos 조회를 포함하려면 body에 새
  `farm_access_token`을 함께 보내주세요(통화의 §3.4와 달리 **한 번의 호출**로 됩니다). 산출물은 같은
  S3 키에 덮어쓰기되고 `generation.run`이 +1 됩니다.
- 멤버 통화가 나중에 재생성되어 내용이 바뀌었어도 daily가 자동 갱신되지는 않습니다 — daily
  `/regenerate`를 호출하시면 그 시점의 전사로 다시 병합·생성합니다.

## 4. 상태 라이프사이클

| 필드 | 값 | 의미 |
|---|---|---|
| `state` | `OPEN` → `ENDED` | `/end` 수신 여부. 편도 |
| `status` | `NONE` → `PROCESSING` → `COMPLETED` \| `EMPTY` \| `FAILED` | 결과 상태. `/regenerate` 시 terminal → `PROCESSING` 복귀 |
| `audio[].status` | `PENDING` → `TRANSCRIBING` → `TRANSCRIBED` \| `FAILED` | 녹음별 STT 상태 |
| `generation.state` | `IDLE` → `QUEUED` → `RUNNING` → `IDLE` | 생성 파이프라인 상태 |

terminal 사유 (`status` + `error.code`):

| status | error.code | 조건 |
|---|---|---|
| `EMPTY` | `NO_AUDIO` | 오디오 이벤트 0건 |
| `EMPTY` | `NO_TRANSCRIPT` | 전사 결과에 텍스트 없음 |
| `EMPTY` | `NO_CONTENT` | 통화에서 추출할 내용 없음 |
| `FAILED` | `STT_FAILED` | 모든 오디오 STT 실패 |
| `FAILED` | `GENERATION_FAILED` | 생성 재시도 소진 |
| `COMPLETED` | `null` | 성공 |

- 일부 오디오만 STT에 실패하면(≥1건 성공) 성공분만으로 생성하고 `generation.warnings`에 남깁니다.
  이때 개별 오디오의 `last_error`에 `STT_TIMEOUT` 등이 기록될 수 있습니다.
- `/end` 후 1시간이 지나도 안 끝난 STT는 해당 오디오만 `FAILED(STT_TIMEOUT)` 처리하고 부분 생성합니다.
- 저희 서버가 재시작되어도 진행 중이던 작업은 자동 복구됩니다 — 백엔드 측 조치 불필요.

**날짜별 일지(`/v1/daily-diaries`)의 라이프사이클**은 통화와 같은 어휘를 씁니다. `state`는 없고
(`/end`가 없으므로), `status`는 트리거 즉시 `PROCESSING`으로 시작해 terminal로 갑니다:

| status | error.code | 조건 |
|---|---|---|
| `EMPTY` | `NO_TRANSCRIPT` | 멤버 통화들에 전사된 오디오가 하나도 없음 |
| `EMPTY` | `NO_CONTENT` | 병합 전사에서 추출할 내용 없음 |
| `FAILED` | `GENERATION_FAILED` | 생성 재시도 소진 |
| `COMPLETED` | `null` | 성공 |

## 5. 콜백 (terminal 알림, 선택)

- **등록**: 통화별로 `POST /v1/calls` body의 `callback_url`. 전역 설정이 아닙니다.
  추가로 저희 서버 설정(`CALLBACK_ENABLED`)이 켜져 있어야 발사됩니다 — **콜백 방식으로 가시려면
  go-live 전에 저희에게 활성화를 요청해 주세요.** 그전까지는 폴링만 동작합니다.
- **발사 시점**: terminal(`COMPLETED`/`EMPTY`/`FAILED`) 전이마다 1회. `/regenerate`로 다시 terminal이
  되면 다시 발사됩니다.
- **요청**: `POST <callback_url>`, `Content-Type: application/json`. 합의된 경우
  `X-API-Key: <CALLBACK_API_KEY>` 헤더 포함(주의: Bearer 아님, `AGENT_API_KEY`와 **별개** 시크릿).

```json
{
  "call_id": "...",
  "status": "COMPLETED",
  "error": null,
  "result_url": "https://jinong-stt-report-generation.jinongservice.co.kr/v1/calls/<call_id>",
  "generation_run": 1
}
```

- `result_url` 조회에도 `AGENT_API_KEY` 인증이 필요합니다.
- **재시도**: 최대 3회(시도 간 10초·30초 대기, 시도당 타임아웃 10초). 2xx 응답이면 성공.
- **at-least-once입니다.** 서명이 없으므로 백엔드는 `(call_id, generation_run)`으로 중복 제거하고,
  콜백 유실 대비로 폴링을 안전망으로 두는 것을 권장합니다. 콜백 성공/실패는 `CallDetail.callback_status`
  (`SENT`/`FAILED`)로 확인할 수 있으며, 콜백 실패가 통화 상태에 영향을 주지는 않습니다.

**날짜별 일지 콜백**: 전송 규칙(재시도·헤더·at-least-once)은 동일하고, `callback_url`은
`POST /v1/daily-diaries` body로 등록합니다. 페이로드는 `call_id` 대신 아래 형태입니다 — 수신 라우팅 시
`daily_diary_id` 필드의 존재로 통화 콜백과 구분하시고, 중복 제거는 `(daily_diary_id, generation_run)`
기준으로 해주세요:

```json
{
  "daily_diary_id": "daily_u123_20260819",
  "diary_date": "2026-08-19",
  "status": "COMPLETED",
  "error": null,
  "call_ids": ["20260819_Qmf1D0X", "20260819_Rx2kP9Y"],
  "result_url": "https://jinong-stt-report-generation.jinongservice.co.kr/v1/daily-diaries/daily_u123_20260819",
  "generation_run": 1
}
```

## 6. S3 규약 (백엔드 준비 사항)

**입력(녹음)** — 백엔드 버킷:
- 백엔드가 녹음을 S3에 업로드하고 참조만 전달합니다. 저희는 해당 객체를 **읽기만** 하고 복사하지 않습니다.
- **저희 서비스의 IAM principal에 해당 버킷/prefix의 `s3:GetObject` + `s3:HeadObject` 권한을 부여해
  주세요.** (`/audio` 호출이 `422 S3_ACCESS_DENIED`면 대부분 이 권한 누락입니다. principal ARN은 별도 전달.)
- 크기 제한 200MB. 오디오 포맷 검증은 STT 게이트웨이 소관이며(ogg/wav/m4a 사용 확인됨), 미지원 포맷은
  해당 오디오의 STT 실패로 나타납니다.

**출력(산출물)** — 저희 버킷 `jinong-agri-stt`:

```
agents/voicecall/{call_id}/call.json                        시작/종료 스냅샷
agents/voicecall/{call_id}/stt/{NN}-{hash}.json             STT 원본 응답
agents/voicecall/{call_id}/transcript/merged.json | .md     병합 전사
agents/voicecall/{call_id}/artifacts/diary/{prdlst_code}.md | .json   (미확정: unresolved.*)
agents/voicecall/{call_id}/artifacts/report.md | .json
agents/voicecall/{call_id}/artifacts/result.json            전체 스냅샷 (generation_run 포함)

agents/voicecall/daily/{diary_id}/daily.json                트리거 스냅샷
agents/voicecall/daily/{diary_id}/transcript/merged.json | .md        멀티콜 병합 전사
agents/voicecall/daily/{diary_id}/artifacts/diary/{prdlst_code}.md | .json
agents/voicecall/daily/{diary_id}/artifacts/result.json     (report 없음)
```

- API 응답의 `s3_key_md` / `s3_key_json` / `transcript_key` / `result_key`는 **이 버킷
  (`jinong-agri-stt`) 기준 키**입니다 (응답에 버킷명은 포함되지 않습니다).
- 재생성 시 같은 키에 덮어씁니다 — 실행별 이력이 필요하면 `result.json`의 `generation_run`을 참고하세요.
- 직접 S3 조회 대신 §3.6 artifact 엔드포인트 사용을 권장합니다(권한 협의 불필요).

## 7. 제한 · 타이밍

| 항목 | 값 |
|---|---|
| 요청 본문 | ≤ 4MB (JSON 이벤트만) |
| 오디오 파일 | ≤ 200MB |
| STT 소요 | 약 10초/오디오-1분, 오디오당 최대 4회 재시도 |
| STT 데드라인 | `/end` 후 1시간 — 초과분은 `STT_TIMEOUT` 처리 후 부분 생성 |
| 생성 | 최대 10분 × 2회 시도 |
| 폴링 권장 | `GET /v1/calls/{id}?inline=false`, 5초 간격 (일반 통화는 종료 후 수 분 내 terminal) |
| 인바운드 rate limit | 없음 (429 미사용) |

## 8. 에러 코드 요약

| HTTP | code | 의미 |
|---|---|---|
| 401 | – (`detail` 문자열) | API 키 없음/불일치 |
| 404 | `CALL_NOT_FOUND` | 미존재 통화 |
| 404 | `DAILY_NOT_FOUND` | 미존재 날짜별 일지 |
| 404 | `NOT_READY` | 전사/산출물 아직 미생성 |
| 409 | `CALL_NOT_ENDED` | `/end` 전에 `/regenerate` 호출 |
| 409 | `ALREADY_PROCESSING` | 생성/전사 진행 중 `/regenerate` 또는 daily 트리거 |
| 409 | `CALLS_NOT_READY` | daily 트리거의 `call_ids`에 terminal 아닌/`FAILED` 통화 포함 (§3.7) |
| 422 | `CALLS_NOT_FOUND` | daily 트리거의 `call_ids`에 미존재 통화 포함 |
| 422 | `NO_TRANSCRIBED_CALLS` | daily 트리거에 `COMPLETED` 통화가 하나도 없음 |
| 422 | `FARM_MISMATCH` | daily 트리거의 통화들이 서로 다른 농장 소속 |
| 422 | `INVALID_CURSOR` | 목록 `cursor` 형식 오류 (§3.6 — `next_cursor`를 그대로 사용하세요) |
| 422 | `S3_OBJECT_NOT_FOUND` | 녹음 객체 없음 |
| 422 | `S3_ACCESS_DENIED` | 녹음 읽기 권한 없음 (§6) |
| 422 | `AUDIO_TOO_LARGE` | 200MB 초과 |
| 422 | `S3_ERROR` | 기타 S3 오류 |
| 422 | – (FastAPI 리스트) | 요청 스키마 검증 실패 |

## 9. cURL 예시

```bash
K=<AGENT_API_KEY>; B=https://jinong-stt-report-generation.jinongservice.co.kr

# 1) 통화 시작
curl -X POST $B/v1/calls -H "Authorization: Bearer $K" -H 'Content-Type: application/json' -d '{
  "call_id": "c1",
  "participants": [{"role":"farmer","user_id":"u1","name":"홍길동"},
                   {"role":"consultant","user_id":"c9","name":"김상담"}],
  "farm_access_token": "<JWT>",
  "callback_url": "https://<backend>/agent-callback"}'

# 2) 녹음 수신 (S3 참조)
curl -X POST $B/v1/calls/c1/audio -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"bucket":"<recording-bucket>","key":"voicetalk/....ogg","seq":1}'

# 3) 통화 종료
curl -X POST $B/v1/calls/c1/end -H "Authorization: Bearer $K" -H 'Content-Type: application/json' -d '{}'

# 4) 폴링
curl "$B/v1/calls/c1?inline=false" -H "Authorization: Bearer $K"

# 5) 결과
curl "$B/v1/calls/c1" -H "Authorization: Bearer $K"
curl "$B/v1/calls/c1/artifacts/report" -H "Authorization: Bearer $K"

# 6) 날짜별 일지 트리거 (그날 통화 c1, c2 가 모두 terminal 이 된 뒤)
curl -X POST $B/v1/daily-diaries -H "Authorization: Bearer $K" -H 'Content-Type: application/json' -d '{
  "diary_id": "daily_u1_20260819", "diary_date": "2026-08-19",
  "call_ids": ["c1", "c2"], "farm_access_token": "<새 JWT>",
  "callback_url": "https://<backend>/agent-callback"}'

# 7) 폴링 → 결과
curl "$B/v1/daily-diaries/daily_u1_20260819?inline=false" -H "Authorization: Bearer $K"
curl "$B/v1/daily-diaries/daily_u1_20260819" -H "Authorization: Bearer $K"
```

## 10. 연동 전 체크리스트

- [ ] `AGENT_API_KEY` 수령 (저희 발급)
- [ ] 녹음 버킷/prefix에 저희 IAM principal 읽기 권한(`GetObject`+`HeadObject`) 부여
- [ ] 통화 시작 페이로드에 `farm_access_token`(농가 JWT) 포함
- [ ] 알림 방식 결정: 콜백(→ `callback_url` 전달 + `CALLBACK_API_KEY` 합의 + 저희 쪽 활성화 요청) 또는 폴링
- [ ] 콜백 수신 시 `(call_id, generation_run)` 기준 중복 제거 구현
- [ ] 에러 파서: `detail` 문자열(401) / 객체(도메인) / 리스트(422 검증) 모두 처리
- [ ] terminal 후 추가 오디오 발생 시 `/regenerate` 호출 로직 (farmos 조회가 필요하면 body에 새 JWT — §3.4)

날짜별 일지(§3.7)를 쓰시는 경우 추가로:

- [ ] `diary_id` 결정론적 생성 규칙 확정 (권장: `daily_{farmerId}_{yyyyMMdd}`)
- [ ] 트리거 시점 결정: 하루 마감 배치 또는 마지막 통화 terminal 콜백 수신 시
- [ ] **트리거·재생성마다 새 농가 JWT 첨부** (통화 때 보낸 토큰은 재사용되지 않음)
- [ ] 멤버 통화 전부 terminal 확인 후 트리거 (`CALLS_NOT_READY` 시 재시도/지연 처리)
- [ ] daily 콜백 수신 라우팅: `daily_diary_id` 필드로 구분, `(daily_diary_id, generation_run)` 중복 제거
