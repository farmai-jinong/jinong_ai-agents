from datetime import UTC, datetime

from app.db.models import CallAudio
from app.services.transcripts import merge_transcripts


def _audio(id, seq=None, recorded_at=None, stt_seconds=None, duration_sec=None, segs=None, status="TRANSCRIBED"):
    a = CallAudio(call_id="c", bucket="b", key=f"k{id}", seq=seq, recorded_at=recorded_at,
                  stt_seconds=stt_seconds, duration_sec=duration_sec, status=status,
                  segments_json=segs if segs is not None else [
                      {"id": "seg_0", "speaker": "A", "start": 0.0, "end": 2.0, "text": " 안녕"},
                      {"id": "seg_1", "speaker": "B", "start": 2.5, "end": 4.0, "text": " 네"}])
    a.id = id
    return a


def test_order_and_offsets():
    a2 = _audio(2, seq=2, stt_seconds=30)
    a1 = _audio(1, seq=1, stt_seconds=60)
    a3 = _audio(3, seq=None, duration_sec=10)     # seq 없음 → 뒤로
    t = merge_transcripts("c", [a2, a3, a1])
    assert [f.audio_id for f in t.files] == [1, 2, 3]
    assert [f.offset_sec for f in t.files] == [0.0, 60.0, 90.0]
    assert t.total_duration_sec == 100.0
    assert t.segments[0].speaker_key == "f0:A" and t.segments[2].speaker_key == "f1:A"
    assert t.segments[2].abs_start == 60.0
    assert t.segments[0].text == "안녕"                     # 선행 공백 1개 제거
    assert t.speakers == ["f0:A", "f0:B", "f1:A", "f1:B", "f2:A", "f2:B"]
    assert "[00:01:00] [f1:A] 안녕" in t.text


def test_recorded_at_ordering_and_failed_file():
    t0 = datetime(2026, 8, 19, 1, 0, tzinfo=UTC)
    t1 = datetime(2026, 8, 19, 1, 5, tzinfo=UTC)
    later = _audio(1, recorded_at=t1, stt_seconds=5)
    earlier = _audio(2, recorded_at=t0, stt_seconds=5)
    failed = _audio(3, recorded_at=t1, status="FAILED", segs=[])
    failed.last_error = "boom"
    t = merge_transcripts("c", [later, earlier, failed])
    assert [f.audio_id for f in t.files] == [2, 1, 3]
    assert t.files[2].status == "FAILED" and t.files[2].error == "boom"
    assert len(t.segments) == 4


def test_empty():
    t = merge_transcripts("c", [_audio(1, segs=[{"speaker": "A", "start": 0, "end": 1, "text": "   "}])])
    assert t.is_empty
