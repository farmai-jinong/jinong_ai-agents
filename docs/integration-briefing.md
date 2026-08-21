# 백엔드 연동 브리핑 (내부용)

> kafka-gateway(팜스올 보이스톡) 백엔드 팀에 우리 쪽 설계를 설명하기 전에 읽는 자료.
> 전달용 명세는 [integration-handoff.md](integration-handoff.md) — 그 문서를 그대로 넘기면 된다.
> 필드 단위 정본은 [api-reference.md](api-reference.md), 내부 동작은 [architecture.md](architecture.md).

## 1. 우리가 뭐 하는 컴포넌트인가 (한 문단 요약)

지농 AI 시스템의 **⑧ 서비스 앱 / AI Agent**. 팜스올 보이스톡(농가↔컨설턴트 통화)의
시작/녹음/종료 **이벤트**를 백엔드(kafka-gateway)로부터 HTTP로 받아, 녹음을 ⑥ 게이트웨이 STT로
전사하고, LangGraph 파이프라인으로 **작물별 영농일지 초안(markdown) + 컨설팅 보고서 초안(markdown)**을
만든다. 모델·GPU를 직접 들고 있지 않고(전부 게이트웨이/외부 LLM 경유), **farmos에는 절대 쓰지 않는다** —
농가 JWT로 읽기만 하고, 앱이 `PUT /m/diary`에 쓸 수 있는 모양의 `prefill` 초안을 돌려줄 뿐이다.
최종 저장은 농가가 앱에서 확인 후 직접 한다.

## 2. 데이터 흐름 (설명 순서대로)

```
kafka-gateway ──(1) POST /v1/calls (통화 시작: call_id, 참가자, 농가 JWT, callback_url)
              ──(2) POST /v1/calls/{id}/audio ×N (녹음 S3 bucket/key 참조 — 바이트 전송 아님)
              ──(3) POST /v1/calls/{id}/end (통화 종료)
agents        ──(2 직후부터) 게이트웨이 STT로 전사 (통화 중에도 병렬 진행)
              ──(3 이후, 모든 STT 완료 시) LLM 생성 → S3(agents/voicecall/…)에 산출물 저장
              ──(terminal) 콜백 POST 또는 백엔드가 GET /v1/calls/{id} 폴링
앱            ── 결과의 diaries[].structured.prefill 을 농가가 확인 → 앱이 farmos에 저장
```

설명할 때 포인트: **오디오 바이트는 우리 API를 지나지 않는다.** 백엔드가 S3에 올리고 참조(bucket/key)만
보낸다. 우리는 HEAD로 검증하고 읽기만 하며, 우리 산출물은 전부 `jinong-agri-stt` 버킷의
`agents/voicecall/{call_id}/` 아래에만 쓴다.

**날짜별(멀티콜) 영농일지** — 하루에 통화가 여러 건이면 백엔드가 통화들 terminal 후
`POST /v1/daily-diaries`(diary_id + diary_date + call_ids[] + 새 JWT)로 **명시적으로 트리거**한다.
우리가 전사를 시간순으로 합쳐 하나의 날짜별 일지(작물별)를 만든다. 통화별 플로우와 별도 리소스로
공존하고, 자동 트리거는 없다(언제 묶을지는 백엔드 결정 — 하루 마감 배치 또는 마지막 콜백 수신 시).

## 3. 상태 머신 요약 (질문 나올 부분)

- 2축이다: `state`(OPEN→ENDED, `/end`만이 전이, 편도)와 `status`(NONE→PROCESSING→COMPLETED|EMPTY|FAILED).
- **`/end`가 STT 진행 중에 와도 안전하다.** 생성은 "ENDED + 대기 중 STT 0건"이 될 때 자동 발화하므로,
  백엔드는 STT 완료를 기다렸다가 `/end`를 보낼 필요가 없다. 이벤트가 생기는 즉시 쏘면 된다.
- 프로세스 재시작 시 자동 복구(TRANSCRIBING→PENDING 등) — 배포 후 백엔드가 할 일 없음.
- terminal 사유는 `error.code`로 구분: `NO_AUDIO`(오디오 0건→EMPTY), `NO_TRANSCRIPT`, `NO_CONTENT`,
  `STT_FAILED`(전부 실패), `STT_TIMEOUT`(종료 후 1h 데드라인), `GENERATION_FAILED`.
  일부 오디오만 STT 실패하면 나머지로 생성하고 `generation.warnings`에 남긴다.

## 4. 백엔드에 설명할 때 꼭 강조할 함정 9개

1. **콜백은 이중 스위치다.** 콜별 `callback_url`(start 바디) + 우리 서버 env `CALLBACK_ENABLED=true`
   (기본 **false**) 둘 다 있어야 발사된다. 백엔드가 콜백 방식으로 가면 go-live 전에 우리가 env를 켜야 하고,
   그전까지는 폴링만 동작한다. → 미팅에서 방식(콜백 vs 폴링) 결정할 것.
2. **콜백은 at-least-once, 무서명.** 최대 3회 시도(시도 간 10s·30s), 2xx면 성공. regenerate마다
   다시 발사된다. 백엔드는 `(call_id, generation_run)`으로 dedupe하고, 폴링을 안전망으로 둬야 한다.
3. **토큰 purge ↔ regenerate 함정.** terminal 되면 농가 JWT를 DB에서 소거하고, terminal 상태에선
   `POST /v1/calls` 업서트가 short-circuit(아무것도 안 바꿈)이다. 재생성에 farmos 조회를 태우려면
   **`/regenerate` body에 새 JWT를 직접** 넣는다(daily 와 동일 계약, 2026-08-21부터). 생략하면
   hints-only(전사+힌트만)로 재생성된다. (기존 '2단계 순서(regenerate 후 /v1/calls 업서트)' 안내는
   워커 즉시 기동과 경합이 있어 폐기.)
4. **에러 파서는 두 모양을 다 받아야 한다.** 401은 `{"detail":"invalid or missing API key"}`(문자열),
   도메인 에러는 `{"detail":{"code","message"}}`(객체), 검증 오류는 FastAPI 기본 422 리스트.
5. **terminal 이후 늦게 도착한 오디오는 자동 재처리되지 않는다.** 큐잉만 하고 `stale:true` 표시 —
   결과에 반영하려면 백엔드가 명시적으로 `/regenerate`를 불러야 한다.
6. **`audio`가 `start`보다 먼저 와도 404가 아니다.** 통화를 자동 생성한다(기본 켜짐). 단 그 시점엔
   참가자·JWT가 없는 통화이므로, `start` 이벤트가 결국 도착해야 정상 생성이 된다. 이벤트 순서가 꼬여도
   전송을 포기하지 말라고 안내할 것.
7. **daily 트리거·재생성마다 새 JWT를 첨부해야 한다.** 통화 때 보낸 토큰은 통화 terminal 시 purge되어
   재사용되지 않는다. daily 도 통화와 같이 트리거/`regenerate` **body에 직접** 넣으면 되고,
   순서 곡예가 필요 없다. 생략하면 hints-only 생성(실패 아님).
8. **daily는 멤버 통화가 전부 terminal이어야 트리거된다.** `NONE`/`PROCESSING`/`FAILED`가 섞이면
   `409 CALLS_NOT_READY` — `FAILED` 통화는 먼저 call `/regenerate`로 살려야 한다. 언제 묶어 쏠지는
   백엔드 책임(하루 마감 배치 vs 마지막 콜백 수신 시)이므로 미팅에서 트리거 시점을 정할 것.
9. **daily 병합 전사의 시간축은 통화 길이 누적이다.** 통화 사이의 실제 공백(오전 통화↔오후 통화 간격)은
   타임스탬프에 표현되지 않는다. 원본 통화 식별은 `files[].call_id`로 한다. 그리고 daily 결과에는
   **컨설팅 보고서가 없다**(일지만) — 보고서 기대치를 미리 맞춰둘 것.

## 5. 백엔드가 우리에게 제공/준비해야 하는 것 (미팅 체크리스트)

- [ ] **통화 시작 페이로드에 농가 JWT**(`farm_access_token`) 포함 — 없으면 farmos 미조회, 전사+힌트만으로 생성.
- [ ] **녹음 S3 업로드 후 bucket/key 전달** + 그 버킷/prefix에 대해 **우리 IAM에 `s3:GetObject`·`s3:HeadObject` 권한** 부여.
  (`/audio`에서 422 나면 십중팔구 이 권한 문제.)
- [ ] 파일 크기 ≤ **200MB**(초과 시 422 AUDIO_TOO_LARGE). 포맷은 게이트웨이 STT 소관(ogg/wav/m4a 확인됨).
- [ ] **콜백 수신 엔드포인트**(선택) — 우리가 `X-API-Key: <CALLBACK_API_KEY>` 헤더로 POST. 키 값 합의 필요.
- [ ] **`AGENT_API_KEY` 수령** — 우리가 발급(콤마 구분 다중 키 가능, 클라이언트별 발급/폐기). 전 요청 헤더 필수.
- [ ] 방식 결정: **콜백 vs 폴링**(권장 폴링 주기 5s, `?inline=false`). 콜백이면 우리 쪽 `CALLBACK_ENABLED` 플립 일정 합의.
- [ ] `call_id`는 kafka-gateway `callId` 그대로(`[A-Za-z0-9_.:-]{1,128}`) — 우리 쪽 기본키.
- [ ] (daily 쓰는 경우) **`diary_id` 결정론적 생성 규칙 합의**(권장 `daily_{farmerId}_{yyyyMMdd}` — 멱등성 키)
  + 트리거 시점(마감 배치 vs 마지막 콜백) + 같은 농가 통화만 묶는 책임은 백엔드에 있음을 안내.

## 6. 우리가 백엔드에 돌려주는 것

- **동기 응답**: 모든 이벤트는 즉시 200/201/202 (처리는 백그라운드). `AudioAck`/`CallDetail`로 진행 상황(`stt_progress`) 확인 가능.
- **terminal 알림**: 콜백 페이로드 `{call_id, status, error, result_url, generation_run}` 또는 폴링.
- **결과**(`status=COMPLETED`일 때 `result`):
  - `diaries[]` — 통화에서 다룬 **작물별** 영농일지 초안. `prdlst_code`(farmos 품목코드, 확정 불가 시 null/`unresolved`),
    markdown 본문, `structured.prefill`(= 앱 `PUT /m/diary`의 `fields` 모양 초안), 건별 상태 `OK|PARTIAL|EMPTY|UNRESOLVED_CROP`.
  - `report` — 컨설팅 보고서 초안(markdown + structured: summary/keywords/action_items/sections).
  - 병합 전사(`/transcript`), 산출물 md/json 직접 조회 엔드포인트, S3 키(우리 버킷 `jinong-agri-stt` 기준).
  - 본문이 512KB를 넘으면 인라인 생략(S3 키 또는 artifact 엔드포인트로 조회).
- **날짜별 일지**(daily, 트리거한 경우): `result.diaries[]`만 있고 **`report` 없음**. 콜백 페이로드는
  `call_id` 대신 `{daily_diary_id, diary_date, status, error, call_ids, result_url, generation_run}` —
  수신 측은 `daily_diary_id` 존재로 통화 콜백과 구분해 라우팅해야 한다.
- **보안 보장**: `farm_access_token`은 어떤 응답·로그에도 노출되지 않고 terminal 시 소거. farmos에는 GET만 한다.
