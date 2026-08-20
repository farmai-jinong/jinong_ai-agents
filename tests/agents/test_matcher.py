from app.agents.mapping.matcher import decompose_jamo, dedupe_texts, match, normalize

FW = [{"n": "관수", "c": 1}, {"n": "적엽", "c": 2}, {"n": "적심", "c": 3}, {"n": "약제살포", "c": 4}, {"n": "측지제거", "c": 5}]
PESTS = [{"n": "잿빛곰팡이병", "c": "a"}, {"n": "흰가루병", "c": "b"}, {"n": "잎곰팡이병", "c": "c"}, {"n": "점박이응애", "c": "d"}]


def m(q, cands, fam):
    return match(q, cands, key=lambda x: x["n"], code=lambda x: str(x["c"]), family=fam)


def test_normalize_synonyms_and_suffixes():
    assert normalize("물주기", "farmwork") == "관수"
    assert normalize("잎따기 작업", "farmwork") == "적엽"
    assert normalize("곁순 제거", "farmwork") == "측지제거"
    assert normalize("사파이어 액상수화제", "product") == "사파이어"
    assert normalize("잿빛곰팡이", "pest") == "잿빛곰팡이병"
    assert normalize("  총채 ", "pest") == "총채벌레"


def test_jamo_decompose():
    assert decompose_jamo("한") == "ㅎㅏㄴ"
    assert decompose_jamo("가a") == "ㄱㅏa"


def test_exact_and_synonym_match():
    r = m("물주기", FW, "farmwork")
    assert r.status == "matched" and r.best.name == "관수" and r.best.method == "exact"
    r = m("순치기", FW, "farmwork")
    assert r.status == "matched" and r.best.name == "적심"


def test_substring_unique_and_ambiguous():
    r = m("잿빛곰팡이", PESTS, "pest")
    assert r.status == "matched" and r.best.code == "a"
    r = m("곰팡이", PESTS, "pest")
    assert r.status == "ambiguous" and {c.code for c in r.candidates} == {"a", "c"}


def test_fuzzy_near_miss_and_unmatched():
    r = m("잿빛공팡이", PESTS, "pest")           # STT 오인식
    assert r.status == "matched" and r.best.code == "a"
    r = m("수확", FW, "farmwork")
    assert r.status == "unmatched"


def test_dedupe_texts():
    keep = dedupe_texts(["환기 강화 권고", "환기 강화 권고함", "배액 EC 조정"])
    assert keep == [0, 2]
