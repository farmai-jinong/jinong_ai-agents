"""자가 개선 루프 — 평가 결과로 프롬프트·매핑 데이터를 고치고 기록한다.

원칙: **하네스가 루프를 소유하고 LLM 은 아이디어만 낸다.** 측정·게이트·수락 판정·기록은 전부 결정적
코드이고, Claude 세션은 "이 감점을 고칠 가설 하나와 최소 변경"만 만든다. 채점 하네스·정답·테스트는
경로 게이트가 막으므로, 과녁을 옮겨 점수를 올리는 일이 구조적으로 불가능하다.

진입점은 `__main__.py` (`python -m app.agents.voice_eval.optimize`).
"""

from .journal import Journal, Record

__all__ = ["Journal", "Record"]
