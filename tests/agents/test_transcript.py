from app.agents.nodes.prepare_transcript import build_turns
from app.agents.tools.transcript import chunk_turns, excerpt, fmt_ts, format_turns
from app.schemas.transcript import MergedTranscript, TranscriptSegment

from .conftest import load_call


def seg(fi, sp, start, end, text, i=0):
    return TranscriptSegment(file_index=fi, audio_id=fi + 1, seg_id=f"seg_{i}", speaker=sp, speaker_key=f"f{fi}:{sp}",
                             start=start, end=end, abs_start=start + fi * 100, abs_end=end + fi * 100, text=text)


def test_merge_consecutive_same_speaker_and_global_tid():
    tr = MergedTranscript(call_id="c", segments=[
        seg(0, "A", 0, 2, " 안녕하세요", 0), seg(0, "A", 2.5, 4, "저희 딸기가", 1), seg(0, "B", 4.5, 6, "네", 2),
        seg(1, "A", 0, 2, "다시 걸었어요", 0)])
    nt = build_turns(tr)
    assert [t.tid for t in nt.turns] == [0, 1, 2]
    assert nt.turns[0].text == "안녕하세요 저희 딸기가" and nt.turns[0].seg_ids == ["seg_0", "seg_1"]
    assert nt.turns[2].file_index == 1 and nt.n_files == 2


def test_drop_physically_implausible_segment():
    dump = "그을음병, 담배가루이, 온동네, 아리랑, 검객, 신청, 영신, 하우스, 라벨, 안나."
    tr = MergedTranscript(call_id="c", segments=[
        seg(0, "A", 49.04, 49.34, dump, 6),                                   # 0.3s / 45자 → 누출
        seg(0, "A", 50.64, 54.12, "폭치면 날아오르는 하얀 벌레면 담배가루이 맞아요.", 7),  # 3.5s / 24자
        seg(0, "B", 54.32, 54.80, "네, 알겠습니다", 8),                          # 0.5s / 7자
        seg(0, "A", 20.92, 27.81, "수확은 매일 하지 오늘 새벽에도 닦고요 요즘 하루에 한 키 백 킬로씩 나와요.", 5)])
    nt = build_turns(tr)
    assert [t.seg_ids[0] for t in nt.turns] == ["seg_5", "seg_7", "seg_8"]
    assert len(nt.dropped) == 1 and nt.dropped[0].startswith("seg_6 0.3s 47자")


def test_no_merge_when_gap_large():
    tr = MergedTranscript(call_id="c", segments=[seg(0, "A", 0, 2, "가", 0), seg(0, "A", 5, 6, "나", 1)])
    assert len(build_turns(tr).turns) == 2


def test_format_and_ts():
    assert fmt_ts(65) == "01:05" and fmt_ts(3661) == "01:01:01"
    tr, _ = load_call("strawberry_botrytis")
    nt = build_turns(tr)
    text = format_turns(nt.turns, nt.n_files)
    assert text.startswith("#0 [00:00] 화자A:")


def test_chunk_turns_overlap():
    tr, _ = load_call("strawberry_botrytis")
    nt = build_turns(tr)
    chunks = chunk_turns(nt.turns, max_tokens=120, overlap=2)
    assert len(chunks) >= 2
    assert chunks[1][0].tid == chunks[0][-2].tid          # 앞 청크 마지막 2턴 겹침
    assert chunks[-1][-1].tid == nt.turns[-1].tid


def test_excerpt_shape():
    tr, _ = load_call("strawberry_botrytis")
    nt = build_turns(tr)
    ex = excerpt(nt.turns, head=3, mid=2, tail=2)
    assert len(ex) == 7 and ex[0].tid == 0 and ex[-1].tid == nt.turns[-1].tid
