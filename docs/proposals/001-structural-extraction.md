# 001 — 프롬프트로 못 고치는 감점의 구조 변경 제안

- 상태: 제안 (자동 적용 안 함 — 승인 시 사람이 브랜치를 따서 구현)
- 기준 측정: `out/voice-eval/report.md` (5케이스, 종합점수 0.78 / judge 축평균 3.767 / 재현율 0.900 / 발생단계 0.667)
- 기준 커밋: `08fcbed`
- 읽은 것: `docs/eval-journal.md` #1~#5, 케이스 5건의 `judge.json`·`facts.json`·산출 `diary_*.md`, `app/agents/` 파이프라인

## 0. 왜 프롬프트가 더 안 먹히는가

저널 #1~#5 는 전부 `app/agents/prompts/extract.system.md` 한 파일만 고쳤고, 5회 모두 **타깃 셀은 줄었는데
거부**됐다. 거부 사유가 방향을 말해 준다.

| 회차 | 타깃 셀 | 타깃 결과 | 거부 사유 |
|---|---|---|---|
| #1 | 기타 기록사항 missing | 5 → 2 | 종합점수 노이즈에 묻힘(당시 목적함수) |
| #2 | 투입 제품 missing | 3 → 1 | 종합점수 ▼0.0067 |
| #3 | 향후 계획 missing | 3 → 0 | 재현율 0.900 → 0.875 |
| #4 | 향후 계획 misclassified | 2 → 0 | faithfulness 4.2 → 3.8 |
| #5 | 기타 기록사항 missing | 5 → 2 | chatter 5.0 → 3.6 |

패턴은 하나다 — **프롬프트는 사실을 더 담게 만들 수는 있어도, 담을 칸을 만들지는 못한다.** 담을 칸이 없으면
LLM 은 (a) 가장 가까운 자유 칸(`observations`)에 밀어 넣거나 (b) 시점·조건 같은 수식어를 잘라내거나
(c) 이미 다른 섹션이 쓴 것을 다시 쓴다. (a)는 chatter 를, (b)는 faithfulness 를, (c)는 classification 을
깎는다. #4·#5 의 거부는 프롬프트 품질 문제가 아니라 **스키마·렌더 경로가 강제한 교환비**다.

`app/agents/schemas.py:19-21` 의 `extra="forbid"` 때문에 이건 은유가 아니라 문자 그대로다. 스키마에 없는
필드는 LLM 이 출력하면 구조화 호출이 실패한다. 즉 "지난주 살포"의 *지난주* 는 프롬프트를 어떻게 쓰든
`ProductFact` 어디에도 들어갈 수 없다(#2 가 `dose` 에 우겨 넣으려다 실패한 이유).

아래 3건은 전부 **스키마 필드 / 렌더 경로 / 결정적 매핑** 층의 변경이며, 저널이 시도한 프롬프트 축과
겹치지 않는다.

### 지목받았지만 원인이 아닌 것 (확인 결과)

- **`mapping/matcher.py` 임계값(`auto=88`, `ambiguous=70`)** — 발생단계 감점의 원인이 아니다.
  `tests/agents/fixtures/farmos/0804MM/dbyhs.json` 은 8행뿐이고 파밤나방·꽃곰팡이가 **표에 아예 없다**.
  임계값을 낮추면 파밤나방이 총채벌레·진딧물로 오매칭돼 `severity_exact` 가 더 나빠진다. 임계값은 건드리지 않는다.
- **`nodes/select_crops.py`** — 현재 5케이스는 전부 단일 작물이라 라우팅 감점이 0건이다.
  `route_facts` 의 느슨한 매칭(`select_crops.py:106-110`)과 대표작물 폴백은 다작물 케이스가 생기기 전까지
  측정 가능한 감점원이 아니다. 다만 `CropFacts`(`schemas.py:270-283`)에 `farm_status`·`advice`·`questions`
  가 없어 그쪽으로 간 사실은 일지 경로에 도달하지 못한다는 사실(저널 #5 가 발견)은 제안 3의 전제로 쓴다.
- **`extraction/misclassified/투입 제품` 중 카테고리 오분류 2건**(나노아이·이리응애가 `[농자재]` 대신 `[기타]`)
  — judge 는 `mapping` 으로 귀속했지만 코드에 카테고리 매핑 단계가 **없다**(`map_facts.py:88` 이 비농약을
  매칭 전에 걸러낸다). 즉 `ProductFact.category` 는 LLM enum 선택이 그대로 렌더된다(`diary.md.j2:71`).
  이건 아직 프롬프트/동의어 표로 시도할 여지가 남아 있어 이번 제안에서 제외한다.

---

## 제안 1 — 병해충 발생단계를 이름 매칭과 분리한다

### 문제

발생단계는 이미 올바르게 계산돼 있는데, **이름이 표준 목록에 없다는 이유로 통째로 버려진다.**

`map_facts.py:52` 가 `severity_to_step()` 으로 단계를 구해 `payload["step_index"]` 에 넣는다. 그런데
표준 행과 이름이 매칭된 경우에만 `payload.update(row.single(step))` 로 실제 단계 문자열이 채워진다
(`map_facts.py:60-64`). 템플릿은 `payload["occrrncStepNm"]` 만 읽으므로(`render/templates/diary.md.j2:48`)
매칭 실패 시 `확인 필요` 가 찍히고, 평가 지표도 `payload["occrrncStepCode"]` 만 보므로
(`voice_eval/metrics.py:32-35`) 그대로 오답 처리된다. **계산된 `step_index` 는 어디에서도 쓰이지 않는다.**

핵심은 이거다 — 발생단계 사다리는 `dbyhs` 표의 **모든 행에서 동일**하다
(`미발생|2%미만|5%미만|10%미만|30%미만|30%이상`, desc `안심|주의|주의|경고|경고|경고` —
`tests/agents/fixtures/farmos/0804MM/dbyhs.json` 8행 전부, `mapping/severity.py:3` 의 설명서 §3.3 스펙).
단계 이름은 **어느 병해충인지에 의존하지 않는다.** 그러므로 이름 매칭 실패와 단계 산출 실패는 독립 사건인데
코드가 둘을 하나로 묶어 놓았다. 프롬프트로는 손댈 수 없는 결정적 코드 경로다.

### 근거 (어느 케이스의 어느 감점)

| 케이스 | 병해충 | 추출된 severity / raw | 계산된 단계 | 렌더 결과 | 정답 |
|---|---|---|---|---|---|
| `strawberry_microbial` | 꽃곰팡이 | 경미 / "조금 보이기 시작한 정도" | 1 | `확인 필요` | `2%미만 (주의)` = 1 |
| `strawberry_nursery_disinfect` | 파밤나방 | 심함 / "벌레가 많이 다녔어" | 4 | `확인 필요` | `30%미만 (경고)` = 4 |
| `strawberry_planting` | 파밤나방 | 보통 / "새잎을 여기저기 갉아먹어…" | 4 (raw_hints `여기저기`) | `확인 필요` | `30%미만 (경고)` = 4 |

세 건 모두 `severity.yaml` 규칙이 **정답과 같은 단계를 이미 냈다**. 해당 감점:

- `mapping/missing/병해충` 2건 — microbial "꽃곰팡이병 발생단계 '2%미만 (주의)' 누락",
  planting "파밤나방 발생단계를 '30%미만 (경고)'로 특정하지 못하고 '확인 필요'로 기재함"
- `mapping/misclassified/병해충` 중 1건 — nursery "파밤나방 발생단계를 '30%미만 (경고)'가 아닌 '확인 필요'로 잘못 처리함"
- judge `severity` 축 3.8 (microbial 3, nursery 3, planting 3)

남는 1건(planting 시들음병 `2%미만` vs 정답 `10%미만`)은 이름이 **매칭된** 항목이라 이 제안 밖이다
(→ 아래 리스크 절의 후속 메모).

### 제안 변경

1. `app/clients/farmos.py` 에 조회된 `dbyhs` 행들의 단계 사다리가 전부 동일한지 확인하는 헬퍼를 두고
   (동일하면 그 사다리를 "표준 사다리"로 채택), `FarmosRefs` 에 `step_ladder: list[DbyhsStep] | None` 로 싣는다.
   사다리가 행마다 다르면 `None` → 현행 동작(`확인 필요`) 유지.
2. `map_facts.map_pests` (`map_facts.py:46-66`): 매칭 실패(`unmatched`/`ambiguous`/`no_refs`)여도
   `step_ladder` 가 있으면 `occrrncStepNm`·`occrrncStepCode`·`occrrncStepDesc`·`occrrncStepDescCode` 를
   채우고 `dbyhsCode`/`dbyhsNm` 은 **비워 둔다**(코드가 없다는 사실은 그대로 유지).
3. 렌더는 그대로 두되 배지만 유지 — `[표준 목록 미매핑]` 은 계속 붙는다(judge 프롬프트 8-12행이 안전 표기
   자체는 감점하지 않는다고 명시).
4. `build_prefill` (`nodes/crop_diary/render_diary.py:42-48`)은 **손대지 않는다** — `status == "matched" and
   payload["dbyhsCode"]` 조건이 그대로라 코드 없는 행은 farmos prefill 에 들어가지 않는다.

### 예상 효과 (평가 지표)

- `severity_exact`: 케이스별 microbial 1/2 → 2/2, nursery 1/2 → 2/2, planting 1/3 → 2/3
  (botrytis 2/2·tomato 1/1 불변) → 평균 **0.667 → 0.933**.
  종합점수 기여 `0.20 × 0.267 = **+0.053**` — 결정적 지표라 judge 노이즈와 무관하고, 판정 노이즈 밴드
  0.02 의 2.6배다.
- 셀: `mapping/missing/병해충` 2 → 0, `mapping/misclassified/병해충` 2 → 1. judge 항목 29 → 26.
- judge `severity` 축 3.8 → 4.4~4.6 예상(3점 케이스 3건 중 2건이 5점, 1건이 4점) → 축평균 +0.10~0.13 →
  종합점수 추가 **+0.010~0.013**.
- **합계 예상 +0.06 안팎.** 게이트는 전부 유지(재현율·faithfulness·chatter 는 이 변경의 영향을 받지 않는다).

### 리스크 · 되돌리기

- **리스크 1: 없는 병해충에 그럴듯한 단계를 붙인다.** 코드 없는 이름(오인식·비표준 명칭)에 `30%미만 (경고)`
  가 찍히면 농가가 표준 항목으로 오해할 수 있다. → `[표준 목록 미매핑]` 배지 유지 + `warnings` 에
  "표준 코드 없음 — 단계는 발화 정도 추정" 한 줄 추가로 완화. prefill 에는 안 들어가므로 farmos 데이터 오염은 없다.
- **리스크 2: 사다리 동일 가정.** 실 farmos 응답이 작물·병해충별로 다른 사다리를 주면 잘못된 단계명을 붙일 수
  있다. → 위 1번의 동일성 검사로 방어(다르면 현행 유지). `--farmos-token` 실조회 1회로 검증할 것.
- **되돌리기**: 변경이 `map_pests` 의 else 분기 + `FarmosRefs` 필드 하나라 revert 1커밋. 회귀 감시는
  `tests/agents/test_severity.py`·`test_render.py`(골든 렌더)가 즉시 잡는다.
- **후속(이 제안 아님)**: 시들음병 `severity_raw`("동당 3~4포기씩, 총 10포기 이상")에는 정도 힌트가 없고,
  힌트어 `군데군데` 는 `location` 필드에 들어가 있다(`facts.json`). `severity_to_step` 이 `severity_raw` 만
  보는 것(`severity.py:56`)도 구조적 결손이지만, `severity.yaml` 데이터 튜닝으로 먼저 시도해 볼 여지가 있어
  분리한다.

---

## 제안 2 — 시점(when)을 사실 스키마의 1급 축으로 올리고, 과거 사실의 렌더 경로를 분리한다

### 문제

파이프라인에는 **"통화일이 아닌 날"을 표현할 자리가 반쪽만 있다.**

- `FarmworkFact.when` 은 `today | past | planned | unknown` 4값뿐이다(`schemas.py:84`). 지난주부터 계속하는
  관수 같은 **진행 중 반복 작업**을 담을 값이 없다.
- `map_farmworks` 는 `when` 이 `today|unknown` 이 아니면 **매핑 목록에서 아예 뺀다**(`map_facts.py:28-30`).
  그래서 `past` 농작업은 `주요 농작업` 체크리스트에 절대 못 들어간다.
- 그런데 렌더러는 `past` + `date_hint` 를 **`향후 작업·확인 계획`** 줄로 만든다(`render/markdown.py:115-116`,
  `"(다른 날짜) {name}"`). 즉 과거 사실의 유일한 출구가 미래 섹션이다.
- `ProductFact` 에는 시점 힌트 필드가 **없다**(`schemas.py:108-116`: name/category/target/dose/when/crop/evidence).
  `when` 도 `applied|planned|recommended|unknown` 이라 "지난주에 살포함"은 `applied` 로 뭉개지고 템플릿은
  `· 투입함` 만 찍는다(`diary.md.j2:71`).

프롬프트가 할 수 있는 선택지는 두 개뿐이고 저널이 둘 다 태웠다 — 과거를 `today` 로 부르게 하거나(#4:
faithfulness 4.2 → 3.8 로 거부) 시점 표현을 `dose` 에 우겨 넣거나(#2: 거부). **정답 기준 자체가 시점 표기를
요구**하기 때문에 시점을 버리는 선택지는 애초에 없다.

### 근거 (어느 케이스의 어느 감점)

`strawberry_nursery_disinfect` 정답(`expected_diary.md:30,38`)은 명시적으로 이렇게 요구한다:
> 방제이력 — 방제대상: 파밤나방 / 약제: **엑설트 액상수화제 (지난주 살포)**
> 투입 제품 — [농약] 엑설트 → 파밤나방 · **지난주 살포**

실산출(`out/voice-eval/strawberry_nursery_disinfect/diary_0804MM.md:29,39`)은 `· 투입함` 뿐이고, 대신
`향후 작업·확인 계획` 첫 줄에 `- (다른 날짜) 약제살포 (지난주)` 가 올라가 있다(같은 파일 42행).

`strawberry_planting` 정답(`expected_diary.md:14-19`)은 `[x] 관수` 를 체크하라고 하고, 정식은 과거 작업이니
기타 기록사항으로 보내라고 못 박는다. 실산출은 `주요 농작업` 에 `약제살포` 하나뿐이고
`향후 작업·확인 계획` 에 `(다른 날짜) 정식 / (다른 날짜) 관수 / (다른 날짜) 천적방제` 3줄이 얹혀 있다
(`diary_0804MM.md:39-41`). 관수는 추출은 됐다(`facts_recall` 3/3) — 버린 건 `map_facts.py:29` 다.

해당 감점 5건:

- `rendering/misclassified/향후 작업·확인 계획` 1건 — planting "과거에 수행한 '정식', '관수', '천적방제' 작업이 향후 계획으로 잘못 기재됨"
- `extraction/misclassified/향후 작업·확인 계획` 2건 중 1건 — nursery "과거 작업인 '지난주 약제살포'를 향후 계획으로 잘못 분류함" (귀속은 extraction 이지만 코드 지점은 `markdown.py:116`)
- `extraction/missing/주요 농작업` 1건 — planting "'관수' 작업이 체크리스트에 누락됨" (귀속은 extraction 이지만 추출은 성공했고 `map_facts.py:29` 가 버렸다)
- `extraction/missing/방제이력` 2건 중 1건 — nursery "엑설트가 '지난주' 살포 약제라는 정보 누락"
- `extraction/missing/투입 제품` 3건 중 1건 — nursery "엑설트가 '지난주' 투입 약제라는 정보 누락"

### 제안 변경

1. **스키마**(`schemas.py`)
   - `FarmworkFact.when` 에 `ongoing` 추가(`today | ongoing | past | planned | unknown`).
     정의: "통화일 이전에 시작해 통화일에도 계속되는 반복·상시 작업"(관수·환기 등).
   - `ProductFact` 에 `date_hint: str | None` 추가(원문 표현 그대로: "지난주", "그저께").
   - `WHEN_KO`(`render/markdown.py:20-21`)에 `ongoing: "계속"` 추가.
   - 프롬프트는 이 두 필드의 **정의만** 새로 적는다(신설 필드의 최소 설명 — 저널이 시도한 서술 규칙 추가와는 다른 성격).
2. **매핑**(`map_facts.py:28-30`): 체크 대상을 `today | ongoing | unknown` 으로 넓힌다. `past` 는 계속 제외.
3. **렌더**(`render/markdown.py:112-120`)
   - `past` + `date_hint` 를 `plans` 에 넣던 분기(115-116행)를 **삭제**하고, 대신
     `past_works` 라는 별도 리스트로 모아 `write_content` 입력(제안 3)과 `기타 기록사항` 서술로 넘긴다.
     새 섹션은 만들지 않는다(섹션 구성은 동의서 §4·앱 화면 블록 고정 — `format` 축 리스크).
   - 제품 줄에 `date_hint` 를 붙인다: `· {{ when_ko }}{% if date_hint %} ({{ date_hint }}){% endif %}`
     (`diary.md.j2:71`), 방제이력 줄도 동일(`diary.md.j2:61`).

### 예상 효과 (평가 지표)

- 셀: `rendering/misclassified/향후 계획` 1 → 0, `extraction/misclassified/향후 계획` 2 → 1,
  `extraction/missing/주요 농작업` 1 → 0, `extraction/missing/방제이력` 2 → 1,
  `extraction/missing/투입 제품` 3 → 2. **합 9건 → 5건 (4건 감소).**
- judge 축: `classification` 2.8 → 3.4 안팎(planting 2 → 3, nursery 3 → 4), `coverage` +0.2~0.4,
  `format` 4.0 → 4.4(planting 향후계획 3줄·nursery 1줄 제거로 중복 해소). 축평균 +0.20~0.30 →
  종합점수 **+0.02~0.03**.
- `facts_recall` 0.900 불변(추출 목록 자체는 안 바뀐다), `severity_exact` 불변 → **게이트 위험 없음**.

### 리스크 · 되돌리기

- **리스크 1: `ongoing` 이 새 오분류 통로가 된다.** LLM 이 애매한 과거 작업을 `ongoing` 으로 올려 체크리스트에
  넣으면 `주요 농작업` 이 부풀고 faithfulness 가 깎인다(#4 가 거부된 형태의 재현). → 정의에
  "통화일에도 하고 있다고 명시적으로 말한 경우만"을 넣고, `ongoing` 항목에는 `date_hint` 를 필수로
  요구해 체크 줄에 `(지난주부터)` 를 함께 렌더한다. 확정 단계(judge×3)에서 faithfulness 하락 시 즉시 거부.
- **리스크 2: prefill 오염.** `ongoing` 이 매핑을 타면 `build_prefill`(`render_diary.py:33-40`)을 통해
  `userFarmworkList` 에 `checked=true` 로 들어간다. 다만 이 에이전트는 farmos 에 쓰지 않고 농가가 앱에서
  확인하는 구조(CLAUDE.md 하드룰)라 파급은 초안 수준이다. 그래도 보수적으로 가려면 1차 구현에서
  `ongoing` 은 렌더만 하고 prefill 에서는 제외하는 플래그를 둔다.
- **리스크 3: 스키마 변경 파급.** `CallFacts` 는 구조화 출력 스키마라 필드 추가 시 vLLM guided decoding /
  OpenAI strict json_schema 양쪽에서 재검증이 필요하다(`schemas.py:1-6` 규칙). `tests/agents/test_llm_structured.py`
  로 먼저 확인.
- **되돌리기**: 스키마 2필드 + 매핑 조건 1줄 + 템플릿 2줄이라 revert 1커밋. 픽스처 골든(`test_render.py`)이
  깨지므로 되돌림 여부가 명확히 드러난다.

---

## 제안 3 — `기타 기록사항` 을 "잔여 사실 싱크"로 정의하고, 부가 서술에 묶인 슬롯을 준다

### 문제

`기타 기록사항` 은 다른 5개 섹션과 성질이 다르다. 나머지는 전부 **결정적 렌더**(매핑 결과·사실 리스트를
템플릿이 그대로 찍는다)인데, 이 칸만 **LLM 자유 패스**이고 입력이 `CropFacts` **전체**다
(`nodes/crop_diary/write_content.py:77` — `cf.model_dump(exclude={"warnings"})`). 결과로 두 방향의 오류가
동시에 난다.

**(a) 넘침** — `follow_ups`·`actions`·`planned/recommended` 제품이 입력에 그대로 들어가고, 프롬프트도
"계획·컨설턴트 권고 요지"를 쓰라고 지시한다(`prompts/diary_content.system.md:7`). 그래서 `향후 작업·확인 계획`
섹션이 이미 결정적으로 렌더한 것을 산문으로 한 번 더 쓴다. 섹션 배타성이 **두 곳에서 따로 결정**되므로
프롬프트 문장으로는 정합을 보장할 수 없다.

**(b) 모자람** — 반대로 *사실에 딸린 부가 서술*은 담을 칸이 없다. `ProductFact` 에는 자유 텍스트 필드가
아예 없고(`schemas.py:108-116`), `PestFact` 도 `severity_raw`·`location` 뿐이며(`schemas.py:97-106`),
`FarmworkFact.detail` 은 존재하지만 `planned` 일 때만 렌더된다(`markdown.py:114` — `today/past` 의 detail 은
어디에도 안 나온다). 남는 자유 칸은 `observations` 하나인데, 이건 **어떤 사실에도 묶이지 않은** 자유 버킷이라
넓히면 잡담이 함께 들어온다 — 저널 #5 가 정확히 이 벽에 부딪혔다(chatter 5.0 → 3.6). #1 도 같은 경로였다.

즉 coverage 와 chatter 가 상충하는 이유는 프롬프트 품질이 아니라 **자유 칸이 무엇에도 묶여 있지 않다**는
구조다. 서술을 이미 채택된 사실(제품·작업·병해충)에 **묶어 두면** 두 지표는 독립이 된다.

### 근거 (어느 케이스의 어느 감점)

`extraction/missing/기타 기록사항` 5건 — 전부 "핵심 명사는 잡혔는데 그에 딸린 서술이 사라진" 형태다.

| 케이스 | 누락된 서술 | 묶여야 할 사실 | 현재 담을 칸 |
|---|---|---|---|
| nursery | "잎에서 흘러내릴 정도로" 살포 | 약제살포(farmwork, `past`/`today`) | `detail` 있으나 **렌더 안 됨** |
| nursery | 마쿠피카 + 나노-I **혼용** | 제품 2건의 관계 | 없음 |
| nursery | 지난주 엑설트 효과가 "그냥 그랬다" | 엑설트(product) | 없음 |
| botrytis | 아졸계를 생장억제 우려로 **거부**, 사파이어 선택 이유 | 제품 선택 판단 | 없음 |
| tomato | 수확량 하루 100kg·품질 양호 | 수확(farmwork) | `detail` 있으나 **렌더 안 됨** |

`rendering/misclassified/기타 기록사항` 2건 — tomato "향후 계획인 '안전 사용 기준 확인', '끈끈이 트랩 추가
설치', '사흘 뒤 재확인' 등이 현재 시점의 기록사항에 포함됨", microbial "향후 계획인 '응애 생존 여부 확인'
항목이 포함됨". planting 산출에서도 같은 중복이 보인다(`diary_0804MM.md:21` ↔ `:43`).

judge 축으로는 `coverage` 2.80(6축 중 최저 공동 1위)과 `format` 4.00 이 여기서 깎였다.

### 제안 변경

1. **묶인 서술 슬롯**(`schemas.py`)
   - `ProductFact.note: str | None`, `PestFact.note: str | None` 추가. 정의: "그 제품/병해충에 대해 농가·
     컨설턴트가 말한 부가 서술 — 방법·정도·혼용·효과 평가·선택과 거부 이유. 원문 표현 유지."
   - `note` 는 **반드시 그 사실에 붙는다.** 어떤 사실에도 안 붙는 잡담은 슬롯 자체가 없어 들어올 수 없다
     (#5 의 chatter 회귀를 구조로 차단).
2. **잔여 사실 계산**(`write_content.py`): `residual_facts(cf, rep, past_works)` 를 결정적으로 만들어
   `write_content` 입력으로 넘긴다. 규칙(전부 코드, 프롬프트 아님):
   - 포함: `observations`, 모든 `note`, `farmworks[*].detail`(체크된 작업 포함), 제안 2의 `past_works`,
     표준 목록 미매핑이라 체크되지 못한 작업(`rep.farmworks` 중 `unmatched|ambiguous`).
   - 제외: `follow_ups`, `actions`, `when in (planned, recommended)` 제품 — 이미 `향후 작업·확인 계획` 이
     결정적으로 렌더한다.
3. **프롬프트**(`diary_content.system.md:7`): "계획·컨설턴트 권고 요지" 지시를 뺀다. 입력에서 이미 빠지므로
   이 줄은 (2) 의 종속 변경이지 독립 시도가 아니다.
4. `deterministic_content`(`write_content.py:36-55`) 폴백도 같은 잔여 집합을 쓰도록 맞춘다.

### 예상 효과 (평가 지표)

- 셀: `extraction/missing/기타 기록사항` 5 → 1~2 (표의 5건 중 `note`/`detail` 로 자리가 생기는 4건이 대상,
  botrytis 의 "거부 이유"는 미사용 제품이라 제품 자체가 목록에서 빠지면 여전히 위태 — 보수적으로 1건 잔존 가정),
  `rendering/misclassified/기타 기록사항` 2 → 0. **합 7건 → 1~2건.**
- judge 축: `coverage` 2.80 → 3.4~3.8, `format` 4.00 → 4.4, `chatter` 5.00 **유지**(슬롯이 사실에 묶여 있음).
  축평균 +0.25~0.40 → 종합점수 **+0.025~0.040**.
- `facts_recall`·`severity_exact` 불변 → 게이트 위험 없음. faithfulness 는 원문 표현 유지 규칙 덕에 중립 예상.

### 리스크 · 되돌리기

- **리스크 1: `note` 가 새로운 잡담 통로가 된다.** 사실에 묶여 있어도 "손주가 왔다더라"를 제품 note 에 붙일
  수 있다. → 정의에 "그 사실의 방법·정도·효과·판단에 한정"을 명시하고, 확정 단계의 chatter 하드 가드
  (`optimize/decide.confirm`)가 그대로 잡는다. 5.0 에서 조금이라도 내려가면 거부.
- **리스크 2: 중복 제거 과잉.** `follow_ups` 를 입력에서 빼면 "표준 목록에 없어 체크 못 된 계획성 작업"이
  어디에도 안 남을 수 있다. → 제안 2 의 `past_works` + `unmatched` 작업을 잔여 집합에 명시적으로 포함해
  방어(위 2번 규칙). 제안 2 와 함께 적용하는 것을 권한다(단독 적용 시 이 구멍이 열린다).
- **리스크 3: 토큰.** 입력이 줄어 오히려 감소 예상(현재 총 160,808).
- **되돌리기**: `residual_facts` 는 순수 함수라 호출부 한 줄로 되돌릴 수 있고, 스키마 `note` 2필드는
  미사용으로 남겨도 무해. `test_render.py` 골든 + `test_voice_eval.py` 로 회귀 감시.

---

## 적용 순서와 검증

1. **제안 1** 먼저 — 결정적 지표(`severity_exact`)만 움직이고 judge 노이즈와 독립이라, 효과 유무가 한 번의
   실행으로 명확히 판정된다. 예상 +0.06.
2. **제안 2** — 스키마 2필드 + 렌더 경로. 제안 3의 전제(`past_works`)를 만든다. 예상 +0.02~0.03.
3. **제안 3** — 제안 2 위에서만 온전하다. 예상 +0.025~0.04.

셋을 다 적용했을 때 예상: 종합점수 **0.78 → 0.88 안팎**, judge 항목 **29 → 15 안팎**,
`severity_exact` 0.667 → 0.933, `facts_recall` 0.900 유지.

검증은 각 단계마다:

```bash
pytest -q tests/agents                                   # 골든 렌더·매핑·구조화 출력
python -m app.agents.voice_eval --stages pipeline,judge  # STT 재사용, 싼 재평가
```

확정 판정은 기존 루프 규칙을 그대로 쓴다 — judge×3 확정, faithfulness·chatter 하드 가드, 노이즈 밴드 0.02.
세 제안 모두 `facts_recall` 을 낮추지 않으므로 저널 #3 형태의 거부(재현율 하락)는 구조적으로 발생하지 않는다.
