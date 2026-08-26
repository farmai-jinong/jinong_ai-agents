# 자가 개선 루프 저널

`python -m app.agents.voice_eval.optimize` 가 반복마다 append 하는 누적 기록이다.
기계용 원본은 `eval-journal.jsonl`; 이 파일은 사람이 읽는 뷰다.

종합점수 = judge 50% + 기대추출 재현율 30% + 발생단계 정확도 20% (`voice_eval/report.py:composite`).
수락된 변경은 `eval/auto-tune` 브랜치에만 커밋된다 — `main` 은 루프가 건드리지 않는다.

---

## #1 — ❌ 거부 · 2026-08-26T19:26:52+09:00

- **타깃 셀**: `extraction/missing/기타 기록사항` (5건)
- **가설**: 추출 프롬프트가 사실을 명사 단위로만 뭉뚱그려 담게 유도해, 발화에 딸린 수량·정도·방법·혼용·효과비교·선택(거부) 이유가 CallFacts 어느 칸에도 안 남고 사라진다 — 이 부가 서술을 원문 표현 그대로 farmworks.detail/observations 에 보존하라는 일반 규칙 한 줄을 넣는다
- **근거**: 타깃 셀의 5개 감점(수확량·품질, 살포 정도 표현, 자재 혼용 사실, 이전 자재 효과 비교, 약제 선택·거부 이유)은 모두 '핵심 명사는 잡혔으나 그에 딸린 서술이 추출 단계에서 탈락'한 동일 유형이며, 이 서술들이 observations/farmworks.detail 로 보존되면 CropFacts 라우팅을 타고 기타 기록사항 생성 입력에 그대로 들어간다. '발화에 없는 내용 금지'를 같은 문장에 묶어 hallucinated 셀 악화를 막았다
- **베이스**: `9943ce3cd`
- **변경 파일**: `app/agents/prompts/extract.system.md` (1 file changed, 1 insertion(+))
- **게이트**: 전부 통과

| 단계 | 종합점수 | judge | 재현율 | 발생단계 | 타깃 셀 |
|---|---|---|---|---|---|
| 스크리닝 | 0.6842 (▲0.0009) | 2.6 | 0.925 | 0.7333 | 5 → 2 |

- **판정 사유**: 종합점수 0.6842 ≤ 기준 0.6833 + 노이즈 0.02
- 제안 세션: `claude --resume 986df347-fd88-4672-a457-d3c63c7cba50`
- 토큰: claude=4,507, screen=161,645
