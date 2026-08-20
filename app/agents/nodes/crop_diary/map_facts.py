"""사실 → 표준 코드 매핑 (결정적). LLM 은 ambiguous 항목에 대해서만 후보 중 선택(disambiguate)."""

from __future__ import annotations

from typing import Any

from ....clients.farmos import DbyhsRow, FarmosRefs
from ...mapping.matcher import MatchResult, match
from ...mapping.severity import severity_to_step
from ...schemas import CropFacts, MappedItem, MappingReport
from ...state import CropDiaryState

_KIND_TO_TYPE = {"병": ("004", "006", "002"), "해충": ("003", "005", "001")}


def _cands(r: MatchResult) -> list[dict[str, Any]]:
    return [c.as_dict() for c in r.candidates[:5]]


def _fw_id(x: dict[str, Any]) -> str:
    return str(x.get("userFarmworkId"))


def map_farmworks(cf: CropFacts, refs: FarmosRefs | None) -> list[MappedItem]:
    out: list[MappedItem] = []
    palette = [x for x in (refs.farmworks if refs else []) if x.get("use", True)]
    for i, f in enumerate(cf.farmworks):
        # 일지 체크 대상: today/unknown (+ past 는 날짜 힌트 없이 통화일로 볼 수 없으므로 제외)
        if f.when not in ("today", "unknown"):
            continue
        item = MappedItem(item_id=f"fw{i}", family="farmwork", source=f.name, status="no_refs",
                          evidence=f.evidence, when=f.when)
        if not palette:
            out.append(item)
            continue
        r = match(f.name, palette, key=lambda x: str(x.get("userFarmworkNm") or ""), code=_fw_id, family="farmwork")
        item.status = r.status  # type: ignore[assignment]
        item.candidates = _cands(r)
        if r.best:
            item.code, item.name, item.score, item.method = r.best.code, r.best.name, r.best.score, r.best.method
            item.payload = dict(r.best.item)
        out.append(item)
    return out


def map_pests(cf: CropFacts, refs: FarmosRefs | None) -> list[MappedItem]:
    out: list[MappedItem] = []
    rows: list[DbyhsRow] = list(refs.dbyhs) if refs else []
    for i, p in enumerate(cf.pests):
        if p.status not in ("발생", "의심"):
            continue
        step, warns = severity_to_step(p.severity, p.severity_raw, p.status)
        item = MappedItem(item_id=f"pest{i}", family="pest", source=p.name, status="no_refs", evidence=p.evidence,
                          warnings=warns, payload={"step_index": step, "kind": p.kind, "status": p.status})
        if not rows:
            out.append(item)
            continue
        r = match(p.name, rows, key=lambda x: x.dbyhs_nm, code=lambda x: x.dbyhs_code, family="pest")
        item.status = r.status  # type: ignore[assignment]
        item.candidates = _cands(r)
        if r.best:
            row: DbyhsRow = r.best.item
            item.code, item.name, item.score, item.method = row.dbyhs_code, row.dbyhs_nm, r.best.score, r.best.method
            item.payload.update(row.single(step))
        out.append(item)
    return out


def _target_for(p, cf: CropFacts) -> str | None:  # type: ignore[no-untyped-def]
    if p.target:
        return p.target
    # 같은 turn 의 병해충
    ev = set(p.evidence)
    for pest in cf.pests:
        if ev & set(pest.evidence):
            return pest.name
    return None


def map_products(cf: CropFacts, refs: FarmosRefs | None) -> list[MappedItem]:
    out: list[MappedItem] = []
    prvnbe = list(refs.prvnbe) if refs else []
    pesti_all = list(refs.pesti_all) if refs else []
    for i, p in enumerate(cf.products):
        item = MappedItem(item_id=f"prod{i}", family="product", source=p.name, status="no_refs", evidence=p.evidence,
                          when=p.when, category=p.category, needs_verification=True,
                          payload={"target_raw": _target_for(p, cf), "dose": p.dose})
        if p.category != "농약" or p.when not in ("applied", "unknown"):
            # 방제이력 대상 아님 (투입 제품 섹션에만 표시) — 매핑 목록에서 제외
            continue
        if not prvnbe and not pesti_all:
            item.warnings.append("이 작물은 방제대상·약제 표준 목록이 없음")
            out.append(item)
            continue
        # 1) 방제대상
        tgt = item.payload["target_raw"]
        prv_hit = None
        if tgt and prvnbe:
            r = match(tgt, prvnbe, key=lambda x: str(x.get("prvnbeNm") or ""), code=lambda x: str(x.get("prvnbeCode") or ""), family="pest")
            if r.status == "matched" and r.best:
                prv_hit = r.best.item
            elif r.status == "ambiguous":
                item.payload["prvnbe_candidates"] = _cands(r)
        if prv_hit:
            item.payload.update({"prvnbeTypeCode": prv_hit.get("prvnbeTypeCode", ""), "prvnbeCode": prv_hit.get("prvnbeCode", ""),
                                 "prvnbeNm": prv_hit.get("prvnbeNm", "")})
        else:
            item.payload.update({"prvnbeTypeCode": "", "prvnbeCode": "", "prvnbeNm": tgt or ""})
            if tgt:
                item.warnings.append("방제대상 표준 코드 미매핑")
            else:
                item.warnings.append("방제대상 불명")
        # 2) 약제 (해당 대상 목록은 fetch 단계에서 별도 조회 없이 전체 목록 사용 — pesti_all)
        cands = pesti_all
        if cands:
            r = match(p.name, cands, key=lambda x: str(x.get("pestiNm") or ""), code=lambda x: str(x.get("pestiCode") or ""), family="product")
            item.status = r.status  # type: ignore[assignment]
            item.candidates = _cands(r)
            if r.best:
                item.code, item.name, item.score, item.method = r.best.code, r.best.name, r.best.score, r.best.method
                item.payload.update({"pestiCode": r.best.item.get("pestiCode", ""), "pestiNm": r.best.item.get("pestiNm", "")})
        else:
            item.status = "no_refs"
        out.append(item)
    return out


async def map_facts(state: CropDiaryState, config) -> dict:  # type: ignore[no-untyped-def]
    cf = state["crop_facts"]
    refs = state.get("refs")
    rep = MappingReport(farmworks=map_farmworks(cf, refs), pests=map_pests(cf, refs), products=map_products(cf, refs))
    return {"mapping": rep}
