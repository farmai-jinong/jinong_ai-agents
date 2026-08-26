"""전사 — 게이트웨이 STT 직접 호출(서버·워커·S3 미경유) + 캐시 + 픽스처 생성.

`SttClient.diarize` / `parse_diarized` 를 그대로 쓰고, 결과를 `run.py:load_fixture` 가 읽는
`{"transcript": MergedTranscript, "ctx": CallContext}` 형태로 떨군다. 단일 파일이라
`services/transcripts.merge_transcripts`(ORM CallAudio 필요)는 쓰지 않고 같은 규칙으로 직접 조립한다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ...clients.stt import SttClient, SttResult, parse_diarized
from ...config import Settings
from ...schemas.pipeline import CallContext
from ...schemas.transcript import MergedTranscript, TranscriptFile, TranscriptSegment
from ...services.transcripts import _fmt_ts, transcript_markdown
from .cases import VoiceCase

log = logging.getLogger("voice_eval.transcribe")


def build_transcript(call_id: str, result: SttResult, *, key: str, bucket: str = "local") -> MergedTranscript:
    """단일 파일 STT 결과 → MergedTranscript (merge_transcripts 와 동일한 speaker_key/텍스트 규칙)."""
    duration = result.seconds or result.source_end_sec or (
        max((float(s.get("end") or 0.0) for s in result.segments), default=0.0))
    segments: list[TranscriptSegment] = []
    speakers: list[str] = []
    for s in sorted(result.segments, key=lambda x: (float(x.get("start") or 0.0), float(x.get("end") or 0.0))):
        text = (s.get("text") or "").lstrip(" ")
        if not text.strip():
            continue
        spk = str(s.get("speaker") or "?")
        skey = f"f0:{spk}"
        if skey not in speakers:
            speakers.append(skey)
        st = float(s.get("start") or 0.0)
        en = float(s.get("end") or st)
        segments.append(TranscriptSegment(
            file_index=0, audio_id=1, seg_id=str(s.get("id") or f"seg_{len(segments)}"),
            speaker=spk, speaker_key=skey, start=st, end=en, abs_start=st, abs_end=en, text=text))
    return MergedTranscript(
        call_id=call_id,
        files=[TranscriptFile(file_index=0, audio_id=1, bucket=bucket, key=key, seq=1,
                              duration_sec=duration, status="TRANSCRIBED")],
        segments=segments, speakers=speakers, total_duration_sec=duration,
        text="\n".join(f"[{_fmt_ts(s.abs_start)}] [{s.speaker_key}] {s.text}" for s in segments),
    )


async def transcribe(settings: Settings, audio: Path, *, num_speakers: int = 2) -> list:
    """게이트웨이 raw 응답(최상위 배열)을 그대로 돌려준다 — 캐시 원본."""
    client = SttClient(settings)
    try:
        result = await client.diarize(audio.read_bytes(), audio.name, num_speakers)
        return result.raw
    finally:
        await client.shutdown()


def write_fixture(case: VoiceCase, transcript: MergedTranscript, ctx: CallContext, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"transcript": json.loads(transcript.model_dump_json()), "ctx": json.loads(ctx.model_dump_json())}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def write_transcript_md(transcript: MergedTranscript, path: Path) -> None:
    path.write_text(transcript_markdown(transcript), encoding="utf-8")


def load_raw(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_raw(raw: list) -> SttResult:
    return parse_diarized(raw)
