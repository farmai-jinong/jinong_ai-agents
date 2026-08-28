# STT 모델 A/B — base vs 승격 FT(g55-w75), agent 평가(voice_eval) 기준 (2026-08-28)

## 왜 했나 / 전제 사실

"베이스라인 모델 배포 vs 현재 배포 모델"로 agent 평가를 비교하려던 것. 조사 결과 중요한 전제가 드러났다:

- agents 서비스는 게이트웨이에 **항상 `diarize=true`** 로 보낸다. 배포된 게이트웨이의 diarize 업스트림은
  `:8102`(qwen3-asr-diar)인데, 이 서버는 FunASR 레지스트리 로더라 **FT 체크포인트를 못 올린다**
  (`jinong_gpu/docs/execution/stt_operations.md` §10-1, 검증 2026-07-06).
- 즉 **현재 운영 agents 경로는 승격 FT(g55-w75)가 타지 않고 base Qwen3-ASR-1.7B 고정**이다.
  FT가 승격된 `:8100`(전사)·`:8103`(스트리밍)은 agents 경로에 없다.

그래서 이 비교는 "FT를 diarize 경로에 태우면 agent 평가가 좋아지는가"를 미리 재는 what-if A/B다.

## 방법

- `:8102`를 우회해 같은 구성을 재현: pyannote `:8104` 화자턴(케이스당 1회, 두 팔 공통) → 턴 슬라이스별
  전사(`language=ko`, 0.1s 미만 제거) → 게이트웨이 diarize 응답 포맷으로 합성해 voice_eval 캐시로 주입.
- 전사 백엔드는 두 팔 모두 vLLM(동일 스택): FT = 운영 `:8100`(symlink → `asr-soup-20260824-g55-w75/checkpoint-4000`),
  base = `Qwen/Qwen3-ASR-1.7B`을 GPU3 임시 포트 8106에 기동(평가 후 종료, 운영 무변경).
- 합성 스크립트: 세션 스크래치 `compose_stt_ab.py` (일회성). 평가:
  `python -m app.agents.voice_eval --out out/voice-eval-ab/{base,ft} --baseline out/voice-eval/summary.json --no-gate`
  (파이프라인 gemini-3.5-flash, judge 기본 설정, 단일 실행).
- 산출물: `out/voice-eval-ab/{base,ft}/` (report.md·summary.json·케이스별 stt/일지), `_work/`(wav·화자턴 캐시).

## 결과 (5케이스 평균)

| 지표 | base | FT g55-w75 | Δ (FT−base) | (참고) 운영 경로 실측* |
|---|---:|---:|---:|---:|
| CER | 0.136 | **0.128** | −0.008 | 0.131 |
| WER | 0.327 | 0.324 | −0.003 | 0.319 |
| 핵심어 인식률 | 0.867 | **0.933** | **+0.067** | 0.950 |
| 기대 추출 재현율 | 0.875 | 0.875 | = | 0.900 |
| 발생단계 정확도 | 1.000 | 0.933 | −0.067 | 1.000 |
| judge 축평균 | 4.200 | 4.233 | +0.033 | 4.333 |
| judge 총점 | 3.00 | 3.20 | +0.20 | 3.40 |
| **종합점수** | 0.8825 | 0.8725 | −0.010 | 0.9033 |

\* 운영 경로 실측 = 기존 승인 기준선 `out/voice-eval`(:8102 FunASR·base, 2026-08-27). 전사 스택·화자합성이
달라 절대값 직접 비교는 부정확 — 참고용.

케이스별 (CER base/FT · 핵심어 base/FT):

| 케이스 | CER | 핵심어 |
|---|---|---|
| strawberry_botrytis_choice | 0.075 / 0.074 | 0.50 / **1.00** (base가 점박이응애 miss) |
| strawberry_microbial | 0.117 / 0.114 | 1.00 / 1.00 |
| strawberry_nursery_disinfect | 0.171 / **0.179** | 0.83 / **0.67** (FT가 파밤나방 추가 miss, 다코닐은 둘 다 miss) |
| strawberry_planting | 0.150 / **0.130** | 1.00 / 1.00 |
| tomato_harvest | 0.169 / **0.144** | 1.00 / 1.00 |

## 해석

- **STT 지표(결정적)는 FT 우세**: CER 4/5 케이스 개선, 핵심어 +6.7%p. jinong-call 전량 서빙 A/B(base .1700 →
  FT .1184, `stt_operations.md` §10)와 방향이 일치하고, 대본 낭독 데이터라 폭은 작다.
- **일지 품질(종합점수)은 동급**: −0.010은 judge·파이프라인 단일 실행 노이즈 범위. FT의 명확한 STT 기인 감점은
  `크린캡→크림캡` 오전사 1건(microbial, 감점 3항목이 같은 뿌리). base 팔은 STT 기인 감점 0건이지만 핵심어
  miss(점박이응애 등)는 감점 아닌 재현율 지표에만 걸렸다. planting 발생단계 miss(2/3)는 STT 기인 증거 없음
  (추출 노이즈 가능성).
- **결론**: FT를 diarize 경로에 태우면 전사 정확도(핵심어·CER)는 개선되고 일지 품질은 최소 동급.
  단, 효과를 실제로 받으려면 `:8102`의 FunASR 로더가 로컬 FT를 못 읽는 문제를 풀어야 한다 — ①
  `qwen_diar_server.py`가 FT를 직접 로드하게 수정(§10-1의 "별도 작업"), 또는 ② 게이트웨이 diarize 조합을
  pyannote + `:8100` 경유로 재구성(이번 A/B가 쓴 구성 그대로라 결과가 이 수치와 같음).

## 재현/정리 상태

- 운영 변경 없음: `:8100`(FT)·`:8102`(base)·심볼릭링크 그대로, 임시 8106 서버·터널은 종료.
- 화자턴 캐시(`out/voice-eval-ab/_work/*/turns.json`)를 유지하므로 같은 턴으로 재전사·재평가 가능.
