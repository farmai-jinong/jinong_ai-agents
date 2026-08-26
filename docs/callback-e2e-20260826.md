# 통화요약 콜백 연동 확인 (2026-08-26)

> 지농 AI Agent(⑧) ⇄ kafka-gateway 연동 시험. 백엔드 전달용.
> 대상: `POST https://dev.jinongservice.co.kr/voicetalk/public/call-summary-callback`

## 결론 — 연동 정상

실서버(`jinong-stt-report-generation.jinongservice.co.kr`)에서 백엔드 요청 순서를 그대로 태워
**통화 → STT → 영농일지·보고서 생성 → 통화요약 생성 → 콜백 저장까지 전 구간 확인했습니다.**
백엔드 수신 엔드포인트는 실제 통화 ID에 대해 `200 Summary callback saved` 를 반환합니다.

시험 도중 발견해 조치한 사항이 하나 있습니다 — **연구팀 서버의 콜백 API Key가 만료 상태**였습니다.
통화요약·`agent-callback` 양쪽 모두 `401 Invalid API Key` 로 거부되고 있었고(날짜별 영농일지 콜백도 그동안 실패),
공유해 주신 키로 교체해 해소했습니다.

---

## 1. 시험 구성

| 항목 | 값 |
|---|---|
| 에이전트 | 지농서버 배포본 (2026-08-26 통화요약 콜백 반영) |
| STT | 게이트웨이 실서비스 |
| 생성 | `gemini-3.5-flash` (Vertex AI) |
| 스토리지 | MinIO `voice-recordings`(입력) / `jinong-agri-stt`(산출물) |
| 콜백 수신 | 백엔드 dev 실엔드포인트 |

---

## 2. E2E ① — 영농 상담 통화 (요약 생성 경로)

백엔드가 보내는 순서 그대로, 실제 상담 음성(5.4MB)으로 실행했습니다.

| 단계 | 요청 | 응답 |
|---|---|---|
| 1 | `POST /v1/calls` | 201 |
| 2 | `POST /v1/calls/{id}/audio` | 202 |
| 3 | `POST /v1/calls/{id}/end` | 202 |
| 4 | `GET /v1/calls/{id}?inline=false` 폴링 | COMPLETED |
| 5 | 콜백 발사 | (아래) |

- **총 소요 57초** (STT 약 35초 포함), LLM 5회 · 17,294 토큰
- 생성된 통화요약 — 콜백 `content` 로 나가는 본문 그대로입니다:

```
- 주제: 오이 담배가루이 방제 상담
- 조치: 검객 수화제 살포 / 끈끈이 트랩 추가 / 안전사용기준 확인
- 후속: 사흘 뒤 경과 확인
```

- 영농일지: 오이 / `PARTIAL` / 검수 통과 · 컨설팅 보고서 · 전사본 모두 정상 생성
- 산출물 저장 경로: `agents/voicecall/{call_id}/artifacts/summary.md|.json`, `.../diary/*.md|.json`,
  `.../report.md|.json`, `.../result.json`, `transcript/merged.json|.md`

**콜백 결과: `404 Call not found`** — 이 통화의 `call_id` 는 저희가 시험용으로 만든 값이라
kafka-gateway 에 이력이 없습니다. **명세대로 동작한 것**이며, 4xx라 재시도 없이 1회로 종료했습니다.

## 3. E2E ② — 백엔드가 발급한 실제 통화 ID (콜백 저장 경로)

kafka-gateway 에 이력이 있는 통화 2건을 재생성해 콜백을 발사했습니다.

| call_id | 콜백 응답 | `callback_status` |
|---|---|---|
| `1JlFzcXpKTGRgc82hPzjt` | `200 OK` | SENT |
| `CYEThMicUQJugnqMK3m2Z` | `200 OK` | SENT |

두 통화 모두 3~4초짜리 인사 통화(전사 `"아."` 수준)라 영농일지로 남길 내용이 없어
`status: "EMPTY"`, `empty_reason: "NO_DIARY_CONTENT"` 로 전송됐고, 백엔드가 정상 저장했습니다.

---

## 4. 엔드포인트 동작 확인

공유해 주신 키(`019fa22e…`)로 직접 호출한 결과입니다.

| 요청 | 응답 | 판정 |
|---|---|---|
| 실제 통화 ID + `status: COMPLETED` + `content` | `200 Summary callback saved` | 정상 |
| 실제 통화 ID + `status: EMPTY` | `200 Summary callback saved` | 정상 |
| 존재하지 않는 통화 ID | `404 Call not found` | 명세대로 |
| 키 없이 호출 | `401 Invalid API Key` | 명세대로 |
| `call_id` 누락 | `400 call_id is required` | 명세대로 |
| 지원하지 않는 `status` | `400 Unsupported terminal status` | 명세대로 |

> 같은 날 오전 시험에서는 유효한 페이로드가 전부 `500 Failed to save summary callback` 이었으나,
> 오후 재확인 시 해소되어 있었습니다. 이전 시험 결과를 공유받으셨다면 이 문서로 대체해 주세요.

---

## 5. 조치한 사항 — 콜백 API Key 교체

첫 실서버 통화에서 `401 Invalid API Key` 가 확인됐습니다. 서버에 설정돼 있던 이전 키가
**통화요약·`agent-callback` 양쪽 모두에서 거부**되고 있었습니다 — 날짜별 영농일지 완료 알림도
그동안 전달되지 않았다는 뜻입니다.

- 공유해 주신 키로 교체 후 재기동 → 이후 콜백 `200` 확인
- 키는 두 콜백이 공유하므로 날짜별 `agent-callback` 도 함께 정상화됐습니다
- 앞으로 키를 교체하실 때는 미리 알려주시면 저희 쪽 설정을 맞추겠습니다

---

## 6. 아직 확인하지 못한 조합

**실제 영농 상담 내용이 담긴 백엔드 통화**로 `status: COMPLETED` + `content` 를 저장하는 흐름은
아직 한 번에 확인하지 못했습니다. 백엔드 dev 에 남아 있는 통화가 전부 3~30초짜리 테스트 통화라
영농일지로 남길 내용이 없기 때문입니다.

각 조각은 검증됐습니다 — 요약 생성·전송은 §2에서, 백엔드의 `COMPLETED`+`content` 수락은 §4에서 확인.
**실제 상담 통화를 한 건 태워 주시면 이 조합까지 마무리됩니다.**

---

## 7. 참고 — 바뀐 콜백 계약

이번에 `content` 의 내용이 바뀌었습니다. 수신 코드 작성 시 참고해 주세요.

- **`content` 는 통화 내용의 단순요약**입니다 — 주제/조치/후속 불릿 3줄, 대체로 100자 안팎.
  영농일지·컨설팅 보고서 마크다운은 싣지 않습니다. 별도 컬럼 확장이 필요하지 않은 분량입니다.
- 요약은 일지를 요약한 것이 아니라 **녹취문에서 직접 뽑습니다** — 일지 서식이 바뀌어도 요약 문구는 영향을 받지 않습니다.
- **이 콜백 도착이 곧 결과 준비 완료 신호**입니다. 받으신 뒤 `GET /v1/calls/{call_id}` 로 일지·보고서·전사본을
  가져가시면 됩니다. 통화 단위 `agent-callback` 은 더 이상 발사하지 않습니다.
- `engine_version` 은 `jinong-summary-v1/{모델명}` 형식입니다.
- `empty_reason`(EMPTY 일 때만) — `NO_AUDIO` · `NO_TRANSCRIPT` · `NO_CONTENT` · `NO_DIARY_CONTENT` · `NO_SUMMARY`
  - `NO_DIARY_CONTENT` 는 통화는 정상 처리됐으나 영농일지로 남길 실질 내용이 없는 경우입니다
    (안부·잡담·일정조율 등). 생성된 일지 초안을 별도 LLM 패스가 한 번 더 검수해 판정합니다.
- 중복 제거는 `(call_id, summary_type)` 기준 UPSERT 로 처리해 주세요. 재생성 시 같은 키로 덮어씁니다.
- 재시도는 최대 3회(10초·30초 대기)이며, **4xx(429 제외)는 재시도하지 않습니다.**
- 날짜별 영농일지는 종전대로 `agent-callback`(마스터 ID 알림)을 요청 body 의 `callback_url` 로 보냅니다 — 변경 없습니다.

### 콜백을 놓치셨을 때

콜백 실패는 통화 처리에 영향을 주지 않습니다. 통화는 `COMPLETED` 로 확정되고 산출물도 모두 저장되므로
`GET /v1/calls/{call_id}` 로 언제든 다시 가져가실 수 있습니다.

| 항목 | 조회 경로 |
|---|---|
| 통화요약 | `result.summary.markdown` 또는 `GET /v1/calls/{id}/artifacts/summary` |
| 영농일지 | `result.diaries[]` (작물별 1건, `prdlst_code`·`prdlst_nm` 포함) |
| 컨설팅 보고서 | `result.report` |
| 전사본 | `GET /v1/calls/{id}/transcript` |

상세 계약은 연동 명세(`integration-handoff.md`) §3.5 · §5.1 참조.
