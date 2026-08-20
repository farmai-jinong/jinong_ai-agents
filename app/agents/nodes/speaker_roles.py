"""파일별 화자 글자 → 역할(farmer/consultant) — LLM 1회(발췌) + Python 제약 검사."""

from __future__ import annotations

import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from ..deps import get_deps
from ..llm import structured_call
from ..prompts.loader import load_system, render_user
from ..schemas import FileSpeakerMap, LetterRole, SpeakerRoleResult, Turn
from ..state import PipelineState
from ..tools.transcript import excerpt, fmt_ts
from ._common import err, participants_view

log = logging.getLogger(__name__)
CONF_MIN = 0.6
_Q = re.compile(r"(나요|까요|어떻게|왜 |되나요|해야 ?하|되는 ?건가|맞나요|할까요|\?)")
_A = re.compile(r"(하세요|하시면|하십시오|권장|추천|드리|보시고|하시는 게|하시는게|해 주세요|해주세요|하셔야)")


def _hints(turns: list[Turn]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for t in turns:
        h = out.setdefault(t.speaker_letter, {"q": 0, "a": 0, "n": 0})
        h["n"] += 1
        h["q"] += len(_Q.findall(t.text))
        h["a"] += len(_A.findall(t.text))
    return out


def _heuristic(turns: list[Turn]) -> tuple[dict[str, str], float]:
    """LLM 실패 시 통계 기반 폴백 (신뢰도 낮음)."""
    h = _hints(turns)
    letters = sorted(h)
    if len(letters) != 2:
        return {}, 0.0
    def score(letter: str) -> float:  # 질문 많고 조언 적을수록 농가
        n = max(1, h[letter]["n"])
        return h[letter]["q"] / n - h[letter]["a"] / n
    a, b = letters
    sa, sb = score(a), score(b)
    if abs(sa - sb) < 0.05:
        return {}, 0.0
    farmer, cons = (a, b) if sa > sb else (b, a)
    return {farmer: "farmer", cons: "consultant"}, 0.4


def apply_roles(turns: list[Turn], sr: SpeakerRoleResult) -> None:
    by_file = {f.file_index: f for f in sr.files}
    for t in turns:
        f = by_file.get(t.file_index)
        if f and f.confidence >= CONF_MIN:
            r = f.mapping.get(t.speaker_letter)
            t.role = r if r in ("farmer", "consultant") else "unknown"
        else:
            t.role = "unknown"


def _normalize_file_index(sr: SpeakerRoleResult, expected: set[int]) -> SpeakerRoleResult:
    """LLM 이 프롬프트의 '[파일 N]'(1-based) 을 그대로 file_index 로 쓴 경우(모델별로 흔함) 0-based 로 되돌린다.
    되돌리는 조건: 응답 인덱스 집합이 기대 집합을 +1 한 것과 정확히 같고, 기대 집합 자체와는 다를 때만(부분 응답은 건드리지 않음)."""
    got = {f.file_index for f in sr.files}
    if got and got != expected and got == {i + 1 for i in expected}:
        return SpeakerRoleResult(files=[f.model_copy(update={"file_index": f.file_index - 1}) for f in sr.files])
    return sr


def validate(sr: SpeakerRoleResult, letters_by_file: dict[int, list[str]]) -> SpeakerRoleResult:
    sr = _normalize_file_index(sr, set(letters_by_file))
    files: list[FileSpeakerMap] = []
    for fi, letters in letters_by_file.items():
        f = next((x for x in sr.files if x.file_index == fi), None)
        if f is None:
            files.append(FileSpeakerMap(file_index=fi, roles=[], confidence=0.0, rationale="LLM 응답 없음"))
            continue
        mp = {k: v for k, v in f.mapping.items() if k in letters}
        conf = float(max(0.0, min(1.0, f.confidence)))
        roles = [v for v in mp.values() if v in ("farmer", "consultant")]
        if len(letters) == 2 and len(set(roles)) < len(roles):
            conf = 0.0   # 두 글자가 같은 역할
        if len(letters) >= 2 and len(mp) < 2:
            conf = min(conf, 0.5)
        files.append(FileSpeakerMap(file_index=fi, roles=[LetterRole(letter=k, role=v) for k, v in mp.items()],
                                    confidence=conf, rationale=f.rationale))
    return SpeakerRoleResult(files=files)


async def assign_speaker_roles(state: PipelineState, config) -> dict:  # type: ignore[no-untyped-def]
    deps = get_deps(config)
    nt = state["transcript"]
    ctx = state["ctx"]
    if not nt.turns:
        return {"speaker_roles": SpeakerRoleResult(files=[])}
    letters_by_file: dict[int, list[str]] = {}
    turns_by_file: dict[int, list[Turn]] = {}
    for t in nt.turns:
        turns_by_file.setdefault(t.file_index, []).append(t)
        if t.speaker_letter not in letters_by_file.setdefault(t.file_index, []):
            letters_by_file[t.file_index].append(t.speaker_letter)
    files_view = []
    for fi, turns in sorted(turns_by_file.items()):
        ex = excerpt(turns)
        files_view.append({
            "file_index": fi, "letters": letters_by_file[fi], "hints": _hints(turns),
            "excerpt": "\n".join(f"#{t.tid} [{fmt_ts(t.abs_start)}] 화자{t.speaker_letter}: {t.text}" for t in ex),
        })
    msgs = [SystemMessage(content=load_system("speaker_roles")),
            HumanMessage(content=render_user("speaker_roles", call_id=ctx.call_id,
                                             participants=participants_view(ctx), files=files_view))]
    try:
        out, trace = await structured_call(deps.llm, SpeakerRoleResult, msgs, name="speaker_roles",
                                           mode=deps.settings.llm_structured_mode, dump_dir=deps.dump_dir,
                                           timeout=deps.settings.node_timeout_s)
        sr = validate(out, letters_by_file)
        apply_roles(nt.turns, sr)
        return {"speaker_roles": sr, "transcript": nt, "usage": [trace.usage()]}
    except Exception as e:  # noqa: BLE001
        log.warning("speaker_roles LLM failed: %s", e)
        files = []
        for fi, turns in sorted(turns_by_file.items()):
            mp, conf = _heuristic(turns)
            files.append(FileSpeakerMap(file_index=fi, roles=[LetterRole(letter=k, role=v) for k, v in mp.items()],
                                        confidence=conf, rationale="LLM 실패 — 통계 폴백"))
        sr = SpeakerRoleResult(files=files)
        apply_roles(nt.turns, sr)
        return {"speaker_roles": sr, "transcript": nt, "errors": [err("assign_speaker_roles", e)],
                "warnings": ["화자 역할 식별 실패 — 화자 A/B 로 표기"]}
