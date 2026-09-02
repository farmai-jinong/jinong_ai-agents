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
(1) POST /v1/calls                      통화 시작 — call_id, 참가자, 농가 JWT
(2) POST /v1/calls/{id}/audio  ×N       녹음 생길 때마다 — S3 bucket/key 참조 (즉시 STT 큐잉)
(3) POST /v1/calls/{id}/end             통화 종료 — STT 진행 중이어도 즉시 보내도 안전
(4) 통화요약 콜백 수신(§5.1, 통화 단순요약 동봉) 또는 GET /v1/calls/{id}?inline=false 를 5초 간격 폴링
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
    {"role": "farmer", "user_id": "u123", "engn_id": "18", "name": "홍길동"},
    {"role": "consultant", "user_id": "c9", "name": "김상담"}
  ],
  "farm_access_token": "<농가 JWT>",
  "farm": {"farm_id": "f1", "farm_nm": "..."},
  "num_speakers": 2,
  "language": "ko",
  "metadata": {"hints": {"prdlst_code": "0804MM", "prdlst_nm": "딸기"}}
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `call_id` | string | **필수** | `[A-Za-z0-9_.:-]{1,128}`. kafka-gateway의 `callId` 그대로 사용 — 저희 쪽 기본키 |
| `started_at` | datetime | 선택 | 통화 시작 시각 |
| `participants` | array(≤4) | 선택 | `{role, user_id, engn_id, name}`. `role`은 `"farmer"` 또는 `"consultant"`만 허용(그 외 422). 화자 수 추정에도 사용. **농가 구분은 `engn_id`(영농체 ID) + `user_id` 복합 키** — farmer 항목에는 `engn_id`를 넣어 주세요(날짜별 일지의 동일 농가 검사에 사용, §3.7) |
| `farm_access_token` | string | 선택 | **농가 JWT.** farmos 영농일지 관련 **읽기 조회에만** 사용. 응답·로그에 절대 노출되지 않고 terminal 시 삭제. 없으면 farmos 조회 없이 전사+힌트만으로 생성 |
| `farm` | object | 선택 | 자유 형식(`farm_id`, `farm_nm` 등). 응답에 그대로 반환 |
| `num_speakers` | int 1..8 | 선택 | STT 화자 분리 힌트 |
| `language` | string | 선택 | 기본 `"ko"` |
| `callback_url` | string | 선택 | 호환용으로 계속 받지만 **통화 단위 콜백에는 사용하지 않습니다** — 통화 결과 알림은 저희 설정의 통화요약 콜백 URL로 발사됩니다 (§5.1). 이 필드는 날짜별 일지(§3.7) 전용 |
| `metadata` | object | 선택 | 자유 형식. `metadata.hints`는 특별 취급(아래) |

`metadata.hints` (선택 — farmos 조회가 없거나 실패할 때 대체 사용):
`prdlst_code`, `prdlst_nm`, `farmer_crops[]`(`[{prdlstCode, prdlstNm, reprsntPrdlstCnt}]`), `diary_date`(`yyyy-MM-dd`), `topic`,
`farmer_engn_id`·`farmer_user_id`(농가 복합 키 — `participants`의 farmer에 `engn_id`가 없을 때 대체 조회 키로 사용).

**응답**: `201`(신규) / `200`(재전송) — 본문은 `CallDetail`(§3.5).

**재전송(업서트) 규칙**:
- 같은 `call_id`로 다시 보내면 보낸 필드만 갱신됩니다(부분 업서트). **JWT가 만료되면 새
  `farm_access_token`으로 이 엔드포인트를 다시 호출해 갱신할 수 있습니다.**
- 통화가 이미 terminal(`COMPLETED|EMPTY|FAILED`)이어도 **`participants` / `farm` / `metadata`는
  갱신합니다** — 누락된 `engn_id`를 같은 `call_id` 재등록으로 보정하는 용도입니다. 상태·산출물·생성
  회차는 바뀌지 않고, 갱신된 필드가 `note`에 담겨 옵니다
  (`"call already finalized — updated participants, metadata"`, 바뀐 게 없으면 `"call already finalized"`).
- terminal 통화에서 `farm_access_token`은 이 경로로 갱신되지 않습니다 — §3.4의 `/regenerate`를 쓰세요.

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
      "markdown": "> 📝 **통화 요약** · …\n> 💬 …\n\n| 항목 | 값 |\n…(총 1,800자 내외; H1 제목 없음, 섹션은 항상 주요 농작업·기타 기록사항·병해충·방제이력·농작업 사진·투입 제품·향후 작업·확인 계획·근거 발화·참고)",
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
      "s3_key_json": "agents/voicecall/smoke-20260821-33min/artifacts/diary/unresolved.json",
      "s3_key_md_internal": "agents/voicecall/smoke-20260821-33min/artifacts/internal/diary/unresolved.md"
    }, {
      "prdlst_code": null, "prdlst_nm": "양파", "diary_date": "2026-08-21", "status": "EMPTY",
      "markdown": "…(언급만 되고 기록할 내용이 없는 작물 — 빈 골격 450자)",
      "structured": {"…": "위와 동일 모양, mapping/evidence 비어 있음"},
      "s3_key_md": "agents/voicecall/smoke-20260821-33min/artifacts/diary/unresolved-2.md",
      "s3_key_json": "agents/voicecall/smoke-20260821-33min/artifacts/diary/unresolved-2.json",
      "s3_key_md_internal": "agents/voicecall/smoke-20260821-33min/artifacts/internal/diary/unresolved-2.md"
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
      "s3_key_json": "agents/voicecall/smoke-20260821-33min/artifacts/report.json",
      "s3_key_md_internal": "agents/voicecall/smoke-20260821-33min/artifacts/internal/report.md"
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
  못하면 `null`(S3 키·artifact 경로는 `unresolved`; 한 결과에 미확정 작물이 여럿이면 두 번째부터 `unresolved-2`, `unresolved-3` …). 건별 `status` ∈ `OK|PARTIAL|EMPTY|UNRESOLVED_CROP`
  (`EMPTY` = 이 작물로 일지에 남길 내용 없음 — 규칙 판정 + 독립 LLM 검수 패스, §5.1).
- **저희는 farmos에 저장하지 않습니다.** `structured.prefill`은 앱 `PUT /m/diary`의 `fields`와 같은
  모양의 **초안**이고, 농가가 앱에서 확인 후 저장하는 용도입니다.
- markdown 본문이 512KB를 넘으면 인라인이 생략됩니다(`markdown: null`) — S3 키 또는 §3.6으로 조회.
- **마크다운은 두 벌입니다** (2026-09-02 변경). `markdown`/`s3_key_md`는 **전달용** — 앱·화면에 그대로 쓰는 본문으로,
  근거 표기 `(근거: #N)`·`## 근거 발화`(전사 인용)·`## 참고`(내부 경고)·작물 코드 `(0804MM)`·통화 ID 행·모델/프롬프트
  버전이 **빠져 있습니다**(보고서는 화자 식별 신뢰도 행도 뺍니다). 항목·판정 문구(`[표준 목록 미매핑]`, `※ 확인 필요`,
  `※ 라벨·PLS 확인 필요`)와 동의서 §8 안내는 그대로입니다. **근거가 다 들어간 정본**은 `s3_key_md_internal`
  (`…/artifacts/internal/…`)에 따로 저장되며 응답에 본문은 싣지 않습니다 — 필요하면 §3.6 엔드포인트에 `?view=internal`.
  `structured`(prefill 포함)·`.json`은 한 벌 그대로입니다.
- `farm_access_token`은 이 응답을 포함해 어떤 응답에도 포함되지 않습니다.
- 미존재 통화: `404 CALL_NOT_FOUND`.

### 3.6 산출물 · 목록 · 헬스

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/calls/{id}/transcript` | 병합 전사 JSON. 세그먼트마다 `role`(농가/컨설턴트) 포함 — §3.6.1. 미준비 시 `404 NOT_READY` |
| GET | `/v1/calls/{id}/artifacts/report?format=md\|json&view=public\|internal` | 컨설팅 보고서 (기본 `text/markdown`). `view` 기본 `public`(전달용, 근거 제거), `internal`은 근거 포함 정본. `format=json`이면 `view` 무시, 그 외 값은 `400 INVALID_VIEW` |
| GET | `/v1/calls/{id}/artifacts/diary/{prdlst_code}?format=md\|json&view=public\|internal` | 작물별 영농일지(`view` 규칙 동일). 미확정 작물은 `{prdlst_code}` 자리에 `unresolved`(다건이면 `unresolved-2` …) |
| GET | `/v1/calls?status=&state=&limit=50&cursor=` | 운영/디버그용 목록 |
| GET | `/healthz` | 무인증 헬스체크 `{status: "ok"\|"degraded", version, worker: {running, pending_stt, pending_gen, pending_daily}}` — DB 이상 시 `degraded` + `pending_*` 는 `null` |

목록의 `next_cursor`는 **불투명 토큰**입니다(내용을 해석하지 마시고 그대로 되돌려 주세요). 정렬은 최신
생성순이며, `next_cursor`가 `null`이면 마지막 페이지입니다.

#### 3.6.1 화자 역할 — 누가 농가인가

STT가 붙이는 화자 글자 `A`/`B`는 **그 녹음에서 먼저 말한 순서**일 뿐입니다. 발신/수신과도 무관하고,
녹음 파일이 둘로 나뉘면 A와 B가 뒤바뀝니다. 그래서 글자만으로는 농가를 가릴 수 없습니다.

저희가 통화 **내용**으로 역할을 추정해 두 곳에 같은 값으로 실어 드립니다:

```json
// GET /v1/calls/{id}/transcript
{"speaker_map": {"f0:A": "consultant", "f0:B": "farmer"},
 "segments": [{"speaker_key": "f0:A", "role": "consultant", "abs_start": 0.0, "text": "…"}]}
```

| 위치 | 값 |
|---|---|
| `GET /v1/calls/{id}/transcript` → `segments[].role` | `farmer` \| `consultant` \| `unknown` (세그먼트별) |
| `GET /v1/calls/{id}/transcript` → `speaker_map` | `speaker_key` → 역할 (같은 값의 요약) |
| `GET /v1/calls/{id}` → `result.speaker_map` | 위와 항상 같은 값 |
| 통화요약 콜백 payload → `speaker_map` | 위와 같은 값 (§5.1) |

- **확신이 없으면 `unknown`으로 둡니다** — 찍지 않습니다. 화면에는 "화자 A/B"로 그대로 두시면 됩니다.
- 역할은 **생성이 끝난 뒤** 채워집니다. 통화요약 콜백을 받은 시점이면 이미 들어 있습니다.
  `EMPTY`/`FAILED`로 끝난 통화는 전부 `unknown`입니다.
- `speaker_key`(`f0:A`)의 `f0`은 녹음 파일 인덱스입니다 — 파일이 여럿이면 `f0:A`와 `f1:A`가 **다른
  사람일 수 있으니** 반드시 `speaker_key` 단위로 매칭해 주세요.

### 3.7 `POST /v1/daily-diaries` — 날짜별(멀티콜) 영농일지 트리거

특정 날짜의 통화 여러 건을 합쳐 **하나의 날짜별 영농일지(작물별)** 를 생성합니다. 산출물은 일지뿐이며
**컨설팅 보고서는 없습니다**(보고서는 통화 단위 유지).

```json
{
  "diary_id": "daily_18_u123_20260819",
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
| `diary_id` | string | **필수** | `[A-Za-z0-9_.:-]{1,128}`. **멱등성 키** — 백엔드가 농가 복합 키/날짜에서 결정적으로 생성 (**확정 규칙**: `daily_{engnId}_{userId}_{yyyyMMdd}`, 예: `daily_18_u123_20260819`). 재전송이 안전해집니다 |
| `diary_date` | string | **필수** | `yyyy-MM-dd`. 산출물 일지의 날짜는 이 값으로 고정됩니다 |
| `call_ids` | array 1..50 | **필수** | 합칠 통화들(그 날짜의 **전체 목록** — 통화가 늘면 누적해서 전부 보내주세요). 중복 불가. **전부 terminal(`COMPLETED`/`EMPTY`)이어야 하고 1건 이상 `COMPLETED`여야 합니다.** 같은 농가의 통화만 묶는 것은 백엔드 책임이며, `call_id`↔작물 매핑은 보내실 필요 없습니다 |
| `farm_access_token` | string | 선택 | **매 트리거·재생성마다 새로 보내주세요** — 통화 때 받은 JWT는 통화 terminal 시 저희 DB에서 삭제되어 재사용되지 않습니다. 생략하면 farmos 조회 없이 전사+힌트만으로 생성합니다. 이 토큰도 daily terminal 시 삭제됩니다 |
| `callback_url` | string | 선택 | 날짜별 일지 완료를 알릴 agent-callback 수신 URL (§5.2). 통화 단위와 달리 **여기서는 실제로 사용됩니다** |
| `language` / `metadata` | – | 선택 | §3.1과 동일한 의미(`metadata.hints` 포함) |

**응답**: `201`(신규 — 생성 큐잉) / `200`(같은 `diary_id` 재-POST). 본문은 `DailyDiaryDetail`(§3.8).

**재-POST = 재생성입니다 (2026-08-26 변경).** 통화가 추가될 때마다 같은 `diary_id` 로 **그 날짜의 전체
`call_ids`** 를 다시 보내주시면 됩니다. `/regenerate` 를 따로 부르실 필요가 없습니다.

| 그 시점 상태 | 동작 | `note` |
|---|---|---|
| terminal (`COMPLETED`/`EMPTY`/`FAILED`) | `call_ids` 를 요청값으로 교체하고 새 생성 회차 큐잉 (`generation.run` +1) | `regeneration queued` |
| 생성 대기 중 | `call_ids` 만 갱신 — 대기 중인 실행이 새 목록을 읽습니다 (회차 유지) | `already queued — call_ids updated` |
| 생성 실행 중 | **아무것도 바꾸지 않습니다** — 실행 중인 회차가 이미 목록을 읽었기 때문입니다. 다음 트리거나 보정 배치에서 반영됩니다 | `generation in progress — re-POST after it finishes` |

- `metadata`·`callback_url`·`language` 는 재-POST 값으로 갱신됩니다. `farm_access_token` 은 **보내셨을
  때만** 갱신합니다(자동 배치처럼 생략하면 기존 값을 건드리지 않습니다).
- `diary_date` 는 `diary_id` 에 이미 포함돼 있어 **불변**입니다 — 최초 생성값을 유지합니다.
- 세 경우 모두 2xx이므로 백엔드는 "생성 요청 접수"로 처리하시면 됩니다.

**동기 검증 실패**:

| HTTP | code | 조건 |
|---|---|---|
| 422 | `CALLS_NOT_FOUND` | 존재하지 않는 call 포함 |
| 409 | `CALLS_NOT_READY` | terminal이 아닌(`NONE`/`PROCESSING`) 또는 `FAILED` call 포함 — `FAILED`는 먼저 해당 통화를 `/regenerate` 하세요 |
| 422 | `NO_TRANSCRIBED_CALLS` | `COMPLETED` call이 하나도 없음 |
| 422 | `FARM_MISMATCH` | call들의 `farm.farm_id` 또는 farmer 복합 키(`engn_id`+`user_id`)가 서로 다름 |

**병합 방식**: 통화를 `started_at` 순으로 이어붙입니다. 병합 전사의 시간축은 각 통화 길이의
누적이며 **통화 사이 실제 공백은 표현되지 않습니다.** 전사의 `files[].call_id`로 원본 통화를 식별할 수
있습니다.

### 3.8 날짜별 일지 — 조회 · 산출물 · 재생성

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/daily-diaries/{diary_id}?inline=false` | 상태/결과 폴링 (§3.5와 같은 요령) |
| GET | `/v1/daily-diaries/{diary_id}/transcript` | 병합 전사 JSON. 미준비 시 `404 NOT_READY` |
| GET | `/v1/daily-diaries/{diary_id}/artifacts/diary/{prdlst_code}?format=md\|json&view=public\|internal` | 작물별 일지 (`view` 규칙은 §3.6과 동일; 미확정 작물은 `unresolved`, 다건이면 `unresolved-2` …) |
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
       "markdown": "> 📝 **통화 요약** · … …(총 5,600자 내외)",
       "structured": {"…": "통화별 diaries[].structured 와 동일 모양"},
       "s3_key_md": "agents/voicecall/daily/daily-smoke-20260821/artifacts/diary/unresolved.md",
       "s3_key_json": "agents/voicecall/daily/daily-smoke-20260821/artifacts/diary/unresolved.json",
       "s3_key_md_internal": "agents/voicecall/daily/daily-smoke-20260821/artifacts/internal/diary/unresolved.md"},
      {"prdlst_code": null, "prdlst_nm": "콩", "diary_date": "2026-08-21", "status": "EMPTY",
       "markdown": "…(빈 골격)", "structured": {"…": "…"},
       "s3_key_md": "agents/voicecall/daily/daily-smoke-20260821/artifacts/diary/unresolved-2.md",
       "s3_key_json": "agents/voicecall/daily/daily-smoke-20260821/artifacts/diary/unresolved-2.json",
       "s3_key_md_internal": "agents/voicecall/daily/daily-smoke-20260821/artifacts/internal/diary/unresolved-2.md"}
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

## 5. 콜백

콜백은 두 종류이고 **용도가 다릅니다**. 통화 단위는 결과 본문을 동봉하는 **통화요약 콜백**,
날짜별 일지는 마스터 ID만 알리는 **agent-callback**입니다. 저희 서버 콜백 활성화는 **완료**됐습니다
(2026-08-24, `CALLBACK_ENABLED=true` + 합의된 `X-API-Key`).

공통 규칙:

- **헤더**: `Content-Type: application/json` + `X-API-Key: <CALLBACK_API_KEY>`
  (주의: Bearer 아님, `AGENT_API_KEY`와 **별개** 시크릿 = 백엔드 `VOICETALK_EXTERNAL_CALLBACK_API_KEY`).
- **발사 시점**: terminal(`COMPLETED`/`EMPTY`/`FAILED`) 전이마다 1회. `/regenerate`로 다시 terminal이
  되면 다시 발사됩니다. 산출물 S3 저장이 끝난 **뒤에** 발사하므로 콜백 수신 시점에 키는 이미 존재합니다.
- **재시도**: 최대 3회(시도 간 10초·30초 대기, 시도당 타임아웃 10초). 2xx면 성공.
  **4xx(429 제외)는 재시도하지 않습니다** — 요청/설정을 고치기 전에는 결과가 같기 때문입니다.
- **at-least-once입니다.** 서명이 없으므로 중복 제거와 폴링 안전망을 권장합니다. 콜백 성공/실패는
  `CallDetail.callback_status` / `DailyDiaryDetail.callback_status`(`SENT`/`FAILED`)로 확인할 수 있고,
  콜백 실패가 통화/일지 상태에 영향을 주지는 않습니다.

### 5.1 통화 단위 — 통화요약 콜백 (통화 단순요약 동봉)

- **수신 URL**: 저희 쪽 전역 설정(`SUMMARY_CALLBACK_URL`)입니다. 개발
  `https://dev.jinongservice.co.kr/voicetalk/public/call-summary-callback`, 운영 전환 시 `data.` 도메인
  URL을 알려주시면 저희가 바꿉니다. (통화별 `callback_url`로 받지 않습니다.)
- **`content`는 통화 내용의 단순요약**입니다 — 주제/조치/후속 불릿 3줄, 대체로 100자 안팎.
  통화이력 화면에 한 줄로 붙이기 좋은 분량입니다.
- **영농일지·컨설팅 보고서 마크다운은 콜백에 싣지 않습니다.** 일지와 전사본은 종전대로
  `GET /v1/calls/{call_id}` 응답(§3.5)으로 가져가시면 됩니다 — **이 콜백 도착이 곧 그 결과가 준비됐다는
  신호**를 겸합니다(별도의 완료 알림은 보내지 않습니다).
- 요약은 일지를 요약한 것이 아니라 **녹취문에서 직접 뽑습니다** — 일지 서식이 바뀌어도 요약 문구는
  영향을 받지 않습니다.

```json
{
  "call_id": "20260819_Qmf1D0X",
  "summary_type": "SUMMARY",
  "status": "COMPLETED",
  "content": "- 주제: 딸기 잿빛곰팡이병 방제 상담\n- 조치: 환기 관리 권고 / 병든 과실 제거\n- 후속: 화요일 방문 예정",
  "engine_version": "jinong-summary-v1/gemini-3.5-flash",
  "speaker_map": {"f0:A": "consultant", "f0:B": "farmer"},
  "diaries": [{"prdlst_code": "0804MM", "prdlst_nm": "딸기", "status": "OK",
               "s3_key_md": "agents/voicecall/20260819_Qmf1D0X/artifacts/diary/0804MM.md",
               "s3_key_md_internal": "agents/voicecall/20260819_Qmf1D0X/artifacts/internal/diary/0804MM.md"}],
  "report": {"s3_key_md": "agents/voicecall/20260819_Qmf1D0X/artifacts/report.md",
             "s3_key_md_internal": "agents/voicecall/20260819_Qmf1D0X/artifacts/internal/report.md"}
}
```

- **`diaries[]`·`report`는 선택 필드입니다** (2026-09-02 추가, `COMPLETED`에만) — 산출물의 **S3 키만** 담고 본문은
  없습니다. `s3_key_md`는 전달용(근거 제거) 마크다운, `s3_key_md_internal`은 근거 포함 정본(`artifacts/internal/`)의
  키이며 `GET /v1/calls/{call_id}` `result`의 값과 같습니다. 키는 `jinong-agri-stt` 버킷 기준이고 prefix는 환경별
  (개발 `agents/voicecall-dev/`, 운영 `agents/voicecall/`; §6). **DTO에서 무시하셔도 되고**, 미지 필드를 거부하는
  설정이면 알려주세요 — 저희 스위치(`CALLBACK_INCLUDE_ARTIFACT_KEYS`)로 즉시 뺍니다. `prdlst_code`는 미확정이면
  `null`(키는 `unresolved…`). MinIO 를 직접 읽지 않으셔도 됩니다 — 같은 본문을 §3.6 엔드포인트(`?view=internal`)로 받을 수 있습니다.

- **`speaker_map`은 선택 필드입니다** (2026-08-31 추가) — 화자 글자 `A`/`B` 중 누가 농가인지의 추정
  결과입니다(§3.6.1). **DTO에서 무시하셔도 됩니다.** 다만 미지 필드를 거부(4xx)하는 설정이면
  알려주세요 — 저희 쪽 스위치(`CALLBACK_INCLUDE_SPEAKER_MAP`)로 즉시 뺍니다. 같은 값은
  `GET /v1/calls/{call_id}`의 `result.speaker_map`에도 있습니다.
- 역할이 확정되지 않은 통화(`EMPTY`/`FAILED`, 또는 추정 신뢰도 미달)에서는 키가 없거나 값이
  `unknown`입니다.

수신 후 흐름:

```
통화요약 콜백 수신  →  content 를 통화이력 요약으로 저장
                  └→  GET /v1/calls/{call_id}          영농일지 result.diaries[] · 보고서 result.report
                      GET /v1/calls/{call_id}/transcript  전사본(MergedTranscript)
```

| status | 본문 | 비고 |
|---|---|---|
| `COMPLETED` | `content` 필수 | 요약·일지 모두 준비됨 |
| `EMPTY` | `empty_reason` | 일지로 남길 내용이 없음. `content` 키는 아예 오지 않습니다 |
| `FAILED` | `fail_reason` (≤1000자) | 예 `"GENERATION_FAILED: ..."` — 앞부분이 §8 에러 코드 |

**`EMPTY` 판별 (2026-08-26 — 백엔드 요청 반영).** 영농일지와 무관한 통화(안부·잡담·일정조율)에서 빈 템플릿이
`COMPLETED`로 나가던 문제를 고쳤습니다. **생성된 일지 초안을 독립 LLM 패스가 한 번 더 검수**해 실질 내용
(농작업·생육관찰·병해충·자재투입)이 없으면 그 작물 일지를 `EMPTY`로 판정하고, 그런 일지밖에 없으면
콜백을 `status: "EMPTY"`로 낮춰 보냅니다. **판별 기준은 콜백 본문이 아니라 저장되는 일지(`result.diaries[]`)**
이며, 이 경우 요약도 만들지 않습니다. 상태값만 보고 처리하시면 됩니다.

`empty_reason` (EMPTY 일 때만 포함):

| empty_reason | 뜻 |
|---|---|
| `NO_AUDIO` | 통화에 녹음이 하나도 없음 |
| `NO_TRANSCRIPT` | 전사에 발화가 없음(무음) |
| `NO_CONTENT` | 통화 전체에 기록할 내용이 없음(일지는 있으나 요약 생성이 끝내 실패한 드문 경우 포함) |
| `NO_DIARY_CONTENT` | 통화는 정상 처리됐지만 영농일지로 남길 실질 내용이 없음(검수 판정) |

```json
{"call_id": "20260819_Qmf1D0X", "summary_type": "SUMMARY", "status": "EMPTY",
 "empty_reason": "NO_DIARY_CONTENT", "engine_version": "jinong-summary-v1/gemini-3.5-flash"}
```

- 판정이 애매하면 **내용이 있는 쪽(`COMPLETED`)으로 기울입니다** — 실제 기록을 지우는 오판을 피하기 위함입니다.
- 작물별 판정 근거는 `GET /v1/calls/{call_id}`의 `result.diaries[].structured.verify`에서 확인할 수 있습니다.
- 콜백을 놓치셨으면 `GET /v1/calls/{call_id}`의 **`result.summary.markdown`** 또는
  `GET /v1/calls/{call_id}/artifacts/summary` 로 같은 요약을 다시 가져올 수 있습니다.
- `summary_type`은 `SUMMARY` 고정입니다(현재 `KEYWORD`/`ACTION_ITEM`은 보내지 않습니다).
- 중복 제거는 `(call_id, summary_type)` 기준 UPSERT로 처리해 주세요.
- `POST /v1/calls` body의 `callback_url` 필드는 호환을 위해 계속 받지만 **통화 단위 발사에는 쓰지
  않습니다**(날짜별 전용). 통화 단위 agent-callback은 통화요약 콜백으로 대체됐습니다 — 수신 준비가
  끝나는 시점을 알려주시면 전환 시점을 맞추겠습니다.

### 5.2 날짜별 일지 — agent-callback (마스터 ID 알림)

`callback_url`은 `POST /v1/daily-diaries` body로 등록합니다. 수신 라우팅 시 `daily_diary_id` 필드의
존재로 구분하시고, 중복 제거는 `(daily_diary_id, generation_run)` 기준으로 해주세요.

```json
{
  "daily_diary_id": "daily_18_u123_20260819",
  "diary_date": "2026-08-19",
  "status": "COMPLETED",
  "error": null,
  "call_ids": ["20260819_Qmf1D0X", "20260819_Rx2kP9Y"],
  "result_url": "https://jinong-stt-report-generation.jinongservice.co.kr/v1/daily-diaries/daily_18_u123_20260819",
  "generation_run": 1,
  "diaries": [{"prdlst_code": "0804MM", "prdlst_nm": "딸기", "status": "OK",
               "s3_key_md": "agents/voicecall/daily/daily_18_u123_20260819/artifacts/diary/0804MM.md",
               "s3_key_md_internal": "agents/voicecall/daily/daily_18_u123_20260819/artifacts/internal/diary/0804MM.md"}]
}
```

- `diaries[]`는 선택 필드(2026-09-02 추가, `COMPLETED`에만)로 §5.1과 같은 모양의 **S3 키만** 담습니다(daily는 `report` 없음).
  중복 제거 키나 필수 처리에는 넣지 마시고, 미지 필드를 거부하는 설정이면 알려주세요(`CALLBACK_INCLUDE_ARTIFACT_KEYS`).

- 콜백을 받으신 뒤 `GET /v1/daily-diaries/{daily_diary_id}?inline=true`로 `result.diaries[]`를 가져가
  저장하시면 됩니다(`result_url` 조회에도 `AGENT_API_KEY` 인증 필요). 마크다운이 길어져도 콜백이 가볍고
  재조회·재처리가 쉬운 구조입니다.
- **작물(품목) 처리 규칙** — `diaries[]`는 이미 아래대로 동작합니다:
  - 같은 날짜에 작물이 여러 개면 `diaries[]`에 **작물별 1건**씩 담깁니다.
  - **같은 날짜·같은 작물은 여러 통화를 합쳐 1건**으로 생성합니다(통화 2건 이상이어도 일지는 1건).
  - 각 건에 `prdlst_code`(팜스올 품목코드)와 `prdlst_nm`을 함께 담습니다.
  - 통화 1건에서 여러 작물이 언급돼도 작물별 결과로 분리하며, **작물과 `call_id`를 직접 연결하지
    않습니다**. 모든 작물이 같은 `daily_diary_id`를 공유합니다.
  - `farm_access_token`이 없으면 팜스올 작물목록을 못 읽어 `prdlst_code`가 `null`이 됩니다
    (S3 키는 `unresolved`, `unresolved-2` …). 이때는 마크다운만 갱신해 주세요.

## 6. S3 규약 (백엔드 준비 사항)

**입력(녹음)** — 백엔드 MinIO 버킷 `voice-recordings` (2026-08-24 반영 완료):
- 백엔드가 녹음을 MinIO(`https://smart-minio.jinongservice.co.kr`)의 `voice-recordings`에 업로드하고
  참조(`bucket`/`key`)만 전달합니다. 저희는 해당 객체를 **읽기만** 하고 복사하지 않습니다.
- 접근 권한은 MinIO 전용 사용자(`jinong-ai-agents`, `voice-recordings` 읽기 전용 정책)로 이미 구성됐습니다 —
  기존 "IAM principal ARN 별도 전달" 절차는 MinIO 전환으로 폐기. (`/audio` 호출이 `422 S3_ACCESS_DENIED`면
  MinIO 사용자/정책 변경 여부를 저희에게 알려주세요.)
- 크기 제한 200MB. 오디오 포맷 검증은 STT 게이트웨이 소관이며(ogg/wav/m4a 사용 확인됨), 미지원 포맷은
  해당 오디오의 STT 실패로 나타납니다.

**출력(산출물)** — 같은 MinIO의 저희 버킷 `jinong-agri-stt` (2026-08-24 생성, 전용 사용자만 쓰기):

```
agents/voicecall/{call_id}/call.json                        시작/종료 스냅샷
agents/voicecall/{call_id}/stt/{NN}-{hash}.json             STT 원본 응답
agents/voicecall/{call_id}/transcript/merged.json | .md     병합 전사
agents/voicecall/{call_id}/artifacts/diary/{prdlst_code}.md | .json   전달용 일지(.md 는 근거·코드·내부 메타 제거) (미확정: unresolved.*)
agents/voicecall/{call_id}/artifacts/internal/diary/{prdlst_code}.md  근거 포함 정본(내부 저장용)
agents/voicecall/{call_id}/artifacts/report.md | .json                전달용 보고서
agents/voicecall/{call_id}/artifacts/internal/report.md               근거 포함 정본
agents/voicecall/{call_id}/artifacts/result.json            전체 스냅샷 (generation_run·양쪽 마크다운·키 포함)

agents/voicecall/daily/{diary_id}/daily.json                트리거 스냅샷
agents/voicecall/daily/{diary_id}/transcript/merged.json | .md        멀티콜 병합 전사
agents/voicecall/daily/{diary_id}/artifacts/diary/{prdlst_code}.md | .json
agents/voicecall/daily/{diary_id}/artifacts/internal/diary/{prdlst_code}.md
agents/voicecall/daily/{diary_id}/artifacts/result.json     (report 없음)
```

경로 규칙:

- **통화(`{call_id}`)가 최상위 디렉터리** — 한 통화의 STT 원본·병합 전사·일지·보고서가 전부 그 아래에
  모입니다. prefix 하나(`agents/voicecall/{call_id}/`)로 통화 단위 조회·정리가 끝납니다.
- **날짜별 집계는 `daily/{diary_id}/`로 분리** — `diary_id`가 `call_id` 네임스페이스와 충돌하지 않습니다.
- **작물별 일지 파일명은 `{prdlst_code}`** (팜스올 작물 코드). 작물 미확정이면 `unresolved.*`,
  미확정 작물이 여러 건이면 `unresolved-2.*`, `unresolved-3.*` … 로 늘어납니다.
- **prefix는 환경별입니다** — 운영 `agents/voicecall/`, 개발(7013 인스턴스) `agents/voicecall-dev/`. 그 아래 레이아웃은
  두 환경이 완전히 같습니다. 응답·콜백의 키는 항상 그 환경의 prefix를 포함한 전체 키이므로 조합하지 마시고 그대로 쓰세요.
  이후 보이스톡 외 산출물이 생기면 같은 버킷의 `agents/<용도>/`로 나란히 확장하며, 기존 키는 바뀌지 않습니다.
- `artifacts/internal/` 아래는 **근거 포함 정본**(전사 인용·근거 번호·내부 경고·모델 버전 포함)입니다. 농가·컨설턴트
  화면에는 `artifacts/diary/*.md`·`artifacts/report.md`(전달용)를 쓰시고, internal 은 검토·감사·문의 대응용으로만 참조해 주세요.
- API 응답의 `s3_key_md` / `s3_key_json` / `transcript_key` / `result_key`는 **이 버킷
  (`jinong-agri-stt`) 기준 키**입니다 (응답에 버킷명은 포함되지 않습니다).
- 재생성 시 같은 키에 덮어씁니다 — 실행별 이력이 필요하면 `result.json`의 `generation_run`을 참고하세요.
- 직접 S3 조회 대신 §3.6 artifact 엔드포인트 사용을 권장합니다(권한 협의 불필요). **`s3_key_md_internal`로 MinIO를 직접
  읽으시려면 `jinong-agri-stt` 버킷 읽기 권한이 백엔드 계정에 필요합니다** — 현재는 부여돼 있지 않으니(전용 사용자만 쓰기)
  필요하시면 MinIO 관리자와 협의해 주세요. 권한 없이도 `?view=internal`로 같은 본문을 받을 수 있습니다.

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
| 422 | `FARM_MISMATCH` | daily 트리거의 통화들이 서로 다른 농가 소속 — `farm.farm_id` 또는 farmer `(engn_id, user_id)` 복합 키 기준 |
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
  "farm_access_token": "<JWT>"}'

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
- [x] 녹음 읽기 권한 — MinIO 전용 사용자로 구성 완료(2026-08-24, §6)
- [ ] 통화 시작 페이로드에 `farm_access_token`(농가 JWT) 포함
- [ ] 통화 시작 `participants[]`의 farmer 항목에 `engn_id` 포함 (농가 구분은 `engn_id`+`user_id` 복합 키 — §3.1)
- [x] 알림 방식 — 콜백 활성화 완료(2026-08-24): 합의된 `X-API-Key` (§5). 폴링은 안전망으로 유지 권장
- [ ] **통화요약 콜백 수신 준비** — `POST .../voicetalk/public/call-summary-callback` (§5.1). 수신 가능 시점을
      알려주시면 통화 단위 agent-callback 발사를 중단하고 전환합니다
- [ ] 통화요약 콜백 `content` 는 통화 단순요약(불릿 3줄, 100자 안팎) — 별도 컬럼 확장 불필요
- [ ] 영농일지·전사본은 콜백이 아니라 `GET /v1/calls/{call_id}` 로 조회 (§3.5, §5.1)
- [ ] 통화요약 콜백 중복 제거: `(call_id, summary_type)` UPSERT (§5.1)
- [ ] **`status="EMPTY"` 처리** — 템플릿 내용 대신 상태값으로 표시. 사유는 `empty_reason` (§5.1)
- [ ] 통화요약 콜백의 **선택 필드 `speaker_map`** — 무시하거나 저장. 미지 필드를 거부하는 DTO면 알려 주세요 (§5.1)
- [ ] 전사 화면에 화자를 표시하신다면 `segments[].role` 사용 — `unknown`은 "화자 A/B" 그대로 (§3.6.1)
- [ ] 에러 파서: `detail` 문자열(401) / 객체(도메인) / 리스트(422 검증) 모두 처리
- [ ] terminal 후 추가 오디오 발생 시 `/regenerate` 호출 로직 (farmos 조회가 필요하면 body에 새 JWT — §3.4)

날짜별 일지(§3.7)를 쓰시는 경우 추가로:

- [x] `diary_id` 결정론적 생성 규칙 확정 — `daily_{engnId}_{userId}_{yyyyMMdd}` (2026-08-25 백엔드 확정, 예: `daily_18_u123_20260819`)
- [ ] 트리거 시점 결정: 하루 마감 배치 또는 마지막 통화 terminal 콜백 수신 시
- [ ] **트리거·재생성마다 새 농가 JWT 첨부** (통화 때 보낸 토큰은 재사용되지 않음)
- [ ] 멤버 통화 전부 terminal 확인 후 트리거 (`CALLS_NOT_READY` 시 재시도/지연 처리)
- [ ] daily 콜백 수신 라우팅: `daily_diary_id` 필드로 구분, `(daily_diary_id, generation_run)` 중복 제거
