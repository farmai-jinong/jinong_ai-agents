# 002 — STT 구간 지연(326초 통화 → 전사 9분) 원인 분해와 단축안

- 상태: **적용 중 (2026-09-16)** — `jinong_gpu` 브랜치 `exp/ctx-01-fast-path` + 게이트웨이 `main` 에 구현·오프라인 동일성 테스트 통과, 라이브 동일성 게이트(8 녹음 × 3 계층) 뒤 prod 재기동. 2-3 은 "제거" 가 아니라 "유지 + 2-2 로 가속" 으로 확정(제거하면 passes=1 통화에서 `text` 가 달라짐). 이 리포는 코드 무변경.
- 기준 통화: prod `4acfb650`(2026-09-16 14:43 KST, 326초 녹음, 120 세그먼트, 전사 2,204자) — 에이전트 실측 STT 543초
- 재현 재료: 같은 녹음을 각 서버에 직접 넣어 단계별로 잰 값 + S3 `stt/raw` 의 `:8105 timings`
- 결론 한 줄: **GPU 가 아니라 단일 스레드 CPU 작업 세 개**(pyannote 스레드 과점유, 순수 파이썬 용어 검색 ×2곳, 전문 전체 재전사)가 시간을 다 쓴다. 코드 4곳을 고치면 같은 통화가 **1분 안쪽**으로 들어온다.

## 0. 경로

```
agents ─(ogg 5.2MB)─▶ gateway(EC2 4코어) ─ssh 터널─▶ :8105 ctx ─▶ :8102 diar ─▶ :8104 pyannote
                                                         │              └▶ :8100 vLLM (턴별)
                                                         └ 카탈로그 검색(56,941건) → :8102 를 컨텍스트 붙여 한 번 더
gateway ◀─ 응답 ─ 세그먼트별 autocorrect + 전문 autocorrect(같은 알고리즘, 같은 카탈로그) ─▶ agents
```

## 1. 시간이 어디로 갔나 (326초 통화)

| 구간 | 실측 | 근거 |
|---|---|---|
| :8105 pass 1 (= :8102 1회) | 64초 | S3 raw `timings.pass1_s` |
| ㄴ 그중 pyannote 화자분리 | **115~127초**(단독 실행 시) | :8104 직접 호출 127초, 별도 프로세스 재현 115초 |
| ㄴ 그중 전문 전체 vLLM 전사 | 25초 | :8100 직접 호출. `qwen_diar_server` 가 diarize=true 에도 전문을 먼저 한 번 전사(`text` 필드용) |
| ㄴ 그중 턴별 ASR 125턴 | 약 20초 | 턴당 0.12~0.19초 |
| :8105 카탈로그 검색 | **120초** | `timings.retrieval_s`; 로컬 프로파일 148초/164초 중 순수 파이썬 DP 1,156만 회 |
| :8105 pass 2 (= :8102 1회 더, 컨텍스트 포함) | 48초 | `timings.pass2_s`; 캐시 없어 pyannote·전문 전사 재실행 |
| 게이트웨이 후처리 | **307초** | 게이트웨이 경로 재현: 총 556.9초 − :8105 timings 합 250초(pass1 85 + retrieval 126 + pass2 40). 그 구간 게이트웨이 컨테이너 CPU 100%, 적용된 교정 0건. 로컬(M시리즈) 재현: 전문 2,204자 autocorrect 70초 + 세그먼트 120개 6초 |

pass 1 이 64초로 pyannote 단독값보다 짧은 이유는 부하 편차다(GPU 서버 loadavg 30→73 사이를 오감).
9/14 의 72초 녹음 2건이 475초 걸린 것도 같은 구조다(`pass2_s` 360초 — :8100 큐 대기).

부하 배경: GPU 서버(B200 4장, 72코어)에 SFT 학습 6개 + held-out 평가 1개가 상시 돌고 있어 loadavg 30~90.
서빙 프로세스는 전부 GPU3 에 있고 학습 하나가 GPU3 를 같이 쓴다. 그러나 아래 병목은 모두 CPU 단일 스레드라
GPU 를 비워도 거의 안 빨라진다.

## 2. 병목별 원인과 수정 (측정 완료)

### 2-1. pyannote: torch 스레드 72개 과점유 → `set_num_threads(4)` 로 **115초 → 4.9초**

`pyannote_diar_server.py` 는 `torch.get_num_threads()==72` 기본값으로 돈다. 부하 높은 박스에서 OpenMP 스핀
대기가 CPU 구간(VBx 클러스터링·풀링)을 수십 배 늘린다. 같은 프로세스 방식으로 같은 파일을 재현:

| 스레드 | 소요 | 턴 수 |
|---|---|---|
| 72 (현재) | 115.1초 | 125 |
| 4 | **4.9초** | 125 |
| 1 | 5.3초 | 125 |

수정: `pyannote_diar_server.py` main 에서 `torch.set_num_threads(int(os.environ.get("PYANNOTE_TORCH_THREADS","4")))`
한 줄, 또는 `stt_models.yaml` 의 launch 에 `OMP_NUM_THREADS=4`. 기동은 `setsid nohup`(ops 메모 참조).

### 2-2. 카탈로그 검색: 위치 기반 프리필터 + C 편집거리 → **72초 → 3.1초, 결과 동일**

`ctx_retrieval.retrieve` 의 프리필터는 "용어의 자모 바이그램이 전사 어딘가에 충분히 있나"만 보는데, 전사가
길어지면 437개 바이그램이 카탈로그 56,719건 중 56,510건을 통과시킨다. 이후 후보마다 단어 시작점 클러스터
전부에 순수 파이썬 DP 를 돌린다(1,156만 회).

프로토타입(`docs/proposals/002-fast_retrieve.py`, 원본 loop 복사 + 두 가지만 변경):

- (A) 클러스터 안에 **서로 다른** 용어 바이그램이 `n_bg − 2·maxd` 개 이상 있을 때만 DP — 이미 만들던
  클러스터에 집합 하나 얹는 것이라 의미 변화 없음(DP 를 통과할 수 없는 클러스터만 건너뜀).
- (B) `_best_prefix_distance` 를 `rapidfuzz.distance.Levenshtein.distance(pat, text[:l], score_cutoff=maxd)` 의
  l 루프(±maxd)로 대체 — 같은 값을 C 로 계산.

실제 통화 5건 pass-1 전사로 검증. **매치 집합·점수 전부 IDENTICAL**:

| 통화(전사 길이) | 기존 | (A)만 | (B)만 | (A)+(B) |
|---|---|---|---|---|
| 4acfb650 (2,204자) | 72.0초 | 10.9 | 15.5 | **3.1** |
| 1c1d842d (1,040자) | 37.4 | 8.3 | 8.1 | **2.2** |
| 35aba9a2 (935자) | 32.1 | 7.6 | 7.0 | **2.0** |
| a6b7e38f (903자) | 30.5 | 6.9 | 6.6 | **1.9** |
| 9c43ac04 (276자) | 8.6 | 1.5 | 1.8 | **0.5** |

`rapidfuzz` 는 세 런타임(:8105 venv `stt-ctx`, 게이트웨이 이미지, 로컬) 모두 미설치라 의존성 추가가 필요하다(→ 추가함: `install_stt.sh`, 게이트웨이 `requirements.txt` 3.14.6 고정, 미설치 시 순수 파이썬 폴백).
(A)만으로도 7배라 의존성 없이 먼저 넣을 수 있다. **게이트웨이 `app/terms.py` 는 같은 알고리즘의 사본**이므로
같은 패치를 두 곳에 적용한다.

### 2-3. 게이트웨이 전문 autocorrect — **유지하고 2-2 로 가속** (제거안 폐기) → 약 **150~300초 → 수 초**

`routes/transcriptions.py::_offline_diarize` 는 세그먼트별 교정 뒤 `body["text"]`(전문) 에도 autocorrect 를
한 번 더 돌린다. 이 전문은 (a) 에이전트가 쓰지 않고(`app/clients/stt.py` 는 `segments` 만 소비, `text` 는 저장만)
(b) 교정 결과도 0건이었다(exact-only, 세그먼트에서 이미 잡힘). 세그먼트 텍스트를 join 해서 `text` 를 만들면
동일한 응답에 비용 0 — 이 제안은 **폐기**했다. "세그먼트 join = 전문" 은 2패스 통화에서만 성립하고, passes=1 통화(최근 80건 중 54건)에서는 `text` 가 :8102 의 전문 디코드라 join 과 다르므로 응답이 바뀐다. 요구사항이 "출력 바이트 동일" 이라 전문 autocorrect 는 남기고 2-2(같은 `app/terms.py`)로 수 초까지 줄인다.

### 2-4. `qwen_diar_server` 의 전문 전체 전사 제거 + pass 2 화자분리 재사용 → **25초×2 + pyannote 1회**

- diarize=true 요청에서 `full_text = _transcribe_path(path)` 는 `text` 필드에만 쓰인다. 세그먼트 join 으로 대체.
  덤으로 vLLM API 프로세스의 이벤트 루프를 25초 막는 librosa 전처리(326초 오디오 로드·리샘플·분할)가 사라져
  뒤에 줄 선 턴별 요청도 안 밀린다.
- `qwen_ctx_server` pass 2 는 같은 오디오를 `:8102` 에 다시 보내 pyannote 를 또 돌린다. `:8102` 에 선택 폼필드
  `turns`(JSON) 를 추가해 pass 1 응답의 `segments` 를 그대로 넘기면 pyannote·전문 전사가 생략되고 턴별 ASR 만
  남는다(20초). merge 는 이미 세그먼트 키 정합을 요구하므로(`merge_passes` 의 `_seg_key` 비교) 오히려 더 안전하다.

## 3. 합치면

| | 현재 | 2-1 | +2-2 | +2-3 | +2-4 |
|---|---|---|---|---|---|
| 326초 통화 STT 총 소요 | 543초 | 약 320 | 약 200 | 약 60 | **약 40초** |

내역(2-4 까지): pass 1 = pyannote 5 + 턴별 20 = 25, 검색 3, pass 2 = 턴별 20, 게이트웨이 세그먼트 교정 수 초.
전부 결정적 로직 변경이라 전사 결과는 바이트 단위로 같아야 하며(2-2 는 5건 검증 완료, 2-1 은 턴 수 동일),
`jinong_gpu/stt-serve/tests` 와 게이트웨이 테스트로 회귀를 잡는다.

## 4. 적용 순서 제안

1. **2-1**(한 줄, 재기동만) → 즉시. 가장 크고 가장 안전.
2. ~~**2-3**(게이트웨이 한 함수)~~ → 폐기, 2-2 를 게이트웨이 `terms.py` 에도 적용하는 것으로 대체.
3. **2-2 (A)** 를 `ctx_retrieval.py` 와 게이트웨이 `terms.py` 에 동시에 → 의존성 없이 7배. 이어서 (B) rapidfuzz.
4. **2-4** → `:8102` 폼필드 추가 + `:8105` 전달 + 전문 전사 제거. 세 서버 계약이 걸려 마지막.

에이전트 쪽 변경은 없다. 다만 `STT_TIMEOUT=900` 이 지금 구조에서는 6분대 통화부터 위험하니, 위 수정 전까지
긴 통화가 재시도로 넘어가면 이 문서를 먼저 볼 것.

## 5. 재현 방법

- 단계별 직접 호출: GPU 서버에서 `curl -F file=@call.ogg http://127.0.0.1:8104/v1/audio/diarize`(pyannote),
  `.../8100/v1/audio/transcriptions`(전문), `.../8102 ...`(diarize=true) 각각 `-w %{time_total}`.
- :8105 내부 분해: 에이전트 S3 `agents/voicecall/<call>/stt/raw...json` 의 `response.timings`.
- 검색 프로토타입: `docs/proposals/002-fast_retrieve.py <pass1.txt>`(rapidfuzz 필요) (원본 `ctx_retrieval` 을 import 해 비교).
- pyannote 스레드: `envs/stt-pyannote` 파이썬에서 `torch.set_num_threads(n)` 후 `Pipeline(...)(wav, num_speakers=2)`.
