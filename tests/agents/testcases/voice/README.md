# 음성 녹음 E2E 테스트케이스 (대본 + 정답)

`~/Downloads/farmsall_farmdiary-encode.json`(팜스올 실제 영농일지 6,466건)에서 기록 품질이 가장 좋은
5건을 표본으로 뽑아, **컨설턴트↔농장주 통화 녹음 → STT → 파이프라인 → 영농일지 초안**의 품질을
검증하기 위한 테스트케이스다. 원본 일지가 정답이고, 대본은 그 정답이 파이프라인 추론으로 재현되도록
역산해 만든 통화 시나리오다.

## 케이스 구성

| 케이스 | 원본(diary_id/작성자/작물) | 검증하는 생성 측면 | 목표 길이 |
|---|---|---|---|
| `strawberry_planting` | 3753 / 진상수 / 딸기 | 정형 수치 추출(정식일·주수·관수·EC/pH), 병해충 3종 심각도, 천적, 방제이력 | 8~10분 |
| `strawberry_botrytis_choice` | 2770 / 강보은 / 딸기 | 진단→약제 선택 **추론**(아졸계 기피 사유), prvnbe 2건(사파이어), 예방 언급 구분 | 3~5분 |
| `strawberry_nursery_disinfect` | 530 / 박완춘 / 딸기 | 다수 상표명 매핑·STT 난이도(다코닐/모스킬/마쿠피카/나노-I), 본포/육묘장 구분 | 3~5분 |
| `strawberry_microbial` | 3184 / 최연수 / 딸기 | 제품 카테고리 판별(농약 vs 비료·미생물 — 농약만 방제이력으로), 연막방제 | 3~5분 |
| `tomato_harvest` | 3509+3513 합성 / 김시홍 / 토마토 | 작물 다양성, 수확기 관리, 희소 메모의 통화 기반 보강 | 3~5분 |

## 파일 구성 (케이스별)

- `source.json` — 원본 일지(verbatim) + 보강 항목(`enriched`). 원본에 항목 미입력이지만 메모·통화상
  자연스러운 값은 임의 보강했고, 보강분은 `_enrichment_note`에 명시.
- `script.md` — 녹음용 대본. 화자 2인(농장주/컨설턴트), 잡담·간접 발화·정정 발화 포함.
  하단에 발화→기대 추출 매핑 표와 "정답에 새면 안 되는 잡담" 목록.
- `expected_diary.md` — 정답 영농일지. `app/agents/render/templates/diary.md.j2` 섹션 구조와 동일해
  실산출 `diary_<code>.md`와 나란히 diff/비교 가능. `(근거: #N)`은 녹음 전사에 따라 달라지므로 생략.
  맨 위 `> 📝 통화 요약 / > 💬 격려` 인용 블록과 맨 아래 `## 참고` 는 채점 제외(플레이스홀더). H1 제목은 없다.
  작물명은 `| 작물 | 이름 (코드) |` 표 행에서 읽는다(`voice_eval/cases.py`).
- `expect.json` — `app/agents/eval.py`의 recall 기대치와 동일 스키마:
  `{"farmworks": [..], "pests": [["이름","기대단계"], ..], "products": [..], "diary_status": {..}}`.
  부분 문자열 매칭이므로 "다코닐"↔"다코닐에이스 액상수화제"는 일치로 채점된다.

## 사용 절차

1. 대본을 2인이 전화 통화 톤으로 녹음한다(모노/스테레오 무관, ogg/m4a/wav).
   화자 순서·애드리브는 자유지만 **검증 포인트 표의 발화 내용은 유지**할 것.
   파일명을 `녹음대본_<케이스>.m4a` 로 두면 하네스가 자동으로 케이스에 붙인다.
2. 평가 하네스로 한 번에 돌린다 (docs/ops.md §4.2):

   ```bash
   python -m app.agents.voice_eval --audio-dir ~/Downloads/recordings
   open out/voice-eval/report.md
   ```

   STT 정확도(핵심어 인식률·CER/WER·화자분리)와 영농일지 요약 정확도(LLM judge 6축 + 원인 귀속)를
   한 리포트에 낸다. 프롬프트를 고친 뒤에는 전사 캐시를 재사용해
   `--stages pipeline,judge --baseline out/voice-eval/summary.json` 으로 델타만 본다.
3. 케이스별 산출물은 `out/voice-eval/<case>/` 에 남는다 — `stt.md`(전사), `diary_*.md`(산출),
   `judge.json`(채점). 눈으로 볼 때는 `expected_diary.md` 와 나란히 비교한다.
4. `--materialize` 를 주면 전사(`fixtures/calls/voice_<case>.json`)·기대치·facts·화자역할이 복사돼
   `python -m app.agents.eval` 에 편입된다(`--provider fake` 로 LLM 없이 매핑·렌더 회귀 점검 가능).

### 채점 규칙에서 알아둘 것

- **핵심어 인식률**은 대본에 실제로 발화된 표기만 센다. `expect.json` 의 표준 명칭(`잿빛곰팡이병`)이
  대본에서는 구어(`잿빛곰팡이`)로 발음되면 그 항목은 `n/a` 로 분모에서 빠진다 — STT 가 만들어낼 수 없는
  표기를 STT 점수로 깎지 않기 위해서다(표준 명칭 복원은 매핑 단계의 몫이라 `severity`/recall 로 따로 채점).
- judge 는 초안 맨 위 `| 항목 | 값 |` 표(생육단계·정식일·기존 일지)를 채점하지 않는다 — farmos 조회값이고,
  테스트 픽스처로 돌리면 통화 시기와 안 맞는 것이 정상이다.
- `expect.json` 에 선택 키 `"stt_keywords": ["1000리터", ...]` 를 추가하면 핵심어에 합쳐진다.

## 주의

- 심각도 발화는 `app/agents/mapping/severity.py`의 구어 힌트와 정합하게 설계돼 있다
  (조금/살짝/한두→1단계, 군데군데/일부→3, 많이/심해→4, 엄청/번졌→5). 녹음 시 해당 표현을 바꾸면
  기대 단계도 달라진다.
- `expected_diary.md`의 생육단계·정식일·기존일지 병합 여부는 farmos 실조회 값에 의존한다(플레이스홀더
  표기). 병해충·약제 표준 매핑은 실제 farmos 참조데이터에 해당 항목이 있다는 전제이며, 테스트 fixture
  (`tests/agents/fixtures/farmos/`)로 돌리면 일부는 `[표준 목록 미매핑]`으로 나오는 것이 정상이다.
- 원본 일지의 날짜(정식 9/11 등)는 대본에 그대로 두었다. 녹음 시점과 계절이 안 맞아도 파이프라인
  검증에는 영향 없다(일지 날짜는 통화 시각 기준으로 잡힘).
