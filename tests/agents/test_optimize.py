"""자가 개선 루프 — 네트워크·LLM 없이 진단·게이트·판정·저널을 검증한다.

Claude 세션은 부르지 않는다(응답 봉투를 캔드로 파싱만 확인). 측정도 부르지 않는다(Measurement 를 직접 만든다).
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from app.agents.voice_eval import report as report_mod
from app.agents.voice_eval.optimize import decide, gates, propose
from app.agents.voice_eval.optimize.diagnose import _section_of, briefing, pick_target
from app.agents.voice_eval.optimize.journal import Journal, Record, render_record
from app.agents.voice_eval.optimize.retro import _slug

DIMS = ("coverage", "faithfulness", "classification", "severity", "chatter", "format")


def _item(cause="extraction", kind="missing", section="기타 기록사항", text="뭔가 누락"):
    return {"cause": cause, "kind": kind, "section": section, "text": text}


def _case_row(case, items, *, facts=0.9, sev=(1, 1), overall=3):
    return {
        "case": case,
        "stt": {"keyword_recall": 1.0, "cer": 0.1, "wer": 0.2, "n_speakers": 2, "top_speaker_share": 0.5,
                "n_segments": 10, "duration_sec": 100.0, "similarity": 90.0, "keywords": []},
        "pipeline": {"farmworks_recall": [1, 1], "pests_recall": [1, 1], "products_recall": [1, 1],
                     "facts_recall": facts, "severity_exact": list(sev),
                     "severity_ratio": sev[0] / sev[1] if sev[1] else 1.0, "evidence_valid": [3, 3],
                     "mapped": 3, "diaries": {"0804MM": "OK"}, "diary_status_ok": True,
                     "speaker_role_ok": True, "speaker_map": {}, "farmos_status": "ok", "warnings": [],
                     "model": "m", "tokens": 100, "calls": 4},
        "judge": {"dimensions": dict.fromkeys(DIMS, 4), "dimension_reasons": dict.fromkeys(DIMS, ""),
                  "items": items, "overall": overall, "summary": "총평", "runs": 1, "tokens": 50,
                  "model": "judge-m"},
    }


def _summary(rows):
    return {"summary": report_mod.aggregate(rows), "cases": rows}


def _journal(tmp_path) -> Journal:
    return Journal(tmp_path / "j.jsonl", tmp_path / "j.md")


def _measure(composite, *, judge=3.0, facts=0.9, sev=0.67, cells=None, errors=None, status_ok=True,
             dims=None):
    return decide.Measurement(composite, judge, facts, sev, status_ok, errors or [], cells or {}, 0, {},
                              dims or dict.fromkeys(DIMS, 4.0))


# --------------------------------------------------------------------------- 종합점수
def test_composite_weights_and_missing_stage():
    assert report_mod.composite(1.0, 1.0, 5.0) == pytest.approx(1.0)
    assert report_mod.composite(0.0, 0.0, 0.0) == pytest.approx(0.0)
    # judge 나 재현율이 미측정이면 점수 자체가 없다 — 0점으로 오인해 게이트가 터지면 안 된다
    assert report_mod.composite(0.9, 0.6, None) is None
    assert report_mod.composite(None, 0.6, 3.0) is None
    # 발생단계 기대가 없는 경우는 만점 취급
    assert report_mod.composite(1.0, None, 5.0) == pytest.approx(1.0)


def test_composite_judge_term_is_finer_than_noise_band():
    """judge 항이 노이즈 밴드와 같은 눈금으로 양자화되면 검출하려는 개선이 묻힌다(저널 #1).

    총점(케이스당 1~5 정수 5개)은 평균 눈금 0.2 → 종합점수 0.02 = 노이즈 밴드 전체였다.
    축 평균(6축×5케이스=30개)은 눈금이 1/30 이라 종합점수 기여가 밴드의 1/6 이다.
    """
    band = 0.02
    overall_step = report_mod.composite(0.9, 0.7, 3.2) - report_mod.composite(0.9, 0.7, 3.0)
    assert overall_step == pytest.approx(band)               # 총점을 쓰면 눈금 = 밴드
    dim_step = report_mod.composite(0.9, 0.7, 3 + 1 / 30) - report_mod.composite(0.9, 0.7, 3.0)
    assert dim_step < band / 5                                # 축 평균은 충분히 미세하다


def test_aggregate_exposes_dimension_mean_and_item_count():
    rows = [_case_row("a", [_item(), _item()]), _case_row("b", [_item()])]
    s = report_mod.aggregate(rows)
    assert s["judge_dimension_mean"] == pytest.approx(4.0)     # 모든 축 4점
    assert s["judge_items"] == 3
    assert s["composite"] == pytest.approx(report_mod.composite(s["facts_recall"], s["severity_exact"], 4.0))


# --------------------------------------------------------------------------- 타깃 셀 선정
def test_pick_target_picks_largest_cell(tmp_path):
    rows = [_case_row("a", [_item(), _item(), _item(section="병해충")]),
            _case_row("b", [_item()])]
    t = pick_target(_summary(rows), _journal(tmp_path))
    assert t.cell == "extraction/missing/기타 기록사항" and t.count == 3
    assert {i["case"] for i in t.items} == {"a", "b"}


def test_pick_target_skips_stt_cause(tmp_path):
    """전사에 없는 내용은 프롬프트로 못 고친다 — 타깃이 되면 안 된다."""
    rows = [_case_row("a", [_item(cause="stt"), _item(cause="stt"), _item(cause="stt"),
                            _item(cause="mapping", section="병해충")])]
    t = pick_target(_summary(rows), _journal(tmp_path))
    assert t.cause == "mapping"


def test_pick_target_respects_cooldown(tmp_path):
    rows = [_case_row("a", [_item(), _item(), _item(section="병해충")])]
    j = _journal(tmp_path)
    j.append(Record(iter=1, ts="t", base_commit="c",
                    target={"cell": "extraction/missing/기타 기록사항"}, decision="rejected"))
    t = pick_target(_summary(rows), j, cooldown=3)
    assert t.section == "병해충"                              # 방금 실패한 셀은 건너뛴다
    assert pick_target(_summary(rows), j, cooldown=0).section == "기타 기록사항"   # 쿨다운 0이면 다시 잡는다


def test_pick_target_none_when_no_items(tmp_path):
    assert pick_target(_summary([_case_row("a", [])]), _journal(tmp_path)) is None


def test_forced_cell_overrides(tmp_path):
    rows = [_case_row("a", [_item(), _item()])]
    t = pick_target(_summary(rows), _journal(tmp_path), forced="mapping/misclassified/병해충")
    assert t.cell == "mapping/misclassified/병해충" and t.cause == "mapping"


# --------------------------------------------------------------------------- 브리핑
def test_briefing_contains_items_prompt_and_history(tmp_path):
    rows = [_case_row("strawberry_microbial", [_item(text="크린캡 혼용 누락")])]
    j = _journal(tmp_path)
    j.append(Record(iter=1, ts="t", base_commit="c", target={"cell": "x"},
                    hypothesis="지난 가설", decision="rejected", reason="효과 없음"))
    t = pick_target(_summary(rows), j, cooldown=0)
    b = briefing(t, _summary(rows), j, tmp_path, "(allowlist)")
    assert "크린캡 혼용 누락" in b
    assert "구조화된 사실(CallFacts)" in b                     # extract.system.md 원문이 붙는다
    assert "지난 가설" in b and "효과 없음" in b               # 같은 가설 반복 방지
    assert "정답 기준 해당 섹션" in b


def test_section_extraction():
    md = "# 제목\n\n## 병해충\n- 가\n- 나\n\n## 방제이력\n- 다\n"
    assert _section_of(md, "병해충") == "## 병해충\n- 가\n- 나\n"
    assert _section_of(md, "방제이력") == "## 방제이력\n- 다"
    assert _section_of(md, "없는섹션") == ""


# --------------------------------------------------------------------------- 게이트
@pytest.mark.parametrize("path,ok", [
    ("app/agents/prompts/extract.system.md", True),
    ("app/agents/prompts/diary_content.user.md.j2", True),
    ("app/agents/mapping/synonyms.yaml", True),
    ("app/agents/mapping/severity.yaml", True),
    ("app/agents/prompts/judge_diary.system.md", False),      # 채점 기준 = 과녁
    ("app/agents/voice_eval/report.py", False),               # 채점 하네스
    ("tests/agents/test_graph.py", False),                    # 홀드아웃
    ("app/agents/nodes/extract_facts.py", False),             # Tier C
    ("app/config.py", False),
    (".env", False),
])
def test_gate_paths(path, ok):
    assert gates.gate_paths([path]).ok is ok


def test_gate_paths_rejects_empty_change():
    assert not gates.gate_paths([]).ok


def test_generalization_blocks_hardcoded_answers():
    """정답의 상표명을 프롬프트에 박으면 거부 — 5케이스 과적합의 가장 쉬운 길을 막는다."""
    bad = "+++ b/x\n+ 사파이어는 잿빛곰팡이병 방제에 쓴다\n"
    assert not gates.gate_generalization(bad).ok
    good = "+++ b/x\n+ 농가가 사용을 거부한 약제는 투입 제품에 넣지 않는다\n"
    assert gates.gate_generalization(good).ok


def test_generalization_allows_shared_vocabulary():
    """동의어 사전에 이미 있는 표준명·제형어는 공용 어휘라 과적합이 아니다."""
    assert gates.gate_generalization("+++ b/x\n+ 액상수화제 같은 제형어는 제거한다\n").ok
    assert gates.gate_generalization("+++ b/x\n+ 딸기·토마토 등 작물명은 목록에서 고른다\n").ok


def test_generalization_ignores_deleted_lines():
    assert gates.gate_generalization("+++ b/x\n-사파이어 예시를 지웠다\n").ok


def test_case_tokens_are_case_specific():
    toks = gates.case_tokens()
    assert {"사파이어", "다코닐", "크린캡"} <= toks
    assert not ({"딸기", "농약", "액상수화제"} & toks)


def test_gate_fixture_recall():
    before = {"a": [3, 3], "b": [2, 2]}
    assert gates.gate_fixture_recall(before, {"a": [3, 3], "b": [2, 2]}).ok
    assert gates.gate_fixture_recall(before, {"a": [3, 3], "b": [3, 3]}).ok      # 개선은 통과
    assert not gates.gate_fixture_recall(before, {"a": [2, 3], "b": [2, 2]}).ok
    assert not gates.gate_fixture_recall(before, {"a": [3, 3]}).ok               # 픽스처 실종


def test_summarize_reports_first_failure():
    ok, d, reason = gates.summarize([gates.GateResult("path", True),
                                     gates.GateResult("pytest", False, "2 failed")])
    assert not ok and d["path"] is True and d["pytest"] == "2 failed"
    assert reason == "[pytest] 2 failed"


# --------------------------------------------------------------------------- 판정
def test_screen_only_filters_clear_regressions():
    """스크리닝(judge 1회)은 확정(3회)보다 잡음이 커서 같은 문턱을 요구하면 진짜 개선이 못 올라온다.

    명백한 악화와 "타깃을 못 고침"만 싸게 쳐내고, 판단은 확정 단계에 맡긴다.
    """
    base = _measure(0.68, cells={"X": 5})
    assert decide.screen(_measure(0.72, cells={"X": 2}), base, "X", 0.02).accept
    assert decide.screen(_measure(0.681, cells={"X": 2}), base, "X", 0.02).accept   # 밴드 미만도 통과
    assert decide.screen(_measure(0.675, cells={"X": 2}), base, "X", 0.02).accept   # 밴드/2 안쪽 하락도
    assert not decide.screen(_measure(0.60, cells={"X": 2}), base, "X", 0.02).accept  # 명백한 악화
    assert not decide.screen(_measure(0.72, cells={"X": 7}), base, "X", 0.02).accept  # 타깃 악화


def test_screen_rejects_errored_run():
    base = _measure(0.68)
    assert not decide.screen(_measure(0.9, errors=["a"]), base, "X", 0.02).accept


def test_confirm_rejects_drop_in_protected_dimensions():
    """종합점수가 올라도 정직성·잡담 축이 떨어지면 거부.

    judge 총점을 종합점수에서 뺀 대신, 총점이 담당하던 "없는 말을 지어내거나 잡담을 섞어 코버리지를
    사는 것"을 여기서 하드 가드로 막는다.
    """
    base = _measure(0.68, cells={"X": 5}, dims={**dict.fromkeys(DIMS, 4.0), "faithfulness": 4.0})
    worse_faith = _measure(0.75, cells={"X": 2}, dims={**dict.fromkeys(DIMS, 4.0), "faithfulness": 3.0})
    v = decide.confirm(worse_faith, base, "X", 0.02)
    assert not v.accept and "faithfulness" in v.reason

    worse_chatter = _measure(0.75, cells={"X": 2}, dims={**dict.fromkeys(DIMS, 4.0), "chatter": 3.0})
    assert not decide.confirm(worse_chatter, base, "X", 0.02).accept
    # 보호 대상이 아닌 축이 떨어지는 것은 종합점수로 판단한다
    other = _measure(0.75, cells={"X": 2}, dims={**dict.fromkeys(DIMS, 4.0), "format": 3.0})
    assert decide.confirm(other, base, "X", 0.02).accept


def test_from_summary_maps_dimension_mean_not_overall():
    rows = [_case_row("a", [_item()], overall=1)]              # 총점은 1점이지만 축은 전부 4점
    m = decide.from_summary(report_mod.aggregate(rows))
    assert m.judge == pytest.approx(4.0)
    assert m.dimensions["faithfulness"] == pytest.approx(4.0)


def test_confirm_rejects_deterministic_regression():
    """judge 점수가 올라도 LLM 무관 지표가 떨어지면 거부 — judge 게이밍 방지의 핵심."""
    base = _measure(0.68, facts=0.9, sev=0.67, cells={"X": 5})
    assert not decide.confirm(_measure(0.75, facts=0.85, cells={"X": 2}), base, "X", 0.02).accept
    assert not decide.confirm(_measure(0.75, sev=0.5, cells={"X": 2}), base, "X", 0.02).accept
    assert not decide.confirm(_measure(0.75, cells={"X": 2}, status_ok=False), base, "X", 0.02).accept


def test_confirm_requires_target_cell_not_growing():
    base = _measure(0.68, cells={"X": 5})
    assert not decide.confirm(_measure(0.75, cells={"X": 7}), base, "X", 0.02).accept
    assert decide.confirm(_measure(0.75, cells={"X": 5}), base, "X", 0.02).accept      # 동률은 허용


def test_confirm_accepts_real_improvement():
    base = _measure(0.68, cells={"X": 5})
    v = decide.confirm(_measure(0.75, facts=0.95, cells={"X": 1}), base, "X", 0.02)
    assert v.accept and "0.6800" in v.reason and "5 → 1" in v.reason


def test_measurement_row_for_journal():
    base = _measure(0.68, cells={"X": 5})
    row = _measure(0.72, cells={"X": 2}).row("X", base)
    assert row["target_before"] == 5 and row["target_after"] == 2 and row["base_composite"] == 0.68


# --------------------------------------------------------------------------- 저널
def test_journal_roundtrip_and_state(tmp_path):
    j = _journal(tmp_path)
    assert j.next_iter == 1 and j.consecutive_rejects() == 0
    j.append(Record(iter=1, ts="t", base_commit="c", target={"cell": "A"}, hypothesis="h1",
                    decision="accepted", reason="좋음", tokens={"judge": 100}))
    j.append(Record(iter=2, ts="t", base_commit="c", target={"cell": "B"}, hypothesis="h2",
                    decision="rejected", reason="나쁨", tokens={"judge": 50}))
    reloaded = Journal(tmp_path / "j.jsonl", tmp_path / "j.md")
    assert reloaded.next_iter == 3
    assert reloaded.consecutive_rejects() == 1
    assert reloaded.spent_tokens() == 150
    assert reloaded.cooled_cells(3) == {"B"}                  # 수락된 A 는 쿨다운 대상이 아니다
    assert [t["hypothesis"] for t in reloaded.tried_hypotheses()] == ["h1", "h2"]
    md = (tmp_path / "j.md").read_text(encoding="utf-8")
    assert "자가 개선 루프 저널" in md and "h1" in md and "✅ 수락" in md and "❌ 거부" in md


def test_journal_plateau_counts_errors_too(tmp_path):
    j = _journal(tmp_path)
    for i, d in enumerate(["rejected", "error", "rejected"], start=1):
        j.append(Record(iter=i, ts="t", base_commit="c", decision=d))
    assert j.consecutive_rejects() == 3


def test_render_record_shows_metric_table():
    r = Record(iter=1, ts="t", base_commit="abcdef1234", target={"cell": "X", "count": 5},
               hypothesis="h", files=["a.md"], gates={"path": True},
               screen={"composite": 0.7, "base_composite": 0.68, "judge": 3.0,
                       "target_before": 5, "target_after": 3},
               decision="accepted", reason="좋음", commit="fedcba9876")
    md = render_record(r)
    assert "▲0.0200" in md and "5 → 3" in md and "`abcdef123`" in md and "`fedcba987`" in md


def test_render_record_lists_failed_gates():
    r = Record(iter=1, ts="t", base_commit="c", gates={"path": True, "generalization": "하드코딩"},
               decision="rejected", reason="x")
    assert "실패 — generalization" in render_record(r)


# --------------------------------------------------------------------------- 제안 파싱
def _envelope(result, **kw):
    return json.dumps({"result": result, "session_id": "sid",
                       "usage": {"input_tokens": 10, "output_tokens": 5}, **kw})


def test_parse_proposal_from_fenced_json():
    inner = {"hypothesis": "h", "why": "w", "files": ["a.md"], "expected": {"from": 5, "to": 2}}
    p = propose.parse_response(_envelope("설명\n```json\n" + json.dumps(inner) + "\n```"))
    assert p.ok and p.hypothesis == "h" and p.files == ["a.md"] and p.tokens == 15


def test_parse_proposal_failures():
    assert not propose.parse_response(_envelope("JSON 없는 산문")).ok
    assert not propose.parse_response(_envelope("x", is_error=True)).ok
    assert not propose.parse_response("not json").ok
    assert not propose.parse_response(_envelope('{"why": "hypothesis 가 없다"}')).ok


def test_system_rules_state_the_hard_constraints():
    """규칙 문구가 사라지면 제안자가 정답을 하드코딩하기 시작한다 — 문구 자체를 고정한다."""
    for phrase in ("allowlist", "하나만", "최소 변경", "제품명", "voice_eval"):
        assert phrase in propose.SYSTEM_RULES


# --------------------------------------------------------------------------- worktree
def test_make_worktree_carries_env_and_stt_cache(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "x.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")          # 커밋 안 됨
    eval_out = tmp_path / "eval"
    (eval_out / "case_a").mkdir(parents=True)
    (eval_out / "case_a" / "fixture.json").write_text("{}", encoding="utf-8")
    (eval_out / "case_a" / "audio.wav").write_bytes(b"x" * 100)

    wt = tmp_path / "wt"
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    propose.make_worktree(repo, wt, base, eval_out)
    # 중단된 실행 뒤에 흔한 상태: 디렉터리는 지워졌는데 등록만 남음 → 재생성이 막히면 안 된다
    shutil.rmtree(wt)
    propose.make_worktree(repo, wt, base, eval_out)
    try:
        assert (wt / "app" / "x.py").exists()
        assert (wt / ".env").read_text(encoding="utf-8") == "SECRET=1\n"   # 비밀은 직접 넣어 준다
        assert (wt / "out" / "voice-eval" / "case_a" / "fixture.json").exists()  # 전사 동결
        assert not (wt / "out" / "voice-eval" / "case_a" / "audio.wav").exists() # 오디오는 불필요
        assert gates.changed_files(wt) == []
    finally:
        propose.remove_worktree(repo, wt)
    assert not wt.exists()


def test_ensure_branch_follows_head_until_something_is_accepted(tmp_path, monkeypatch):
    """수락분이 없으면 브랜치를 HEAD 로 맞춘다 — 낡은 베이스에서 이미 고친 것을 또 고치지 않게."""
    from app.agents.voice_eval.optimize import __main__ as loop
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("1", encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "c1")
    monkeypatch.setattr(loop, "REPO", repo)

    first = loop.git("rev-parse", "HEAD", cwd=repo)
    loop.ensure_branch(first)
    assert loop.git("rev-parse", loop.BRANCH, cwd=repo) == first

    (repo / "a.txt").write_text("2", encoding="utf-8")      # main 만 앞으로 간다
    run("add", "-A")
    run("commit", "-qm", "c2")
    second = loop.git("rev-parse", "HEAD", cwd=repo)
    loop.ensure_branch(second)
    assert loop.git("rev-parse", loop.BRANCH, cwd=repo) == second      # 수락분 없음 → 따라간다

    run("branch", "-f", loop.BRANCH, first)                 # 수락분이 있는 상황을 흉내
    run("checkout", "-q", loop.BRANCH)
    (repo / "b.txt").write_text("x", encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "accepted")
    accepted = loop.git("rev-parse", loop.BRANCH, cwd=repo)
    run("checkout", "-q", "-")
    loop.ensure_branch(second)
    assert loop.git("rev-parse", loop.BRANCH, cwd=repo) == accepted    # 수락분은 지우지 않는다


# --------------------------------------------------------------------------- 기타
def test_retro_slug():
    assert _slug("structural-extraction") == "structural-extraction"
    assert _slug("!!!") == "structural"
