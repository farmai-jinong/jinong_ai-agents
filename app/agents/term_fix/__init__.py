"""STT 용어 오청 복구 — 전사(turn) + 도메인 용어 카탈로그 → LLM 치환 목록 → 결정적 적용·가드.

`:8105` 리트리버(자모 편집거리)가 예산 밖이라 못 잡는 오청(`하반→파밤나방`, `세츠→엑설트`)을 문맥으로 복구한다.
실험·판정 근거는 `docs/stt-term-fix-2026-09-06.md`; 오프라인 하네스는 `app/agents/voice_eval/term_fix/`.
파이프라인에서는 `node.correct_terms` 가 `prepare_transcript` 뒤에 farm/speaker 노드와 병렬로 돈다(`TERM_FIX_ENABLED`, 기본 off).
"""
