"""여러 녹음 파일의 STT 결과를 하나의 MergedTranscript 로 병합 (순수 함수).

- 정렬: (seq NULLS LAST, recorded_at NULLS LAST, id)
- 오프셋: 앞 파일 duration 누적. duration = usage.seconds → source_end_sec → 호출자 duration_sec → 세그먼트 max end → 0
- speaker_key = f"f{file_index}:{speaker}" (파일 간 글자 뒤바뀜 대응)
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..db.models import CallAudio
from ..db.repo import order_audio
from ..schemas.transcript import MergedTranscript, TranscriptFile, TranscriptSegment


def _fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def file_duration(a: CallAudio, segments: Sequence[dict[str, Any]], source_end_sec: float | None = None) -> float:
    if a.stt_seconds:
        return float(a.stt_seconds)
    if source_end_sec:
        return float(source_end_sec)
    if a.duration_sec:
        return float(a.duration_sec)
    if segments:
        return max(float(s.get("end") or 0.0) for s in segments)
    return 0.0


def merge_transcripts(call_id: str, audios: Sequence[CallAudio],
                      raw_by_audio: dict[int, Any] | None = None) -> MergedTranscript:
    """`audios` 는 TRANSCRIBED/FAILED 행. segments 는 행의 segments_json(경량 캐시)에서 읽고,
    없으면 raw_by_audio[audio.id](게이트웨이 raw 배열)에서 파싱한다."""
    from ..clients.stt import parse_diarized

    files: list[TranscriptFile] = []
    segments: list[TranscriptSegment] = []
    speakers: list[str] = []
    offset = 0.0
    for idx, a in enumerate(order_audio(audios)):
        segs: list[dict[str, Any]] = list(a.segments_json or [])
        src_end: float | None = None
        if not segs and raw_by_audio and a.id in raw_by_audio and raw_by_audio[a.id] is not None:
            parsed = parse_diarized(raw_by_audio[a.id])
            segs = parsed.segments
            src_end = parsed.source_end_sec
        dur = file_duration(a, segs, src_end)
        files.append(TranscriptFile(
            file_index=idx, audio_id=a.id, bucket=a.bucket, key=a.key, seq=a.seq, recorded_at=a.recorded_at,
            offset_sec=offset, duration_sec=dur, speaker_hint=a.speaker_hint, stt_raw_key=a.stt_raw_key,
            status="TRANSCRIBED" if a.status == "TRANSCRIBED" else "FAILED",
            error=a.last_error if a.status != "TRANSCRIBED" else None,
        ))
        if a.status == "TRANSCRIBED":
            for s in sorted(segs, key=lambda x: (float(x.get("start") or 0.0), float(x.get("end") or 0.0))):
                text = (s.get("text") or "")
                if text.startswith(" "):
                    text = text[1:]
                if not text.strip():
                    continue
                spk = str(s.get("speaker") or "?")
                key = f"f{idx}:{spk}"
                if key not in speakers:
                    speakers.append(key)
                st = float(s.get("start") or 0.0)
                en = float(s.get("end") or st)
                segments.append(TranscriptSegment(
                    file_index=idx, audio_id=a.id, seg_id=str(s.get("id") or f"seg_{len(segments)}"),
                    speaker=spk, speaker_key=key, start=st, end=en,
                    abs_start=offset + st, abs_end=offset + en, text=text,
                ))
        offset += dur

    text = "\n".join(f"[{_fmt_ts(s.abs_start)}] [{s.speaker_key}] {s.text}" for s in segments)
    return MergedTranscript(call_id=call_id, files=files, segments=segments, speakers=speakers,
                            total_duration_sec=offset, text=text)


def transcript_markdown(t: MergedTranscript, speaker_map: dict[str, str] | None = None) -> str:
    """사람이 읽는 병합 전사 (S3 transcript/merged.md)."""
    role_ko = {"farmer": "농가", "consultant": "컨설턴트", "unknown": None}
    lines = [f"# 통화 전사 — {t.call_id}", "",
             f"- 파일 수: {len(t.files)} · 총 길이: {_fmt_ts(t.total_duration_sec)} · 세그먼트: {len(t.segments)}", ""]
    for s in t.segments:
        label = s.speaker_key
        if speaker_map and speaker_map.get(s.speaker_key) in role_ko and role_ko[speaker_map[s.speaker_key]]:
            label = f"{role_ko[speaker_map[s.speaker_key]]}({s.speaker_key})"
        lines.append(f"- [{_fmt_ts(s.abs_start)}–{_fmt_ts(s.abs_end)}] {label}: {s.text}")
    return "\n".join(lines) + "\n"
