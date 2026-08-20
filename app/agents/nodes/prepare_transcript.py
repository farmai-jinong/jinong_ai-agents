"""세그먼트 → turn (같은 파일·화자 연속 병합, 전역 tid), 토큰 추정, 일지 날짜."""

from __future__ import annotations

from ..schemas import NormalizedTranscript, Turn
from ..state import PipelineState
from ..tools.transcript import est_tokens, format_turns
from ._common import diary_date_for

MERGE_GAP_SEC = 1.0
MERGE_MAX_CHARS = 220


def build_turns(state_raw, ) -> NormalizedTranscript:  # type: ignore[no-untyped-def]
    segs = sorted(state_raw.segments, key=lambda s: (s.file_index, s.start))
    turns: list[Turn] = []
    for s in segs:
        text = (s.text or "").strip()
        if not text:
            continue
        if turns:
            last = turns[-1]
            if (last.file_index == s.file_index and last.speaker_letter == s.speaker
                    and s.start - last.end_sec < MERGE_GAP_SEC and len(last.text) + len(text) + 1 <= MERGE_MAX_CHARS):
                last.text = f"{last.text} {text}"
                last.end_sec = s.end
                last.abs_end = s.abs_end
                last.seg_ids.append(s.seg_id)
                continue
        turns.append(Turn(tid=len(turns), file_index=s.file_index, speaker_letter=s.speaker,
                          speaker_key=s.speaker_key, start_sec=s.start, end_sec=s.end,
                          abs_start=s.abs_start, abs_end=s.abs_end, text=text, seg_ids=[s.seg_id]))
    n_files = max([f.file_index for f in state_raw.files] + [s.file_index for s in segs] + [-1]) + 1
    duration = state_raw.total_duration_sec or (max((t.abs_end for t in turns), default=0.0))
    return NormalizedTranscript(turns=turns, n_files=max(n_files, 1),
                                est_tokens=est_tokens(format_turns(turns, n_files)), duration_sec=duration)


async def prepare_transcript(state: PipelineState, config) -> dict:  # type: ignore[no-untyped-def]
    nt = build_turns(state["raw"])
    return {"transcript": nt, "diary_date": diary_date_for(state["ctx"]),
            "speaker_roles": None, "diaries": [], "errors": [], "usage": [], "warnings": []}
