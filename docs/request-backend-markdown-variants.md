# [백엔드 요청] 영농일지 본문 두 벌(근거 포함 / 미포함) 구분 저장

> 대상: 팜스올 보이스톡 백엔드(kafka-gateway, 브랜치 `livekit`)
> 작성: 2026-09-08 · 근거: 운영 :7003 실측 + `livekit` 코드(`c42be601`) 대조
> 관련: `docs/api-reference.md` §콜백 / `docs/integration-handoff.md` §5.1.1

## 0. 한 줄 요약

저희가 영농일지를 **근거 포함(internal)** / **근거 미포함(public)** 두 벌로 렌더해 두는데, 백엔드는
`GET /v1/calls/{id}?inline=true` 를 `view` 없이 호출하고 있어 **운영에서 근거 포함본이 저장되고 농가 앱에
그대로 노출됩니다.** 저희가 기본값을 바꾸면 바로 잘리지만, 그러면 백엔드가 근거를 **아예 못 받게** 되므로
어느 쪽을 쓸지·둘 다 보관할지 결정이 필요합니다. **요청안은 §4의 A안(`view=public` 명시)** 입니다.

## 1. 지금 무슨 일이 일어나고 있나

운영 통화 `a6aa25b6-61bb-4f07-9682-1f1e9e2ba6e0`(2026-09-08) 실측입니다.

백엔드는 `ResearchAiSttClient.fetchAndSaveResultState()` 에서 이렇게 호출합니다:

```java
// ResearchAiSttClient.java:352
String url = getBaseUrl() + "/v1/calls/" + callId + "?inline=true";   // view 파라미터 없음
...
// ResearchAiSttClient.java:611
.markdownContent(cd.getMarkdown())                                    // 받은 그대로 단일 컬럼에 저장
```

`view` 가 없으면 저희 서버 설정(`API_MARKDOWN_VIEW`)이 적용되는데, **운영은 `internal`** 입니다.
그래서 백엔드가 실제로 받아 저장한 값은 이렇습니다:

| 작물 | 저장된 bytes | `(근거: #N)` | `## 근거 발화` 절 | 통화 ID | 모델·프롬프트 버전 |
|---|---|---|---|---|---|
| 딸기 | 2,663 | 포함 | 포함 | 포함 | 포함 |
| 파프리카 | 2,270 | 포함 | 포함 | 포함 | 포함 |

`markdownContent` 는 `VoiceTalkDiary` 단일 컬럼이고, 그대로 `AiDiaryListResponseDTO` ·
`AiDiaryHistoryResponseDTO` · `PutAiDiaryDTO` 를 타고 **농가 앱 화면까지** 갑니다.

### 두 벌이 실제로 어떻게 다른가

같은 통화, 같은 구조화 데이터에서 렌더한 두 벌입니다(LLM 재호출 없음).

**`view=public` — 1,506 bytes**
```markdown
| 항목 | 값 |
|---|---|
| 작물 | 딸기 |
| 생육단계 | 정보 없음 |

## 주요 농작업
- [ ] 난방 (표준 목록 조회 불가 — 앱에서 체크 필요)

## 병해충
- (예방 언급) 흰가루병
...
_AI 초안 — 농가 확인 후 저장. … 생성: 2026-09-08 09:03_
```

**`view=internal` — 2,663 bytes** (차이 나는 부분만)
```markdown
| 작물 | 딸기 (0804MM) |                                          ← 작물 코드
| 통화 | a6aa25b6-61bb-4f07-9682-1f1e9e2ba6e0 (…) |               ← 통화 ID

## 주요 농작업
- [ ] 난방 (표준 목록 조회 불가 — 앱에서 체크 필요) (근거: #12)   ← 인라인 근거

## 근거 발화                                                       ← 절 전체가 추가
- `#10` [01:01–01:10] 농가: "어제 딸기 런너 정리랑 마른잎 딸기는 마쳤고요. …"
- `#11` [01:10–01:20] 컨설턴트: "아 어 요즘 밤 기온이. 뚝 떨어져가지고 …"
- `#12` [01:21–01:26] 농가: "네 딸기 온 온풍기 기름 채우고 야간 온도 십도 밑으로 …"

## 참고
- 없음

_… 생성: 2026-09-08 09:03, 모델 gemini-3.5-flash, 프롬프트 v2_        ← 모델·프롬프트 버전
```

`public` 이 빼는 것은 **`(근거: #N)` 인라인 표기 · `## 근거 발화` 절 · `## 참고` 절 · 작물 코드 · 통화 ID ·
모델/프롬프트 버전** 여섯 가지입니다. 판정 문구(`[표준 목록 미매핑]`, `※ 확인 필요`)와 동의서 §8 안내
문구는 **양쪽 모두 유지**됩니다 — 농가가 확인해야 할 정보는 지우지 않습니다.

`## 근거 발화` 는 **통화 원문을 그대로 인용**합니다. 화자 구분과 발화 시각이 함께 붙습니다.

## 2. 왜 지금까지 이렇게 뒀나 (저희 쪽 경위)

두 벌 렌더는 2026-09-07 에 들어갔고, 그때 운영 `.env` 를 일부러 `API_MARKDOWN_VIEW=internal` +
`CALLBACK_INCLUDE_ARTIFACT_KEYS=false` 로 두었습니다. **백엔드가 두 벌 전환을 받기 전까지 응답 본문과
콜백 payload 를 이전과 100% 동일하게 유지**하려는 의도였습니다(무전환 배포). 즉 지금 상태는 사고가 아니라
"백엔드와 합의 전까지 기존 동작 보존"이고, 이 문서가 그 합의를 요청하는 것입니다.

## 3. 호환성 확인 결과 (저희가 먼저 확인했습니다)

**Q. agent-callback payload 에 두 벌 정보를 실어 보내면 되나?**
**A. 보내도 백엔드가 깨지지는 않지만, 아무 효과가 없습니다.** 그래서 콜백만으로는 해결되지 않습니다.

확인한 내용:

1. **미지 필드는 안전합니다.** `JinongAgentResponseDTO` 의 9개 내부 클래스 전부
   (`CallDetail` · `CallResult` · `CropDiary` · `AgentCallbackPayload` 등)에
   `@JsonIgnoreProperties(ignoreUnknown = true)` 가 붙어 있습니다. `ResearchAiSttClient.java:72` 가
   `new ObjectMapper()`(= `FAIL_ON_UNKNOWN_PROPERTIES` 기본 true)를 직접 쓰지만, 이 애너테이션이
   클래스 단위로 덮으므로 저희가 필드를 늘려도 파싱은 깨지지 않습니다.
2. **그러나 받을 그릇이 없습니다.** `CropDiary` 의 필드는 `prdlst_code` / `prdlst_nm` / `diary_date` /
   `status` / `markdown` / `structured` 뿐이고, `VoiceTalkDiary` 엔티티에도 본문 컬럼은
   `markdownContent` **하나**입니다. 저희가 `markdown_internal` 같은 필드를 추가로 보내도
   조용히 버려집니다.
3. **`AgentCallbackPayload` 는 더욱 그렇습니다.** 이 콜백은 ID 통보용이라 본문 필드 자체가 없고,
   백엔드는 이 콜백을 받아 다시 `GET` 으로 본문을 가져갑니다. 따라서 **본문 변형 선택은 콜백이 아니라
   `GET` 호출 시점에 결정되어야 합니다.**

**결론: 저희 쪽 발신만으로는 해결 불가. 백엔드에서 `GET` 호출을 바꾸거나 컬럼을 늘려야 합니다.**

## 4. 요청 — A안을 권합니다

### ✅ A안 (권장) — `GET` 에 `view=public` 을 명시

백엔드 수정은 **한 줄**입니다.

```java
// ResearchAiSttClient.java:352
String url = getBaseUrl() + "/v1/calls/" + callId + "?inline=true&view=public";
```

- 저희는 **오늘 이미 지원합니다** — 서버 수정 없이 바로 되고, 저희 `API_MARKDOWN_VIEW` 설정과 무관하게
  요청이 이깁니다(인스턴스별 설정에 흔들리지 않는다는 뜻이라 오히려 안전합니다).
- 날짜별 일지도 같습니다: `GET /v1/daily-diaries/{id}?inline=true&view=public`
  (`ResearchAiSttClient.java:667`).
- 근거가 필요할 때는 **그때만** 따로 가져가시면 됩니다(§5) — 농가 앱에는 안 나가고, 컨설턴트 화면이나
  운영 조회에서만 쓰실 수 있습니다.
- 저희 운영 `.env` 를 `API_MARKDOWN_VIEW=public` 으로 되돌리는 건 **A안 반영 확인 후** 하겠습니다.
  (지금 바로 바꾸면 백엔드가 근거를 영영 못 받게 되므로, 순서를 지키려 합니다.)

### B안 — 두 벌 다 저장

농가 앱에는 `public`, 컨설턴트/내부 화면에는 `internal` 을 쓰고 싶으시면:

- `VoiceTalkDiary` 에 `markdownContentInternal` 컬럼 추가
- 저희가 `GET` 응답 `result.diaries[]` 에 `markdown_internal` 을 **추가로** 실어 드립니다
  (기존 `markdown` 필드는 그대로 두므로 하위 호환 유지 — 저희 쪽 작업 필요, 반나절)
- 앱 노출 경로(`AiDiaryListResponseDTO` 등)에서는 `markdownContent` 만 쓰도록 유지

### C안 (비권장) — S3 키로 받아 필요할 때 조회

`CALLBACK_INCLUDE_ARTIFACT_KEYS=true` 로 켜면 콜백에 `s3_key_md`(전달용) / `s3_key_md_internal`
(근거 포함) 두 키를 실어 드립니다. 다만 백엔드가 MinIO 를 직접 읽어야 하고 본문 저장 시점이 갈라져서,
지금 구조에는 A안이 더 맞습니다.

## 5. 근거 포함본이 따로 필요하실 때

A안을 택하셔도 근거는 언제든 가져갈 수 있습니다.

```
GET /v1/calls/{call_id}/artifacts/diary/{prdlst_code}?view=internal    # 작물별 일지 정본
GET /v1/calls/{call_id}/artifacts/report?view=internal                 # 컨설팅 보고서 정본
GET /v1/calls/{call_id}/transcript                                     # 전사 원문(화자·시각 포함)
```

`view` 를 생략하면 저희 서버 기본값이 적용되니, **항상 명시**하시길 권합니다.

## 6. 결정이 필요한 것

- [ ] **A / B / C 중 선택** — 저희 권장은 A
- [ ] A안이면: `view=public` 반영 시점 → 저희가 그 뒤 `API_MARKDOWN_VIEW=public` 으로 정리
- [ ] B안이면: 컬럼 추가 일정 → 저희가 `markdown_internal` 필드 추가 배포
- [ ] 근거 포함본을 어느 화면에 쓰실 계획인지 (컨설턴트 앱? 운영 백오피스? 안 쓰심?)

## 부록 — 확인에 쓴 명령

```bash
# 백엔드가 실제로 받는 것
curl -s -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:7003/v1/calls/$CID?inline=true" | jq '.result.diaries[].markdown'

# 두 벌 비교
curl -s -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:7003/v1/calls/$CID/artifacts/diary/0804MM?view=public"
curl -s -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:7003/v1/calls/$CID/artifacts/diary/0804MM?view=internal"
```
