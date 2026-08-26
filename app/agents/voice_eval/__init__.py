"""음성 테스트케이스 평가 하네스 — 녹음 → STT → 파이프라인 → LLM judge → 리포트/회귀 게이트.

케이스는 `tests/agents/testcases/voice/<case>/` (script.md · expected_diary.md · expect.json · source.json),
녹음은 리포지토리 밖(`~/Downloads/recordings` 등)에서 `--audio-dir` 로 받는다. 실행 진입점은 `__main__.py`.
"""

from .cases import VoiceCase, load_cases

__all__ = ["VoiceCase", "load_cases"]
