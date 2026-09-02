# 에이전트 파이프라인 — 흐름과 판정 기준

이 문서는 **"무엇이 무엇을 보고 판정하는가"** 와 **"그 기준이 어디에 적혀 있는가"** 만 다룬다.
상태기계·복구·배포는 `architecture.md` / `ops.md`, 외부 API 계약은 `api-reference.md` 를 본다.

판정 주체는 두 종류다.

- **규칙(결정적)** — 파이썬 코드. 같은 입력이면 항상 같은 출력. 임계값이 코드나 YAML 에 박혀 있다.
- **LLM** — 프롬프트 1회 호출. 기준은 `app/agents/prompts/*.system.md` 에 자연어로 적혀 있고,
  출력은 항상 파이썬 제약 검사(`validate` 류)를 한 번 더 통과해야 한다.

원칙: **LLM 은 코드를 만들지 않는다.** 표준 코드(작물·농작업·병해충·약제)는 규칙 매칭이 고르고,
LLM 은 애매한 항목에 한해 **주어진 후보 중에서만** 고른다 (`mapping/matcher.py` 정책 D7).

---

## 0. 전체 흐름

워커 잡 3종 (`app/worker/`):

```
STT 잡      녹음 1개 → 게이트웨이 화자분리(/v1/audio/transcriptions) → segments 저장
생성 잡      전사 병합 → LangGraph 파이프라인 → 산출물 S3 → 상태 확정 → 콜백
날짜별 잡    여러 통화 전사 병합 → 같은 파이프라인 (보고서는 버림)
```

파이프라인 그래프 (`app/agents/graph.py`):

```
START → prepare_transcript ─┬→ load_farm_context ──────┐
                            └→ assign_speaker_roles ───┴→ extract_facts → select_crops
                                                                              │
                          ┌───────────────────────────────────────────────────┤
                          ├→ Send("build_crop_diary") × 작물 수               │
                          └→ build_report ──────────────────────────────────┐ │
                                                                 finalize ←─┴─┘ → END

build_crop_diary (작물 1건당 서브그래프):
  fetch_refs → map_facts ─(ambiguous 있으면)→ disambiguate ─┐
                         └────────────────────────────────┴→ write_content → render_diary → verify_diary
```

생성이 끝나면 워커가 **화자 역할을 전사에 되먹이고**(`services/transcripts.apply_speaker_map`)
`transcript/merged.json|.md` 를 같은 키에 다시 쓴다.

---

## 1. 판정 노드 한눈에

| 노드 | 무엇을 판정하나 | 방식 | 기준 출처 |
|---|---|---|---|
| `prepare_transcript` | 세그먼트 → turn 병합 | 규칙 | `nodes/prepare_transcript.py` (gap 1.0s · 220자) |
| `load_farm_context` | 농가 작물 목록 출처 | 규칙(강등 사슬) | `nodes/farm_context.py` |
| `assign_speaker_roles` | 화자 글자 → 농가/컨설턴트 | **LLM** + 제약 | `prompts/speaker_roles.system.md` |
| `extract_facts` | 녹취문 → 구조화 사실 | **LLM** (+청크 병합) | `prompts/extract.system.md` |
| `select_crops` | 대상 작물 · 사실 배분 | 규칙 | `nodes/select_crops.py` |
| `fetch_refs` | farmos 참조 조회 여부 | 규칙(전제 조건) | `nodes/crop_diary/fetch_refs.py` |
| `map_facts` | 사실 → 표준 코드 | 규칙(매칭) | `mapping/matcher.py` · `mapping/severity.yaml` |
| `disambiguate` | 애매한 항목 후보 선택 | **LLM** (후보 한정) | `prompts/disambiguate.system.md` |
| `write_content` | 기타 기록사항 산문 | **LLM** | `prompts/diary_content.system.md` |
| `render_diary` | 일지 status · prefill | 규칙 | `nodes/crop_diary/render_diary.py` |
| `verify_diary` | 실질 내용 유무 (강등만) | **LLM** | `prompts/verify_diary.system.md` |
| `build_report` | 보고서 서술 | **LLM** (실패 시 규칙) | `prompts/report.system.md` |
| `finalize` | speaker_map · 경고 조립 | 규칙 | `nodes/finalize.py` |

파이프라인 **밖**: `agents/summarize.py` (통화 단순요약 — 콜백 `content`),
`agents/voice_eval/` (평가 하네스 — 운영 경로 아님).

---

## 2. 노드별 상세

### 2.1 `prepare_transcript` — 세그먼트를 turn 으로

**보는 것**: 병합 전사의 세그먼트(파일 인덱스·화자 글자·시각·텍스트).

**규칙** (`nodes/prepare_transcript.py`): 같은 파일·같은 화자 글자의 연속 세그먼트를 하나의 turn 으로 합친다.
합치는 조건은 **간격 < 1.0초**(`MERGE_GAP_SEC`) **이고 합친 길이 ≤ 220자**(`MERGE_MAX_CHARS`).
turn 에는 전역 `tid` 가 붙고, 이후 모든 노드의 `evidence` 는 이 `tid` 를 가리킨다.

### 2.2 `load_farm_context` — 농가 작물 목록

LLM 없음. **출처를 순서대로 강등**한다 (`nodes/farm_context.py`):

| 순서 | 조건 | source | status |
|---|---|---|---|
| 1 | 농가 JWT 있음 → farmos `list_crops` 성공 | `farmos` | `ok` |
| 2 | 토큰 없음 + AP 백엔드 URL·키 + 농가 복합 키(`engn_id`+`user_id`) | `ap_backend` | `partial` |
| 3 | 위가 다 안 되면 `hints`(`farmer_crops` 또는 `prdlst_nm`/`prdlst_code`) | `hints` | `unavailable`/`disabled` |
| 4 | 아무것도 없음 | `none` | `disabled` |

**농가 식별은 `(engn_id, user_id)` 복합 키만 인정**한다 — `user_id` 단독 조회는 금지(백엔드 문서 §1).
2번 경로는 작물 코드까지는 확정하지만 방제대상·약제·농작업 팔레트를 못 받으므로 일지는 `PARTIAL` 로 남는다.

### 2.3 `assign_speaker_roles` — 누가 농가인가 ★

STT 화자 글자 `A`/`B` 는 **그 요청 안의 등장 순서**일 뿐이다. 발신/수신도 아니고, 녹음 파일이 나뉘면
뒤집힌다(게이트웨이 계약 `jinong_ai-gateway/docs/api-reference.md` §3). 그래서 **내용으로** 판정한다.

**LLM 에 넘기는 입력 4가지** (`nodes/speaker_roles.py:91`):

| 입력 | 내용 |
|---|---|
| 참여자 정보 | 농가/컨설턴트 **이름만**. 누가 A인지는 알려주지 않는다(알 수 없으므로) |
| 화자 글자 목록 | 파일별 `A, B, …` |
| 통계 힌트 (`_hints`) | 글자별 **질문 표현**·**조언 표현** 정규식 카운트 + 발화 수 |
| **발췌문** (`excerpt`) | 앞 12턴 + 중간 6턴(균등 샘플) + 뒤 4턴. 짧은 통화면 전문 |

정규식은 코드에 있다 — 질문 `(나요|까요|어떻게|왜 |되나요|해야 ?하|되는 ?건가|맞나요|할까요|\?)`,
조언 `(하세요|하시면|하십시오|권장|추천|드리|보시고|하시는 게|해주세요|하셔야)`.

**판단 기준**: `prompts/speaker_roles.system.md` 에 자연어로 명시돼 있다.

- 농가 = 자기 농장·하우스·작물 상황을 설명하고 **질문**하는 쪽
- 컨설턴트 = 원인 설명·**권고·처방**을 하는 쪽
- 호칭 힌트 — 농가는 상대를 "선생님/박사님/소장님", 컨설턴트는 "사장님/대표님/어르신"
- **"통계 힌트도 참고하되 내용이 우선이다"** — 통계는 보조 신호이고 판정의 주근거는 대화 내용
- 파일마다 독립 판단 (파일1의 A와 파일2의 A는 다른 사람일 수 있음)
- 제3자(가족·직원)가 분명하면 `other`

**파이썬 제약 검사** (`validate()`):

1. 응답에 없는 글자는 버린다
2. 한 파일에서 **두 글자가 같은 역할** → `confidence = 0` *(화자가 2명일 때만)*
3. 화자 2명 이상인데 매핑이 1개뿐 → `confidence` 를 0.5로 제한

**게이트**: `apply_roles()` 는 **`confidence >= 0.6`**(`CONF_MIN`)인 파일만 역할을 붙이고, 미달이면
전부 `unknown` 이다. **확신이 없으면 찍지 않는다.**

**LLM 실패 시**: `_heuristic()` 폴백 — 글자별 `질문비율 - 조언비율` 이 높은 쪽을 농가로 본다.
단 신뢰도 0.4를 주므로 0.6 게이트에 걸려 **사실상 `unknown` 으로 떨어진다**. 화자가 2명이 아니면
아예 빈 결과를 낸다.

> **알려진 한계.** 위 제약 2·3은 화자가 2명 이상일 때만 걸린다. 화자가 **1명뿐인** 파일
> (화자분리가 1개 클러스터만 낸 경우)에는 제약이 걸리지 않아 LLM 판정이 그대로 통과한다.
> 그 클러스터에 두 사람 발화가 섞여 있으면 한쪽 역할로 뭉뚱그려진다.

결과는 `speaker_key`(`f0:A`) → `farmer|consultant|unknown` 맵으로 나가고,
`GET /v1/calls/{id}/transcript` 의 `segments[].role` 과 `result.speaker_map` 이 같은 값을 가리킨다.

### 2.4 `extract_facts` — 녹취문에서 사실 뽑기

**보는 것**: 통화 시각, 참여자, **화자 역할 주석**(`_speaker_note` — 신뢰도 0.6 이상인 파일만
`A=farmer` 식으로 넣고, 미달이면 "불확실 (화자 글자 그대로)"), 농가 작물 목록, hints, 그리고 녹취문 전체.

**기준 출처**: `prompts/extract.system.md`. 뽑는 축은 `CallFacts` 스키마(`agents/schemas.py`)가 정한다 —
`crops_mentioned` / `farm_status` / `farmworks` / `observations` / `pests` / `products` / `questions` /
`advice` / `actions` / `follow_ups` / `keywords` / `stt_uncertainties`.

**긴 통화** (`extract_max_input_tokens` = 14000 토큰 초과): turn 경계로 청크(`chunk_tokens` = 8000,
겹침 6턴) → 청크별 추출 → **결정적 병합**(`merge_facts`, 텍스트 유사도 ≥90 dedupe) → 요약·키워드만
`prompts/extract_merge.system.md` 로 1회 통합. 청크 파싱이 깨지면 그 청크를 **반으로 잘라 재시도**한다.

**검증**: `_valid_evidence()` 가 범위 밖 `tid` 를 잘라낸다 — 존재하지 않는 발화를 근거로 못 단다.

**실패 시**: 빈 `CallFacts` + 경고. 일지·보고서는 못 만들고 전사만 저장된다.

### 2.5 `select_crops` — 어떤 작물의 일지를 쓸 것인가

**LLM 없음. 100% 규칙** (`nodes/select_crops.py`). 우선순위대로:

1. `hints.prdlst_code`/`prdlst_nm` 이 있으면 그것부터 (농가 작물 목록과 매칭 시도)
2. 통화에서 **언급된 작물**(`crops_mentioned`)을 농가 작물 목록과 이름 매칭
3. 사실의 `crop` 필드에 등장한 이름
4. 아무것도 안 걸리면 **대표작물**(`reprsntPrdlstCnt == 1`) → 없으면 첫 작물 → 경고
5. 농가 작물 목록 자체가 없으면 `UNRESOLVED_CROP` 1건 + 경고

작물 이름 매칭 임계값은 여기서만 완화돼 있다 — `auto=85`, `ambiguous=70`, 그리고 ambiguous 라도
최고 후보가 **90 이상이면 채택**(`_match_crop`).

**사실 배분**(`route_facts`): `crop` 이 붙은 사실은 그 작물로, 안 붙은 사실은 대상이 1개면 그쪽,
여러 개면 **대표 작물 + 경고**. 후속·조치는 작물 정보가 없으므로 모든 대상에 공유한다.

### 2.6 `fetch_refs` — farmos 참조 데이터

**LLM 없음.** 다음이 **전부** 성립할 때만 조회한다: farmos 팩토리 있음 **and** 농가 JWT 있음
**and** 작물 코드 확정됨 **and** `farm.source == "farmos"`. 하나라도 빠지면 `refs_status="disabled"` 로
넘어가고, 표준 코드 매핑 없이 일지가 만들어진다(→ `PARTIAL`).

### 2.7 `map_facts` — 사실을 표준 코드에 붙이기

**LLM 없음. 결정적 매칭** (`mapping/matcher.py`): `exact → substring(양방향, 길이 ≥2) → rapidfuzz(문자열 + 자모 분해)`

| 임계값 | 기본값 | 뜻 |
|---|---|---|
| `auto` | 88.0 | 이 이상이면 **확정**(`matched`) |
| `ambiguous` | 70.0 | 이 미만은 후보에서 제외 |
| `sole_auto` | 80.0 | 후보가 **하나뿐**일 때 완화된 확정선 |
| `top_k` | 5 | LLM 에 넘길 후보 수 |

substring 매칭은 점수를 최소 90으로 본다. 확정에는 2위와의 **격차 5점 이상**(또는 2위가 `auto` 미만)이
필요하다. `auto`~`ambiguous` 사이면 `ambiguous` 로 남겨 `disambiguate` 로 넘긴다.

**무엇을 매핑 대상으로 삼는가** (여기서 걸러진다):

- 농작업: `when` 이 `today|ongoing|unknown` 인 것만. `past` 는 통화일 작업이 아니므로 체크 대상에서 빼고
  기타 기록사항 서술로 돌린다.
- 병해충: `status` 가 `발생|의심` 인 것만.

**병해충 발생단계**: `mapping/severity.py` — 표준 6단계(0 미발생 / 1 2%미만 / 2 5%미만 / 3 10%미만 /
4 30%미만 / 5 30%이상). 원문에 **%가 있으면 우선**, 없으면 단어 매핑. 단어표와 구어 힌트는
**`mapping/severity.yaml` 에 데이터로** 있고(자가 개선 루프가 튜닝하는 표면), %→단계 구간은 설명서
스펙이라 코드에 고정이다.

### 2.8 `disambiguate` — 애매한 것만 LLM 에게

`map_facts` 가 `ambiguous` 로 남긴 항목이 **하나라도 있을 때만** 실행된다(조건부 엣지 `_needs_llm`).

**보는 것**: 항목 원문 이름, 후보 목록(최대 5), 그리고 그 항목의 `evidence` turn 텍스트(최대 2개, 120자).

**기준 출처**: `prompts/disambiguate.system.md`.

**제약**: `apply_pick()` 이 **후보 코드 목록 안의 값만** 받아들인다. 후보 밖 값을 고르면
`unmatched` 로 되돌리고 `"LLM 이 후보 밖 값을 골라 무시함"` 경고를 남긴다. 고르지 않으면 `unmatched`.

### 2.9 `write_content` — 기타 기록사항

**기준 출처**: `prompts/diary_content.system.md`. 500자 상한(`MAX_CHARS`).

**입력이 곧 제약이다.** `residual_facts()` 가 **다른 섹션이 이미 결정적으로 렌더하는 사실을 입력에서 뺀다** —
`follow_ups`·`actions`·`planned`/`recommended` 제품·`planned` 작업은 "향후 작업·확인 계획"이 이미
렌더하므로 제외. 체크 줄로 표현된 농작업도 **부가 서술(`detail`)이 있을 때만** 넣는다.
섹션 배타성을 프롬프트 문장이 아니라 **입력 집합으로 보장**하는 설계다.

**실패 시**: 사실 bullet 로 결정적 대체.

### 2.10 `render_diary` — 일지 status 와 prefill

**LLM 없음. 규칙만** (`nodes/crop_diary/render_diary.py`):

| status | 조건 |
|---|---|
| `UNRESOLVED_CROP` | 작물을 확정하지 못함 (`target.resolved == False`) |
| `EMPTY` | `CropFacts.is_empty` — 농작업·관찰·병해충이 없고, `applied`/`unknown` 제품도 없음 |
| `PARTIAL` | 내용은 있으나 ① `refs_status` 가 `unavailable|partial|disabled` **이면서 farm 출처가 farmos 가 아님**(→ "표준 코드 매핑 없이 생성됨" 경고) 또는 ② `refs_status == "partial"` |
| `OK` | 위 어디에도 안 걸림 |

**prefill 은 `status` 가 `OK`/`PARTIAL` 이고 작물 코드가 있고 `refs` 가 실제로 있을 때만** 만들어진다 —
AP 백엔드 경로(`refs` 없음)는 코드가 확정돼도 prefill 이 나오지 않는다.
**`prefill_ready`** 는 거기에 더해 `refs_status == "ok"` **이고** 병해충·약제에 `ambiguous` 가
하나도 없을 때만 참이다.
기존 일지(`diaryId`)가 있으면 기존 체크를 보존하고 `content` 를 이어붙인다.

### 2.11 `verify_diary` — 빈 템플릿 걸러내기 (강등 전용)

`render_diary` 의 규칙 판정은 "추출된 사실이 하나라도 있는가"만 본다. 그래서 잡담에서 관찰 1건이
잘못 뽑히면 **모든 칸이 "언급 없음"인 빈 템플릿이 `OK` 로** 나간다. 이 노드는 **렌더된 초안을 사람이 보듯
다시 읽고** 판정한다.

**보는 것**: 작물명, 일지 날짜, **렌더된 마크다운 전문**, 기타 기록사항 본문. (사실 구조체가 아니라 결과물)

**기준 출처**: `prompts/verify_diary.system.md`.

**규칙**:
- `verify_diary_enabled=false` 이거나 status 가 이미 `EMPTY`/`UNRESOLVED_CROP` 이면 **LLM 호출 자체를 건너뛴다**
- 판정이 "내용 있음"이거나 **`confidence < 0.6`**(`verify_diary_min_confidence`)이면 **강등하지 않는다**
- **강등만 하고 승격은 하지 않는다.** 판정이 애매하면 내용이 있는 쪽으로 기운다
- 강등 시 빈 템플릿으로 다시 렌더하고 **prefill 을 거둔다**(농가 앱에 빈 초안을 밀어 넣지 않기 위해)
- 실패는 fail-open — 규칙 판정을 유지하고 경고만 남긴다

### 2.12 `build_report` — 컨설팅 보고서

**기준 출처**: `prompts/report.system.md`. 서술만 LLM 이 쓰고 개요는 결정적으로 렌더한다.

**검증**: 인용한 `evidence` 중 실재하지 않는 `tid` 는 잘라낸다.
**`needs_verification` 플래그**는 규칙이다 — 정규식
`(농약|약제|살포|희석|배액|비료|시비|양액|EC|ppm|\d+배|리터|ml|kg|그램|용량|안전사용|독성)` 에 걸리는
문장에 "사람 확인 필요" 표시가 붙는다. **수치·약제 관련 서술은 LLM 판단과 무관하게 항상 검토 대상**이다.

**실패 시**: `deterministic_narrative()` — `CallFacts` 를 그대로 bullet 로 옮긴 결정적 보고서.

### 2.13 `finalize` — 조립

**LLM 없음.** `speaker_map` 을 `speaker_key` 단위로 펴고(신뢰도 0.6 게이트 재적용), 토큰 사용량을 합산하고,
경고를 정리한다. 여기서 **한글 비율 < 0.5** 인 일지·보고서에 "한국어 비율 낮음 — 검토 필요" 경고가 붙는다.

---

## 3. 파이프라인 밖의 판정

### 3.1 통화 단순요약 (`agents/summarize.py`)

백엔드 통화요약 콜백의 `content` 가 되는 불릿 3줄. **일지 파이프라인과 독립된 LLM 패스**로,
일지 산출물을 요약하는 게 아니라 **녹취문을 다시 읽는다** — 일지 서식이 바뀌어도 요약이 흔들리지 않게.

- 기준 출처: `prompts/call_summary.system.md` (긴 통화 통합은 `call_summary_merge.system.md`)
- **일지가 실질 내용을 가질 때만 호출**한다(`has_diary_content`) — 잡담 통화에 LLM 을 쓰지 않는다
- 서식은 LLM 에 맡기지 않고 `render_summary()` 가 결정적으로 렌더 (조치 최대 3건, 후속 최대 2건)
- 실패 시 보고서 요약으로 폴백(추가 LLM 호출 없음)

### 3.2 평가 하네스 (`agents/voice_eval/`)

**운영 경로가 아니다.** 실녹음으로 STT 정확도 + 영농일지 품질을 채점하는 회귀 게이트.
채점 프롬프트는 `prompts/judge_diary.system.md` 이고, 자가 개선 루프(`voice_eval/optimize/`)는
채점 프롬프트와 정답 파일을 **열람만** 할 수 있다(`optimize/gates.py` — 채점자를 고쳐 점수를 올리는 것 금지).

---

## 4. LLM 프롬프트 목록

전부 `app/agents/prompts/` 아래. `*.system.md` 는 정적, `*.user.md.j2` 는 Jinja2 템플릿.
`PROMPT_VERSION` (`prompts/loader.py`)이 산출물 푸터에 찍힌다.

| 프롬프트 | 쓰는 곳 | 판정하는 것 |
|---|---|---|
| `speaker_roles` | `nodes/speaker_roles.py` | 화자 글자 → 농가/컨설턴트 |
| `extract` | `nodes/extract_facts.py` | 녹취문 → 구조화 사실 |
| `extract_merge` | `nodes/extract_facts.py` | 청크 요약·키워드 통합 |
| `disambiguate` | `nodes/crop_diary/disambiguate.py` | 애매한 항목의 후보 선택 |
| `diary_content` | `nodes/crop_diary/write_content.py` | 기타 기록사항 산문 |
| `verify_diary` | `nodes/crop_diary/verify_diary.py` | 일지 실질 내용 유무 |
| `report` | `nodes/report.py` | 보고서 서술 |
| `call_summary` · `call_summary_merge` | `agents/summarize.py` | 통화 단순요약 |
| `judge_diary` | `voice_eval/judge.py` | (평가 전용) 일지 채점 |

---

## 5. 임계값·게이트 한눈에

| 값 | 기본 | 의미 | 위치 |
|---|---|---|---|
| `CONF_MIN` | 0.6 | 화자 역할 채택 하한 — 미만은 `unknown` | `nodes/speaker_roles.py` |
| 폴백 신뢰도 | 0.4 | 통계 휴리스틱 — 게이트에 걸려 사실상 `unknown` | `nodes/speaker_roles.py` |
| `MERGE_GAP_SEC` / `MERGE_MAX_CHARS` | 1.0s / 220자 | turn 병합 조건 | `nodes/prepare_transcript.py` |
| `auto` / `ambiguous` / `sole_auto` | 88 / 70 / 80 | 이름 매칭 확정선 | `mapping/matcher.py` |
| 작물 매칭 | 85 / 70 (+90 구제) | 작물만 완화 | `nodes/select_crops.py` |
| `verify_diary_min_confidence` | 0.6 | 이 미만이면 강등하지 않음 | `config.py` |
| `extract_max_input_tokens` | 14000 | 넘으면 청크 분할 | `config.py` |
| `chunk_tokens` / `chunk_overlap_turns` | 8000 / 6턴 | 청크 크기·겹침 | `config.py` |
| `node_timeout_s` / `gen_timeout_sec` | 180s / 600s | 노드별 / 전체 상한 | `config.py` |
| 한글 비율 | 0.5 | 미만이면 "검토 필요" 경고 | `nodes/finalize.py` |

---

## 6. 설계 원칙 (왜 이렇게 나눠져 있나)

1. **코드는 규칙이, 문장은 LLM 이.** 표준 코드(작물·농작업·병해충·약제)는 매칭이 고르고 LLM 은
   후보 중에서만 고른다. LLM 이 코드를 지어내면 farmos 저장이 깨진다.
2. **LLM 출력은 항상 파이썬이 다시 검사한다.** 후보 밖 값 거부, 존재하지 않는 `evidence` 제거,
   역할 중복 검사, 신뢰도 게이트.
3. **확신이 없으면 비운다.** 화자 역할은 `unknown`, 작물은 `UNRESOLVED_CROP`, 일지는 `EMPTY`.
   찍어서 맞히는 것보다 비우는 게 낫다 — 농가가 확인할 초안이기 때문.
4. **강등은 하되 승격은 안 한다.** `verify_diary` 는 빈 초안을 걸러낼 뿐 없는 내용을 만들지 않는다.
5. **섹션 배타성은 프롬프트가 아니라 입력 집합으로.** 같은 사실이 두 섹션에 겹쳐 나오지 않게
   `write_content` 의 입력에서 아예 뺀다.
6. **실패해도 앞으로 간다.** 요약·검수·farmos 조회 실패는 생성을 막지 않고 경고로 남는다.
