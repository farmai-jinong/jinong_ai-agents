# [백엔드 안내] 재생성 시 백엔드 DB 직접 조회 — 첫 생성 vs 재생성 데이터 출처

> 대상: 팜스올 보이스톡 백엔드(kafka-gateway, 브랜치 `livekit`)
> 작성: 2026-09-30 · 적용: **dev 전용**(`BACKEND_DB_URL` 설정 시에만 동작, prod 는 비활성) · 커밋 `9b29fc0`
> 관련: `docs/architecture.md`(백엔드 DB 갱신) · `docs/ops.md` §0·§7 · `docs/integration-briefing.md`(읽기 전용 롤) · `docs/backend-update-2026-09-16.md`

## 0. 한 줄 요약

1. **첫 생성(run 1)은 지금과 같습니다** — 통화 등록 payload(`participants`·`metadata.hints`) → farmos(농가 JWT) →
   AP research API(`/voicetalk/public/research/farm-context`) → hints 순으로 농가·작물을 봅니다.
2. **STT 시작 시와 재생성(run ≥ 2) 시에는 백엔드 PostgreSQL 을 읽기 전용으로 직접 조회**해 참여자·농가 등록 작물을
   최신화한 뒤 생성합니다. 통화 이후 백엔드에서 바뀐 참여자·등록 작물이 재생성에 반영됩니다.

**API 요청·응답 모양은 바뀌지 않습니다.** 백엔드 쪽 코드 변경은 필요 없고, §4 의 DB 롤 발급만 요청드립니다.

## 1. 언제 백엔드 DB 를 읽나

| 시점 | 트리거 | 백엔드 DB 조회 |
|---|---|---|
| 통화 첫 생성 | `POST /v1/calls` → `/audio` → `/end` | ❌ (payload 스냅샷 + 기존 순서) |
| STT 잡 시작 | 녹음 수신 후 STT 시작 | ✅ 참여자·등록 작물 갱신 |
| 통화 재생성 | `POST /v1/calls/{id}/regenerate` (run ≥ 2) | ✅ 갱신 + 작물 조회 1순위 |
| 날짜별 일지 첫 생성 | `POST /v1/daily-diaries` (새 `diary_id`) | ❌ |
| 날짜별 일지 재생성 | **같은 `diary_id`** 재-POST, `POST /v1/daily-diaries/{id}/regenerate` (run ≥ 2) | ✅ 멤버 통화 전부 갱신 + 작물 조회 1순위 |

재생성 시 작물 조회 순서: **백엔드 DB** → (비었거나 실패하면) farmos → AP research API → hints.

## 2. 무엇을 읽고 무엇을 바꾸나

읽는 테이블(SELECT 만):

| 테이블 | 용도 |
|---|---|
| `voicetalk.tb_voice_talk_history` | `call_id` 로 발신/수신자 `engn_id`·`user_id` |
| `smartfarm.tb_user` | 이름(`user_nm`), 역할(`user_job_secode = 001001004` → 컨설턴트, 다른 쪽 → 농가) |
| `smartfarm.tb_frmhs` / `tb_frlnd` / `tb_frlnd_prdlst` | 농가 등록 작물 — 백엔드 `findUserPrdlstList` 와 같은 조건, 코드별 1행(대표 여부 포함) |
| `smartfarm.tb_stdr_prdlst` | 표준 품목(M 그룹) 코드·이름 |

갱신 대상(연구팀 DB 안에서만):

- `participants` — 역할·이름·`engn_id`/`user_id` (양쪽 다 컨설턴트이거나 둘 다 아니면 갱신하지 않고 payload 값 유지)
- `num_speakers` — 비어 있을 때만
- `metadata.hints` — `farmer_engn_id`·`farmer_user_id`·`farmer_crops` 만 덮어씀(`prdlst_code`·`diary_date` 등 기존 힌트는 유지)
- **바꾸지 않는 것**: `farm`, `farm_access_token`, 작물 고정 `crop`

실패(DB 접속 불가·통화 행 없음·등록 작물 없음)는 **fail-open** — 기존 스냅샷으로 그대로 생성합니다. 조회 결과는
`GET /v1/calls/{id}`·`GET /v1/daily-diaries/{id}` 응답의 `metadata.backend_refresh` 에 남습니다.

```json
"metadata": {
  "hints": {"farmer_engn_id": "1", "farmer_user_id": "test7",
            "farmer_crops": [{"prdlstCode": "1326MM", "prdlstNm": "파프리카", "reprsntPrdlstCnt": 1}, "…"]},
  "backend_refresh": {"reason": "regenerate", "found": true, "changed": ["participants", "hints"],
                      "crops": 3, "warnings": [], "at": "2026-09-30T…"}
}
```

## 3. 주의 — 적용되지 않는 경로

1. **품목 변경(`PUT /m/aidiary`)은 해당 없음.** 작물이 바뀌면 규칙상 새 `diary_id`(`…_{prdlstCode}` / `…_nocode`)로
   보내므로 run 1(첫 생성)로 처리되고 백엔드 DB 를 읽지 않습니다. 같은 `diary_id` 로 다시 요청할 때만 DB 를 읽습니다.
2. **웹에서 수정한 STT 전사·일지 본문은 읽지 않습니다.** 재생성도 연구팀에 저장된 원본 전사를 사용합니다.
   "STT 수정 후 재분석"은 별도 작업으로 협의가 필요합니다.
3. **dev 전용**입니다. prod 는 `BACKEND_DB_URL` 을 비워 두어 기존 동작 그대로입니다.

## 4. 백엔드 요청 사항

- [ ] **백엔드 PostgreSQL 읽기 전용 롤 발급** — 현재는 기존 계정으로 세션 읽기 전용(`default_transaction_read_only=on`)
  설정을 걸어 SELECT 만 하고 있습니다. §2 의 6개 테이블에 SELECT 권한만 있는 롤이면 충분합니다.
  경로: 지농서버(172.31.10.137) → 172.31.1.109:25432.
- [ ] 위 테이블의 컬럼명·조인 조건이 바뀌면 미리 알려주세요(§2 조회가 fail-open 으로 조용히 빠집니다).
