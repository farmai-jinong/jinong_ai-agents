from app.agents.mapping.severity import pct_to_step, severity_to_step


def test_percent_priority():
    assert severity_to_step("심함", "한 5% 정도")[0] == 3
    assert severity_to_step("경미", "30% 넘게")[0] == 5
    assert pct_to_step(0) == 0 and pct_to_step(1.5) == 1 and pct_to_step(12) == 4


def test_word_map_and_warnings():
    step, w = severity_to_step("경미", "조금 보이는 것 같아요")
    assert step == 1 and not w
    step, w = severity_to_step("보통", None)
    assert step == 2
    step, w = severity_to_step("심함", "너무 많아졌어요")
    assert step in (4, 5)
    step, w = severity_to_step("불명", None, "의심")
    assert step == 1 and any("확인" in x for x in w) and any("의심" in x for x in w)


def test_raw_hint_conflict_warns():
    step, w = severity_to_step("경미", "온 하우스에 다 퍼졌어요")
    assert step == 1 and w
