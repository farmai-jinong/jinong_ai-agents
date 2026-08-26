# 자가 개선 루프 저널

`python -m app.agents.voice_eval.optimize` 가 반복마다 append 하는 누적 기록이다.
기계용 원본은 `eval-journal.jsonl`; 이 파일은 사람이 읽는 뷰다.

종합점수 = judge 50% + 기대추출 재현율 30% + 발생단계 정확도 20% (`voice_eval/report.py:composite`).
수락된 변경은 `eval/auto-tune` 브랜치에만 커밋된다 — `main` 은 루프가 건드리지 않는다.

---
