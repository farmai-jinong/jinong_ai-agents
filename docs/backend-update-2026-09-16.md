# [백엔드 안내] 작물 고정 영농일지 API + 통화 전사에 판정 작물 동봉

> 대상: 팜스올 보이스톡 백엔드(kafka-gateway, 브랜치 `livekit`)
> 작성: 2026-09-16 · 적용: 운영 `https://jinong-stt-report-generation.jinongservice.co.kr` (§4 실측)
> 관련: `docs/integration-handoff.md` §3.6.2 · §3.7.1 · §8 · §9 / `docs/api-reference.md`

## 0. 한 줄 요약

1. **작물·날짜 고정 영농일지** — `POST /v1/daily-diaries` 에 `crop` 을 주면 자동 판정 없이 **그 작물 일지 1건**만
   만듭니다(날짜는 기존처럼 `diary_date` 고정). 농가가 앱에서 작물·날짜를 고르는 화면용입니다.
2. **통화 전사에 판정 작물 동봉** — `GET /v1/calls/{id}/transcript` 응답(및 daily `/transcript`)에 `crops[]` 가
   추가됩니다. 기존 필드는 그대로이며 **필드 하나가 추가**될 뿐입니다.

둘 다 하위호환입니다(새 필드는 선택, 기존 요청·응답 모양 불변). 배포는 2026-09-16 운영 반영 완료(§4).

## 1. 작물 고정 영농일지 — `POST /v1/daily-diaries` + `crop`

### 1.1 요청

```json
{
  "diary_id": "daily_18_u123_20260916_0804MM",
  "diary_date": "2026-09-16",
  "call_ids": ["20260916_Qmf1D0X", "20260916_Rx2kP9Y"],
  "farm_access_token": "<농가 JWT>",
  "callback_url": "https://<backend>/voicetalk/public/agent-callback",
  "crop": {"prdlst_code": "0804MM", "prdlst_nm": "딸기"}
}
```

| 필드 | 규칙 |
|---|---|
| `crop` | 선택. **있으면 작물 고정 모드**, 없으면 지금과 같은 자동 판정 |
| `crop.prdlst_code` | 팜스올 품목코드, `[A-Za-z0-9_.:-]{1,64}`. 있으면 이걸 우선 사용 |
| `crop.prdlst_nm` | 작물명 1..64자. 코드가 없으면 이름으로 농가 등록 작물 → 표준 품목 순으로 코드를 찾습니다 |
| 둘 다 없음 (`{}`) | `422`(스키마 검증 리스트) |
| `diary_id` | **자동 배치와 다르게** — 권장 `daily_{engnId}_{userId}_{yyyyMMdd}_{prdlstCode}`. ASCII 만 허용되므로 코드 없이 이름만 보낼 땐 ASCII 대체 토큰(예: `_nocode`) |
| 나머지 | `call_ids`·`farm_access_token`·`callback_url`·`metadata` 규칙은 §3.7 그대로 (멤버 통화 전부 terminal, 1건 이상 COMPLETED, 같은 농가) |

### 1.2 동작

- 지정한 작물 **1건만** 생성합니다. 통화에서 다른 작물이 언급돼도 그 작물 일지는 만들지 않습니다.
- **다른 작물로 명시된 내용은 뺍니다.** 예: 딸기로 고정했는데 파프리카 병해 얘기가 있으면 그 항목은 제외하고
  `generation.warnings` 에 `"파프리카 관련 항목 3건 제외(작물 고정: 딸기)"` 로 남깁니다. 작물이 특정되지 않은 내용
  (관수·환기 등)과 후속·조치는 고정 작물 일지에 들어갑니다.
- 코드가 없으면 저희가 채웁니다(농가 등록 작물 → 표준 품목). 못 찾으면 `prdlst_code: null`(S3 키 `unresolved`).
  농가 등록 작물이 아니면 일지 메타 표에 `(미등록 작물)` 이 붙습니다.
- `metadata.hints.prdlst_code` 와 같이 오면 `crop` 이 우선합니다.

### 1.3 응답·조회

`DailyDiaryDetail` 에 `crop` 이 추가됩니다(요청값 정규화, 자동 모드는 `null`). `result.diaries` 는 항상 **1건**.

```json
{
  "diary_id": "daily_18_u123_20260916_0804MM", "diary_date": "2026-09-16", "status": "COMPLETED",
  "call_ids": ["20260916_Qmf1D0X"], "crop": {"prdlst_code": "0804MM", "prdlst_nm": "딸기"},
  "generation": {"run": 1, "warnings": ["파프리카 관련 항목 2건 제외(작물 고정: 딸기)"], "...": "..."},
  "result": {"diaries": [{"prdlst_code": "0804MM", "prdlst_nm": "딸기", "diary_date": "2026-09-16",
                          "status": "PARTIAL", "markdown": "> 📝 **통화 요약** · …", "...": "..."}],
             "transcript_key": "…", "result_key": "…"}
}
```

조회·산출물·`/regenerate`·콜백(§5.2)은 자동 모드와 동일합니다.

### 1.4 재-POST 규칙 — `crop` 은 불변

| 상황 | 결과 |
|---|---|
| 같은 `diary_id`, 같은 `crop` (또는 `crop` 생략) | 기존과 동일 — 새 생성 회차(`200 regeneration queued` 등) |
| 같은 `diary_id`, **다른** `crop` | `422 CROP_MISMATCH` (RUNNING 이어도) — 작물이 바뀌면 새 `diary_id` |
| 자동 모드로 만든 `diary_id` 에 `crop` 추가 | `422 CROP_MISMATCH` (모드 전환도 새 `diary_id`) |

`CROP_MISMATCH` 는 백엔드의 `diary_id` 생성 규칙이 어긋났다는 신호로 취급해 주세요.

### 1.5 빈 결과 두 가지 (둘 다 정상 종료)

| 경우 | 응답 |
|---|---|
| 통화 내용이 전부 다른 작물이라 쓸 게 없음 | `status: COMPLETED` + `result.diaries[0].status: "EMPTY"` (빈 골격 마크다운) |
| 통화가 잡담뿐 | `status: EMPTY`, `error.code: NO_CONTENT` (일지 없음) |

## 2. 통화 전사에 판정 작물 동봉 — `crops[]`

```json
// GET /v1/calls/{id}/transcript  (생성 완료 후)
{"call_id": "…",
 "speaker_map": {"f0:A": "consultant", "f0:B": "farmer"},
 "crops": [{"prdlst_code": "0804MM", "prdlst_nm": "딸기", "status": "PARTIAL"},
           {"prdlst_code": "1326MM", "prdlst_nm": "파프리카", "status": "PARTIAL"}],
 "segments": [{"speaker_key": "f0:A", "role": "consultant", "text": "…", "...": "..."}],
 "files": [ … ], "speakers": [ … ], "total_duration_sec": 0, "text": "…"}
```

| 필드 | 값 |
|---|---|
| `crops[].prdlst_code` | 팜스올 품목코드. 확정 못 하면 `null` |
| `crops[].prdlst_nm` | 작물명 |
| `crops[].status` | 그 작물 일지의 상태 `OK` \| `PARTIAL` \| `EMPTY` \| `UNRESOLVED_CROP` |

- **`GET /v1/calls/{id}` 의 `result.diaries[]` 와 순서·값이 항상 같습니다**(같은 생성 결과에서 씁니다).
- 생성 **전**(STT 직후) 조회, `EMPTY`/`FAILED` 통화는 **`[]`** 입니다. 화자 `role` 과 같은 시점(생성 완료)에 채워집니다.
- 날짜별 일지 `GET /v1/daily-diaries/{diary_id}/transcript` 에도 같은 모양으로 들어갑니다(작물 고정이면 1건).
- 콜백 페이로드에는 싣지 않습니다(통화요약 콜백의 선택 필드 `diaries[]` 가 이미 코드·이름·상태를 담습니다).
- 전사 JSON DTO 가 미지 필드를 거부하도록 돼 있으면 `crops` 를 추가해 주세요.

## 3. 백엔드 체크리스트

- [ ] 작물 고정 화면: `POST /v1/daily-diaries` 에 `crop` 동봉, `diary_id` 에 작물 코드 포함(`daily_{engnId}_{userId}_{yyyyMMdd}_{prdlstCode}`)
- [ ] `422 CROP_MISMATCH` 처리(작물 변경 = 새 `diary_id`)
- [ ] 응답 `crop` 필드·`result.diaries` 1건 전제, `diaries[0].status = "EMPTY"` 와 daily `EMPTY/NO_CONTENT` 화면 처리
- [ ] 전사 DTO 에 `crops[]` 추가(또는 무시 허용)
- [ ] 필요하면 전사 화면에 판정 작물 표시 — `result.diaries[]` 와 동일하니 어느 쪽을 써도 됨

## 4. 운영 실측 (2026-09-16, prod :7003)

_(배포 후 채움)_
