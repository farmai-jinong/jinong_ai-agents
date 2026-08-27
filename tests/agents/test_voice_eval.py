"""음성 테스트케이스 평가 하네스 — 네트워크 없이 파싱·채점·집계·게이트를 검증한다.

실제 전사/LLM 은 부르지 않는다. 게이트웨이 응답은 캔드 배열로, judge 는 FakeChatModel 로 대체한다.
"""

from __future__ import annotations

import json

import pytest

from app.agents.prompts.loader import load_system, render_user
from app.agents.run import load_fixture
from app.agents.schemas import DiaryJudgeOut
from app.agents.tools.fake_llm import FakeChatModel, detect_kind
from app.agents.voice_eval import audio as audio_mod
from app.agents.voice_eval import judge as judge_mod
from app.agents.voice_eval import report as report_mod
from app.agents.voice_eval import stt_score, transcribe
from app.agents.voice_eval.cases import case_names, load_case, load_cases, parse_script

DIMS = ("coverage", "faithfulness", "classification", "severity", "chatter", "format")


async def _no_sleep(_seconds):
    """재시도 백오프를 실제로 기다리지 않는다."""


class _Trace:
    total_tokens = 10


def _stt_raw(turns: list[tuple[str, str]], *, seconds: float = 120.0) -> list:
    """게이트웨이 diarize 응답 모양 — 최상위 배열 1개 chunk."""
    segs = [{"id": f"seg_{i}", "speaker": spk, "start": i * 5.0, "end": i * 5.0 + 4.0, "text": f" {txt}"}
            for i, (spk, txt) in enumerate(turns)]
    return [{"source_end_sec": seconds,
             "response": {"text": " ".join(t for _, t in turns), "segments": segs,
                          "usage": {"seconds": seconds}}}]


# --------------------------------------------------------------------------- 케이스 로딩 / 대본 파싱
def test_all_cases_load():
    cases = load_cases()
    assert len(cases) >= 5
    for c in cases:
        assert c.utterances, f"{c.name}: 발화가 하나도 안 뽑혔다"
        assert c.prdlst_code and c.diary_date, f"{c.name}: source.json 에서 작물/날짜를 못 읽었다"
        assert c.crop_name in ("딸기", "토마토")
        assert c.stt_keywords


def test_parse_script_takes_only_utterances():
    """머리말 표와 하단 '검증 포인트 매핑' 은 기준 텍스트에 섞이면 안 된다."""
    c = load_case("strawberry_microbial")
    ref = c.reference_text
    assert "목표 길이" not in ref and "기대 추출" not in ref and "|" not in ref
    assert "광합성균" in ref and "크린캡" in ref
    assert c.first_role == "farmer"
    assert {u.role for u in c.utterances} == {"farmer", "consultant"}


def test_parse_script_ignores_table_bold():
    md = "| **항목** | 값 |\n\n---\n\n**농장주:** 관수했어요.\n\n## 검증 포인트 매핑\n\n**농장주:** 표 뒤 발화\n"
    u = parse_script(md)
    assert [x.text for x in u] == ["관수했어요."]


def test_tomato_uses_merged_source():
    """tomato_harvest 는 original 이 없고 original_merged_from + enriched 만 있다."""
    c = load_case("tomato_harvest")
    assert c.prdlst_code == "0803MM" and c.diary_date == "2025-08-19"
    assert c.context(with_farmos=True).hints.prdlst_nm == "토마토"


# --------------------------------------------------------------------------- STT 채점
def test_cer_wer_basics():
    assert stt_score.cer("가나다 라마", "가나다 라마") == 0.0
    assert stt_score.wer("오늘 관수 했어요", "오늘 관수 했어요") == 0.0
    assert stt_score.cer("가나다라마", "가나다라바") == pytest.approx(0.2)
    assert stt_score.wer("오늘 관수 했어요", "오늘 관수를 했어요") == pytest.approx(1 / 3)
    assert stt_score.cer("", "아무거나") == 0.0          # 기준이 없으면 0 (0으로 나누지 않는다)


def test_keyword_exact_fuzzy_miss():
    hyp = "오늘 다코닐에이스 액상수화제랑 타코닐 비슷한 걸 뿌렸어요"
    assert stt_score.match_keyword("다코닐", hyp, "product").status == "exact"
    assert stt_score.match_keyword("마쿠피가", hyp, "product").status == "miss"
    # 자모 기준 근사 — 한 음절이 잘못 들린 정도는 잡고(음절 기준이면 66점이라 놓친다),
    # 두 음절이 어긋나면 다른 제품으로 본다.
    assert stt_score.match_keyword("다코닐", "다코날 뿌렸어요", "product").status == "fuzzy"
    assert stt_score.match_keyword("다코닐", "타코날 뿌렸어요", "product").status == "miss"


def test_keyword_matches_korean_numerals():
    """대본 '여섯 리터' ↔ 전사 '6리터' 가 같은 것으로 잡혀야 한다."""
    assert stt_score.match_keyword("6리터", "각 여섯 리터씩 넣었어요").status in ("exact", "fuzzy")
    assert stt_score.expand_numerals("물 천 리터") == "물 1000 리터"


def test_speaker_stats_detects_collapse():
    segs = [{"speaker": "A", "text": "가" * 90}, {"speaker": "B", "text": "나" * 10}]
    n, share = stt_score.speaker_stats(segs)
    assert n == 2 and share == pytest.approx(0.9)
    assert stt_score.speaker_stats([{"speaker": "A", "text": "가"}])[0] == 1


def test_score_excludes_keywords_never_spoken():
    """대본에 없는 표기(표준 명칭 `잿빛곰팡이병` — 실제 발화는 `잿빛곰팡이`)는 n/a 로 분모에서 빠진다.

    STT 가 만들어낼 수 없는 표기를 STT 점수로 깎으면 안 된다 — 표준 명칭 복원은 매핑 단계의 몫이다.
    """
    c = load_case("strawberry_botrytis_choice")
    raw = _stt_raw([("A", "잿빛곰팡이가 심해요"), ("B", "사파이어 액상수화제 이천배로 치세요")])
    res = transcribe.parse_raw(raw)
    sc = stt_score.score(reference=c.reference_text, hypothesis=res.text, keywords=c.stt_keywords,
                         expect=c.expect, segments=res.segments, duration_sec=120.0)
    assert sc.not_spoken == ["잿빛곰팡이병"]
    assert sc.misses == ["점박이응애"]                    # 대본엔 있는데 전사에서 빠짐 = 진짜 STT 실패
    assert sc.keyword_recall == pytest.approx(0.5)       # 분모 2 (사파이어·점박이응애)
    assert sc.n_speakers == 2


def test_score_all_keywords_na_is_full_marks():
    sc = stt_score.score(reference="관수했어요", hypothesis="관수했어요", keywords=["없는약제"],
                         expect={"products": ["없는약제"]}, segments=[{"speaker": "A", "text": "x"}],
                         duration_sec=1.0)
    assert sc.keyword_recall == 1.0 and sc.not_spoken == ["없는약제"]


# --------------------------------------------------------------------------- 픽스처 왕복
def test_fixture_roundtrip(tmp_path):
    """캔드 STT → fixture.json → run.py:load_fixture 로 되읽힌다 (eval.py 와 같은 계약)."""
    c = load_case("strawberry_microbial")
    res = transcribe.parse_raw(_stt_raw([("A", "미생물 관주했어요"), ("B", "연막은요?")], seconds=99.0))
    tr = transcribe.build_transcript(c.call_id(), res, key="a.wav")
    ctx = c.context(with_farmos=True)
    p = tmp_path / "fixture.json"
    transcribe.write_fixture(c, tr, ctx, p)

    tr2, ctx2 = load_fixture(str(p))
    assert tr2.call_id == c.call_id() and len(tr2.segments) == 2
    assert tr2.speakers == ["f0:A", "f0:B"]
    assert tr2.segments[0].text == "미생물 관주했어요"      # 선행 공백 제거
    assert tr2.total_duration_sec == 99.0
    assert ctx2.hints.prdlst_code == "0804MM"
    assert "[f0:A]" in tr2.text and not tr2.is_empty


def test_build_transcript_skips_blank_segments():
    res = transcribe.parse_raw(_stt_raw([("A", "  "), ("B", "관수했어요")]))
    tr = transcribe.build_transcript("c1", res, key="a.wav")
    assert [s.text for s in tr.segments] == ["관수했어요"] and tr.speakers == ["f0:B"]


# --------------------------------------------------------------------------- 일시 오류 견딤
@pytest.mark.asyncio
async def test_transcribe_retries_transient_and_raises_permanent(settings, monkeypatch, tmp_path):
    """일시 오류(전송 실패)는 재시도하고, 영구 오류(415 등)는 즉시 올린다.

    2026-08-27 에 `STT transport error` 한 번으로 tomato_harvest 가 통째로 빠져 그 실행의 점수가
    전부 비교 불가가 됐다 — 워커는 재시도하는데 하네스만 한 방에 죽던 문제.
    """
    from app.agents.voice_eval import transcribe as tr
    from app.clients.stt import SttError

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    monkeypatch.setattr(tr.asyncio, "sleep", _no_sleep)

    calls = {"n": 0}

    async def flaky(self, data, filename, num_speakers=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise SttError("STT transport error: ", permanent=False)
        return tr.parse_raw(_stt_raw([("A", "관수했어요")]))

    monkeypatch.setattr("app.clients.stt.SttClient.diarize", flaky)
    raw = await tr.transcribe(settings, audio, attempts=4)
    assert calls["n"] == 3 and raw[0]["response"]["segments"]

    async def unsupported(self, data, filename, num_speakers=None):
        calls["n"] += 1
        raise SttError("STT 415", permanent=True, status=415)

    calls["n"] = 0
    monkeypatch.setattr("app.clients.stt.SttClient.diarize", unsupported)
    with pytest.raises(SttError):
        await tr.transcribe(settings, audio, attempts=4)
    assert calls["n"] == 1                                    # 영구 오류는 재시도하지 않는다


@pytest.mark.asyncio
async def test_transcribe_gives_up_after_attempts(settings, monkeypatch, tmp_path):
    from app.agents.voice_eval import transcribe as tr
    from app.clients.stt import SttError

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    monkeypatch.setattr(tr.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    async def always_fail(self, data, filename, num_speakers=None):
        calls["n"] += 1
        raise SttError("boom", permanent=False)

    monkeypatch.setattr("app.clients.stt.SttClient.diarize", always_fail)
    with pytest.raises(SttError):
        await tr.transcribe(settings, audio, attempts=3)
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_judge_retries_then_tolerates_partial_failure(settings, monkeypatch):
    """일시 오류는 재시도하고, repeat 중 일부만 성공해도 성공분으로 집계한다."""
    monkeypatch.setattr(judge_mod.asyncio, "sleep", _no_sleep)
    c = load_case("tomato_harvest")
    calls = {"n": 0}

    async def flaky(llm, case, diary, transcript, *, settings, dump_dir=None):
        calls["n"] += 1
        if calls["n"] in (1, 2):
            raise RuntimeError("503 upstream")
        return DiaryJudgeOut(**_verdict({"coverage": 4}, overall=4)), _Trace()

    monkeypatch.setattr(judge_mod, "judge_once", flaky)
    out = await judge_mod.judge(None, c, "# 산출", "전사", settings=settings, repeat=1, attempts=3)
    assert out["runs"] == 1 and out["overall"] == 4 and calls["n"] == 3


@pytest.mark.asyncio
async def test_judge_raises_when_every_attempt_fails(settings, monkeypatch):
    monkeypatch.setattr(judge_mod.asyncio, "sleep", _no_sleep)
    c = load_case("tomato_harvest")

    async def always_fail(llm, case, diary, transcript, *, settings, dump_dir=None):
        raise RuntimeError("503 upstream")

    monkeypatch.setattr(judge_mod, "judge_once", always_fail)
    with pytest.raises(RuntimeError, match="채점이"):
        await judge_mod.judge(None, c, "# 산출", "전사", settings=settings, repeat=2, attempts=2)


# --------------------------------------------------------------------------- 오디오 매칭
def test_match_by_name_and_audio_map(tmp_path):
    files = [tmp_path / "녹음대본_tomato_harvest.m4a", tmp_path / "통화 아무개_260825.m4a"]
    for f in files:
        f.write_bytes(b"")
    m = audio_mod.match_by_name(["tomato_harvest", "strawberry_planting"], files,
                                {"strawberry_planting": "통화 아무개_260825.m4a"})
    assert m["tomato_harvest"].name == "녹음대본_tomato_harvest.m4a"
    assert m["strawberry_planting"].name == "통화 아무개_260825.m4a"


def test_resolve_by_similarity_is_one_to_one():
    refs = {c.name: c.reference_text for c in load_cases(["strawberry_microbial", "tomato_harvest"])}
    hyp = {"a.m4a": refs["tomato_harvest"][:400], "b.m4a": refs["strawberry_microbial"][:400]}
    assert audio_mod.resolve_by_similarity(hyp, refs) == {"a.m4a": "tomato_harvest",
                                                          "b.m4a": "strawberry_microbial"}


# --------------------------------------------------------------------------- judge
def _verdict(scores: dict[str, int], items: list[dict] | None = None, overall: int = 4) -> dict:
    return {"dimensions": [{"name": n, "score": scores.get(n, 5), "reason": ""} for n in DIMS],
            "items": items or [], "overall": overall, "summary": "총평"}


def test_judge_prompt_is_detectable_and_has_no_generation_preamble():
    sysmsg = load_system("judge_diary", preamble=False)
    assert detect_kind([type("M", (), {"type": "system", "content": sysmsg})()]) == "judge_diary"
    assert "[공통 규칙]" not in sysmsg
    assert "[공통 규칙]" in load_system("verify_diary")       # 생성 프롬프트는 그대로


def test_judge_user_prompt_carries_all_three_inputs():
    c = load_case("strawberry_microbial")
    txt = render_user("judge_diary", crop_name=c.crop_name, case_name=c.name,
                      expected_diary=c.expected_diary, actual_diary="# 산출", transcript_text="[f0:A] 관수")
    assert "정답 기준" in txt and "# 산출" in txt and "[f0:A] 관수" in txt


@pytest.mark.asyncio
async def test_judge_parses_and_writes_items(settings):
    c = load_case("strawberry_microbial")
    items = [{"kind": "missing", "section": "방제이력", "text": "크린캡 누락", "cause": "extraction"}]
    llm = FakeChatModel(responses={"judge_diary": _verdict({"coverage": 3}, items, overall=3)})
    out = await judge_mod.judge(llm, c, "# 산출 일지", "[f0:A] 관수했어요", settings=settings)
    assert out["overall"] == 3 and out["dimensions"]["coverage"] == 3
    assert out["items"] == items and out["runs"] == 1


@pytest.mark.asyncio
async def test_judge_repeat_takes_median_and_unions_items(settings):
    c = load_case("tomato_harvest")
    seq = [_verdict({"coverage": 2}, [{"kind": "missing", "section": "병해충", "text": "x", "cause": "stt"}], 2),
           _verdict({"coverage": 4}, [{"kind": "missing", "section": "병해충", "text": "x", "cause": "stt"}], 4),
           _verdict({"coverage": 4}, [{"kind": "hallucinated", "section": "병해충", "text": "y",
                                       "cause": "extraction"}], 4)]
    calls = {"n": 0}

    def pick(_messages):
        v = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return v

    llm = FakeChatModel(responses={"judge_diary": pick})
    out = await judge_mod.judge(llm, c, "# 산출", "전사", settings=settings, repeat=3)
    assert out["runs"] == 3 and out["overall"] == 4 and out["dimensions"]["coverage"] == 4
    assert len(out["items"]) == 2                        # 같은 항목은 한 번만


def test_cause_matrix_counts():
    j = [{"items": [{"kind": "missing", "cause": "stt"}, {"kind": "missing", "cause": "extraction"}]},
         {"items": [{"kind": "misclassified", "cause": "extraction"}, {"kind": "chatter_leak", "cause": "x"}]}]
    m = judge_mod.cause_matrix(j)
    assert m["stt"]["missing"] == 1 and m["extraction"]["missing"] == 1
    assert m["extraction"]["misclassified"] == 1
    assert sum(sum(v.values()) for v in m.values()) == 3   # 알 수 없는 cause 는 버린다


def test_make_judge_llm_does_not_mutate_settings(settings):
    settings.llm_provider, settings.llm_model = "openai", "gpt-4.1"
    judge_mod.make_judge_llm(settings, "openai", "gpt-4.1-mini")
    assert settings.llm_model == "gpt-4.1"               # 원본 Settings 는 그대로


# --------------------------------------------------------------------------- 집계 / 게이트 / 리포트
def _row(case: str, *, kw=1.0, facts=1.0, sev=(1, 1), overall=5, faith=5, status_ok=True, items=None):
    return {
        "case": case,
        "stt": {"keyword_recall": kw, "cer": 0.1, "wer": 0.2, "n_speakers": 2, "top_speaker_share": 0.5,
                "n_segments": 10, "duration_sec": 100.0, "similarity": 90.0,
                "keywords": [{"keyword": "다코닐", "status": "miss", "score": 40.0}] if kw < 1 else []},
        "pipeline": {"farmworks_recall": [1, 1], "pests_recall": [1, 1], "products_recall": [1, 1],
                     "facts_recall": facts, "severity_exact": list(sev),
                     "severity_ratio": sev[0] / sev[1] if sev[1] else 1.0, "evidence_valid": [3, 3],
                     "mapped": 3, "diaries": {"0804MM": "OK"}, "diary_status_ok": status_ok,
                     "speaker_role_ok": True, "speaker_map": {"f0:A": "farmer"}, "farmos_status": "ok",
                     "warnings": [], "model": "m", "tokens": 100, "calls": 4},
        "judge": {"dimensions": {d: (faith if d == "faithfulness" else 5) for d in DIMS},
                  "dimension_reasons": {d: "" for d in DIMS}, "items": items or [], "overall": overall,
                  "summary": "총평", "runs": 1, "tokens": 50, "model": "judge-m"},
    }


def test_gate_passes_and_fails():
    th = report_mod.load_thresholds()
    good = report_mod.aggregate([_row("a"), _row("b")])
    assert report_mod.gate(good, th) == []

    bad = report_mod.aggregate([_row("a", kw=0.5, facts=0.4, overall=2, faith=1, status_ok=False)])
    fails = report_mod.gate(bad, th)
    assert any("stt_keyword_recall" in f for f in fails)
    assert any("facts_recall" in f for f in fails)
    assert any("judge_overall_mean" in f for f in fails)
    assert any("faithfulness" in f for f in fails)
    assert any("diary_status" in f for f in fails)


def test_gate_flags_errored_case():
    summary = report_mod.aggregate([_row("a"), {"case": "b", "error": "SttError: 502"}])
    assert any("실행 실패" in f for f in report_mod.gate(summary, report_mod.load_thresholds()))


def test_severity_excluded_when_no_expectation():
    """기대 단계가 없는 케이스는 평균을 끌어내리면 안 된다."""
    s = report_mod.aggregate([_row("a", sev=(0, 0)), _row("b", sev=(1, 1))])
    assert s["severity_exact"] == 1.0


def test_report_renders_cause_matrix_and_misses():
    items = [{"kind": "missing", "section": "방제이력", "text": "크린캡 누락", "cause": "extraction"}]
    rows = [_row("a", kw=0.5, items=items), {"case": "b", "error": "RuntimeError: boom"}]
    summary = report_mod.aggregate(rows)
    th = report_mod.load_thresholds()
    md = report_mod.render(rows, summary, th, None, report_mod.gate(summary, th))
    assert "## 2. 원인 귀속 집계" in md
    assert "**extraction**" in md
    assert "다코닐" in md                                  # 미인식 핵심어 목록
    assert "ERROR: RuntimeError" in md
    assert "[missing · extraction] (방제이력) 크린캡 누락" in md


def test_report_baseline_delta():
    cur = report_mod.aggregate([_row("a", facts=0.9)])
    base = report_mod.aggregate([_row("a", facts=0.7)])
    md = report_mod.render([_row("a", facts=0.9)], cur, report_mod.load_thresholds(), base, [])
    assert "▲0.200" in md


def test_thresholds_file_is_valid():
    th = json.loads(report_mod.THRESHOLDS_PATH.read_text(encoding="utf-8"))
    assert set(th) - {"_comment"} == set(report_mod.DEFAULT_THRESHOLDS)


def test_case_names_stable():
    assert "strawberry_botrytis_choice" in case_names()
