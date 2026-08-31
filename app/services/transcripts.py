"""여러 녹음 파일의 STT 결과를 하나의 MergedTranscript 로 병합 (순수 함수).

- 정렬: (seq NULLS LAST, recorded_at NULLS LAST, id)
- 오프셋: 앞 파일 duration 누적. duration = usage.seconds → source_end_sec → 호출자 duration_sec → 세그먼트 max end → 0
- speaker_key = f"f{file_index}:{speaker}" (파일 간 글자 뒤바뀜 대응)
- 역할(농가/컨설턴트)은 병합 시점에는 알 수 없다 — 생성이 끝난 뒤 `apply_speaker_map` 으로 되먹인다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..db.models import Call, CallAudio
from ..db.repo import order_audio
from ..schemas.transcript import MergedTranscript, Role, TranscriptFile, TranscriptSegment

ROLE_KO: dict[str, str | None] = {"farmer": "농가", "consultant": "컨설턴트", "unknown": None}


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


def merge_calls(diary_id: str, per_call: Sequence[tuple["Call", Sequence[CallAudio]]]) -> MergedTranscript:
    """여러 통화의 전사를 하나로 병합 (날짜별 영농일지용, 순수 함수).

    - 통화 정렬: (started_at NULLS LAST, call_id)
    - 통화마다 `merge_transcripts` 로 병합한 뒤 file_index/offset/abs/speaker_key 를 글로벌 베이스로 리베이스.
      speaker_key = f"f{전역 file_index}:{speaker}" 라 통화 간에도 충돌하지 않는다 (화자 글자는 요청마다 재배정).
    - abs 시간은 원래부터 duration 누적 합성값 → 통화 간 실제 공백은 표현하지 않는다 (문서 명시).
    """
    def order_key(item: tuple["Call", Sequence[CallAudio]]):  # type: ignore[no-untyped-def]
        call, _ = item
        return (call.started_at is None,
                call.started_at.timestamp() if call.started_at else 0.0,
                call.call_id)

    files: list[TranscriptFile] = []
    segments: list[TranscriptSegment] = []
    speakers: list[str] = []
    offset = 0.0
    base_idx = 0
    for call, audios in sorted(per_call, key=order_key):
        part = merge_transcripts(call.call_id, audios)
        remap = {f.file_index: base_idx + f.file_index for f in part.files}
        for f in part.files:
            files.append(f.model_copy(update={
                "file_index": remap[f.file_index], "offset_sec": offset + f.offset_sec, "call_id": call.call_id,
            }))
        for seg in part.segments:
            new_idx = remap[seg.file_index]
            key = f"f{new_idx}:{seg.speaker}"
            if key not in speakers:
                speakers.append(key)
            segments.append(seg.model_copy(update={
                "file_index": new_idx, "speaker_key": key,
                "abs_start": offset + seg.abs_start, "abs_end": offset + seg.abs_end,
            }))
        base_idx += len(part.files)
        offset += part.total_duration_sec

    text = "\n".join(f"[{_fmt_ts(s.abs_start)}] [{s.speaker_key}] {s.text}" for s in segments)
    return MergedTranscript(call_id=diary_id, files=files, segments=segments, speakers=speakers,
                            total_duration_sec=offset, text=text)


def apply_speaker_map(t: MergedTranscript, speaker_map: dict[str, str] | None) -> MergedTranscript:
    """파이프라인이 추정한 역할(speaker_key → farmer|consultant|unknown)을 전사에 되먹인다 (순수 함수).

    화자 글자(A/B)는 등장 순서일 뿐이라 그 자체로는 농가/컨설턴트를 뜻하지 않는다. 역할은 생성
    파이프라인의 `assign_speaker_roles` 가 내용으로 추정하므로, 전사를 처음 쓸 때는 알 수 없고
    생성이 끝난 뒤에야 채울 수 있다. 알 수 없는 키·허용값 밖의 값은 `unknown` 으로 남긴다.
    """
    mp: dict[str, Role] = {}
    for key in t.speakers:
        v = (speaker_map or {}).get(key)
        mp[key] = v if v in ("farmer", "consultant") else "unknown"
    return t.model_copy(update={
        "speaker_map": mp,
        "segments": [s.model_copy(update={"role": mp.get(s.speaker_key, "unknown")}) for s in t.segments],
    })


def transcript_markdown(t: MergedTranscript, speaker_map: dict[str, str] | None = None) -> str:
    """사람이 읽는 병합 전사 (S3 transcript/merged.md). speaker_map 생략 시 전사에 실린 역할을 쓴다."""
    mp: dict[str, str] = dict(speaker_map or t.speaker_map or {})
    lines = [f"# 통화 전사 — {t.call_id}", "",
             f"- 파일 수: {len(t.files)} · 총 길이: {_fmt_ts(t.total_duration_sec)} · 세그먼트: {len(t.segments)}", ""]
    for s in t.segments:
        label = s.speaker_key
        ko = ROLE_KO.get(mp.get(s.speaker_key, ""))
        if ko:
            label = f"{ko}({s.speaker_key})"
        lines.append(f"- [{_fmt_ts(s.abs_start)}–{_fmt_ts(s.abs_end)}] {label}: {s.text}")
    return "\n".join(lines) + "\n"
