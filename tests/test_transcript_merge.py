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


# --- merge_calls (날짜별 멀티콜 병합) ---------------------------------------

from app.db.models import Call  # noqa: E402
from app.services.transcripts import merge_calls  # noqa: E402


def _call(cid, started_at=None):
    return Call(call_id=cid, s3_prefix=f"agents/voicecall/{cid}", started_at=started_at)


def test_merge_calls_rebase_and_order():
    c1 = _call("c1", datetime(2026, 8, 20, 1, 0, tzinfo=UTC))
    c2 = _call("c2", datetime(2026, 8, 20, 3, 0, tzinfo=UTC))
    a1 = _audio(1, seq=1, stt_seconds=60)
    a2 = _audio(2, seq=1, stt_seconds=30)
    a3 = _audio(3, seq=2, stt_seconds=30)
    t = merge_calls("d1", [(c2, [a2, a3]), (c1, [a1])])   # 입력 순서 무관 — started_at 정렬
    assert t.call_id == "d1"
    assert [f.call_id for f in t.files] == ["c1", "c2", "c2"]
    assert [f.file_index for f in t.files] == [0, 1, 2]            # 전역 리베이스
    assert [f.offset_sec for f in t.files] == [0.0, 60.0, 90.0]
    assert t.total_duration_sec == 120.0
    # speaker_key 도 전역 file_index 네임스페이스 → 통화 간 충돌 없음
    assert t.speakers == ["f0:A", "f0:B", "f1:A", "f1:B", "f2:A", "f2:B"]
    # c2 첫 세그먼트의 abs 시간은 c1 길이(60s)만큼 밀림
    seg_c2 = next(s for s in t.segments if s.file_index == 1)
    assert seg_c2.abs_start == 60.0 and seg_c2.speaker_key == "f1:A"
    assert "[00:01:00] [f1:A] 안녕" in t.text


def test_merge_calls_null_started_at_last():
    c1 = _call("c1", datetime(2026, 8, 20, 1, 0, tzinfo=UTC))
    c0 = _call("c0", None)                                          # started_at 없음 → 뒤로
    t = merge_calls("d2", [(c0, [_audio(1, stt_seconds=5)]), (c1, [_audio(2, stt_seconds=5)])])
    assert [f.call_id for f in t.files] == ["c1", "c0"]


def test_merge_calls_single_call_equals_merge_transcripts():
    c1 = _call("c1", datetime(2026, 8, 20, 1, 0, tzinfo=UTC))
    audios = [_audio(1, seq=1, stt_seconds=60), _audio(2, seq=2, stt_seconds=30)]
    single = merge_transcripts("c1", audios)
    combined = merge_calls("d3", [(c1, audios)])
    assert [s.model_dump(exclude={"seg_id"}) for s in combined.segments] == \
           [s.model_dump(exclude={"seg_id"}) for s in single.segments]
    assert combined.total_duration_sec == single.total_duration_sec


# --- apply_speaker_map (역할 되먹임) -----------------------------------------

from app.services.transcripts import apply_speaker_map, transcript_markdown  # noqa: E402


def test_apply_speaker_map_fills_roles():
    t = merge_transcripts("c", [_audio(1, seq=1, stt_seconds=10)])
    assert all(s.role == "unknown" for s in t.segments)      # 병합 시점에는 역할을 모른다
    t2 = apply_speaker_map(t, {"f0:A": "consultant", "f0:B": "farmer"})
    assert t2.speaker_map == {"f0:A": "consultant", "f0:B": "farmer"}
    assert [s.role for s in t2.segments] == ["consultant", "farmer"]
    assert t2.model_dump(mode="json")["segments"][1]["role"] == "farmer"
    assert all(s.role == "unknown" for s in t.segments)      # 원본은 그대로 (순수 함수)
    # md 는 speaker_map 인자를 안 줘도 전사에 실린 역할을 쓴다
    assert "컨설턴트(f0:A):" in transcript_markdown(t2) and "농가(f0:B):" in transcript_markdown(t2)


def test_apply_speaker_map_unknown_and_missing():
    t = merge_transcripts("c", [_audio(1, seq=1, stt_seconds=10)])
    t2 = apply_speaker_map(t, {"f0:A": "farmer", "f9:Z": "consultant"})   # 없는 키는 무시
    assert t2.speaker_map == {"f0:A": "farmer", "f0:B": "unknown"}        # 빠진 화자는 unknown
    assert transcript_markdown(t2).count("농가(f0:A):") == 1
    assert "] f0:B: " in transcript_markdown(t2)                         # unknown 은 라벨을 안 붙인다
    assert apply_speaker_map(t, None).speaker_map == {"f0:A": "unknown", "f0:B": "unknown"}
