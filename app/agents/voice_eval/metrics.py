"""결정적(LLM 무관) 지표 — 기대 추출 재현율, 발생단계 정확도, 화자 역할.

재현율은 `app/agents/eval.py` 의 `_recall`/`_evidence_validity` 를 그대로 재사용한다(하네스 두 개가
같은 잣대를 쓰도록). 여기서 더하는 것은 `expect.json` 의 `pests: [[이름, 기대단계], ...]` 둘째 원소 —
`eval.py` 가 현재 버리고 있는 **발생단계**로, `mapping/severity.py` 회귀를 잡는 지표다.
"""

from __future__ import annotations

from typing import Any

from ...schemas.pipeline import PipelineResult
from ..eval import _evidence_validity, _recall
from ..mapping.matcher import normalize
from .cases import VoiceCase


def _diary_for(result: PipelineResult, code: str | None):  # type: ignore[no-untyped-def]
    for d in result.diaries:
        if code is None or d.prdlst_code == code:
            return d
    return result.diaries[0] if result.diaries else None


def severity_exact(case: VoiceCase, result: PipelineResult) -> tuple[int, int]:
    """(일치, 기대) — 기대 단계가 표기된 병해충만 센다."""
    expected = [(p[0], str(p[1])) for p in case.expect.get("pests", []) if isinstance(p, list) and len(p) > 1]
    if not expected:
        return 0, 0
    got: dict[str, str] = {}
    for d in result.diaries:
        for m in (d.structured.get("mapping") or {}).get("pests", []):
            key = normalize(m.get("name") or m.get("source") or "", "pest")
            step = str((m.get("payload") or {}).get("occrrncStepCode") or "")
            if key and step:
                got[key] = step
    hit = 0
    for name, step in expected:
        n = normalize(name, "pest")
        actual = got.get(n) or next((v for k, v in got.items() if n and (n in k or k in n)), None)
        if actual == step:
            hit += 1
    return hit, len(expected)


def speaker_role_ok(case: VoiceCase, result: PipelineResult) -> bool | None:
    """대본 첫 발화 화자의 역할과 전사 첫 화자의 판정 역할이 같은가. 판정 불가면 None."""
    if not result.speaker_map or case.first_role == "unknown":
        return None
    first_key = next(iter(result.speaker_map))
    return result.speaker_map[first_key] == case.first_role


def score(case: VoiceCase, result: PipelineResult) -> dict[str, Any]:
    exp = case.expect
    facts = result.facts or {}
    fw = _recall([f for f in exp.get("farmworks", [])],
                 [f["name"] for f in facts.get("farmworks", [])], "farmwork")
    pest = _recall([p[0] if isinstance(p, list) else p for p in exp.get("pests", [])],
                   [p["name"] for p in facts.get("pests", [])], "pest")
    prod = _recall(exp.get("products", []), [p["name"] for p in facts.get("products", [])], "product")
    sev = severity_exact(case, result)
    diaries = {d.prdlst_code or d.prdlst_nm: d.status for d in result.diaries}
    mapped = sum(1 for d in result.diaries for fam in ("farmworks", "pests", "products")
                 for m in (d.structured.get("mapping") or {}).get(fam, []) if m.get("status") == "matched")
    hit = fw[0] + pest[0] + prod[0]
    total = fw[1] + pest[1] + prod[1]
    return {
        "farmworks_recall": list(fw), "pests_recall": list(pest), "products_recall": list(prod),
        "facts_recall": round(hit / total, 4) if total else 1.0,
        "severity_exact": list(sev),
        "severity_ratio": round(sev[0] / sev[1], 4) if sev[1] else 1.0,
        "evidence_valid": list(_evidence_validity(result)),
        "mapped": mapped,
        "diaries": diaries,
        "diary_status_ok": all(diaries.get(k) == v for k, v in exp.get("diary_status", {}).items()),
        "speaker_role_ok": speaker_role_ok(case, result),
        "speaker_map": result.speaker_map,
        "farmos_status": result.farmos_status,
        "warnings": result.warnings,
        "model": result.model,
        "tokens": result.usage.get("total_tokens"),
        "calls": result.usage.get("calls"),
    }
