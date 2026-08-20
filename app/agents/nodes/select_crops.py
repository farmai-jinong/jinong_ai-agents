"""대상 작물 결정 + 사실 라우팅.

규칙: crops_mentioned ↔ farm.crops 이름 매칭(≥85 또는 부분문자열) → 대상; 없으면 대표작물
(reprsntPrdlstCnt==1) → 첫 작물; hints.prdlst_code 우선; farm 불명 + 작물 언급 → code None;
아무것도 없으면 UNRESOLVED_CROP 1건. crop=None 사실은 대상 1개면 그쪽, 여러 개면 대표/첫 작물 + warning.
"""

from __future__ import annotations

from ..mapping.matcher import match
from ..schemas import CallFacts, CropFacts, CropRef, CropTarget, FarmContext
from ..state import PipelineState


def _match_crop(name: str, crops: list[CropRef]) -> CropRef | None:
    if not name:
        return None
    r = match(name, crops, key=lambda c: c.prdlstNm, code=lambda c: c.prdlstCode or c.prdlstNm,
              family="crop", auto=85.0, ambiguous=70.0)
    if r.status == "matched" and r.best:
        return r.best.item
    # 방울토마토 ⊃ 토마토 등 부분 문자열은 matcher substring 에서 처리됨; ambiguous 면 가장 높은 후보
    if r.status == "ambiguous" and r.candidates and r.candidates[0].score >= 90:
        return r.candidates[0].item
    return None


def _default_crop(crops: list[CropRef]) -> CropRef | None:
    for c in crops:
        if c.reprsntPrdlstCnt == 1:
            return c
    return crops[0] if crops else None


def choose_targets(facts: CallFacts, farm: FarmContext, hint_code: str | None, hint_nm: str | None) -> tuple[list[CropTarget], dict[str, str], list[str]]:
    """returns (targets, name→target_key, warnings). target_key = prdlst_code or prdlst_nm."""
    warnings: list[str] = []
    crops = farm.crops
    name_to_key: dict[str, str] = {}
    targets: list[CropTarget] = []

    def add(code: str | None, nm: str, reason: str, resolved: bool = True) -> str:
        key = code or nm
        if not any((t.prdlst_code or t.prdlst_nm) == key for t in targets):
            targets.append(CropTarget(prdlst_code=code, prdlst_nm=nm, reason=reason, resolved=resolved))
        return key

    if hint_code or hint_nm:
        c = next((c for c in crops if c.prdlstCode and c.prdlstCode == hint_code), None) if hint_code else None
        if c is None and hint_nm:
            c = _match_crop(hint_nm, crops)
        if c is not None:
            add(c.prdlstCode, c.prdlstNm, "hint")
        else:
            add(hint_code, hint_nm or hint_code or "", "hint")
    # 언급된 작물
    for m in facts.crops_mentioned:
        c = None
        if m.matched_name:
            c = next((x for x in crops if x.prdlstNm == m.matched_name), None) or _match_crop(m.matched_name, crops)
        if c is None:
            c = _match_crop(m.name_raw, crops)
        if c is not None:
            key = add(c.prdlstCode, c.prdlstNm, "mentioned")
            name_to_key[m.name_raw] = key
            if m.matched_name:
                name_to_key[m.matched_name] = key
            name_to_key[c.prdlstNm] = key
        elif not crops and m.name_raw:
            key = add(None, m.matched_name or m.name_raw, "mentioned-unknown-farm")
            name_to_key[m.name_raw] = key
    # 사실의 crop 필드에 등장한 이름
    for name in {x.crop for x in (facts.farmworks + facts.observations + facts.pests + facts.products) if x.crop}:
        if name in name_to_key:
            continue
        c = _match_crop(name, crops)
        if c is not None:
            name_to_key[name] = add(c.prdlstCode, c.prdlstNm, "fact-crop")
        elif not crops:
            name_to_key[name] = add(None, name, "fact-crop-unknown-farm")
    if not targets:
        d = _default_crop(crops)
        if d is not None:
            add(d.prdlstCode, d.prdlstNm, "default(대표작물)" if d.reprsntPrdlstCnt == 1 else "default(첫 작물)")
            if crops and len(crops) > 1:
                warnings.append(f"통화에서 작물이 특정되지 않아 {d.prdlstNm} 로 가정")
        else:
            add(None, "미확정 작물", "unresolved", resolved=False)
            warnings.append("작물을 확정할 수 없음(농가 작물 목록 없음, 통화 미언급)")
    for c in crops:
        name_to_key.setdefault(c.prdlstNm, c.prdlstCode or c.prdlstNm)
    return targets, name_to_key, warnings


def route_facts(facts: CallFacts, targets: list[CropTarget], name_to_key: dict[str, str]) -> tuple[dict[str, CropFacts], list[str]]:
    warnings: list[str] = []
    keys = [(t.prdlst_code or t.prdlst_nm) for t in targets]
    out = {k: CropFacts() for k in keys}
    primary = keys[0]
    multi = len(keys) > 1

    def dest(crop: str | None) -> str:
        if crop and crop in name_to_key and name_to_key[crop] in out:
            return name_to_key[crop]
        if crop:
            # 느슨한 매칭
            for nm, k in name_to_key.items():
                if k in out and (crop in nm or nm in crop):
                    return k
        if multi:
            warnings.append("작물 미특정 항목이 있어 대표 작물에 배정")
        return primary

    for f in facts.farmworks:
        out[dest(f.crop)].farmworks.append(f)
    for o in facts.observations:
        out[dest(o.crop)].observations.append(o)
    for p in facts.pests:
        out[dest(p.crop)].pests.append(p)
    for p in facts.products:
        out[dest(p.crop)].products.append(p)
    # 후속/조치는 작물 정보가 없어 모든 대상에 공유 (단일이면 그쪽)
    for k in keys:
        out[k].follow_ups = list(facts.follow_ups)
        out[k].actions = list(facts.actions)
    return out, sorted(set(warnings))


async def select_crops(state: PipelineState, config) -> dict:  # type: ignore[no-untyped-def]
    facts: CallFacts = state["facts"]
    farm: FarmContext = state.get("farm") or FarmContext()
    ctx = state["ctx"]
    targets, name_to_key, w1 = choose_targets(facts, farm, ctx.hints.prdlst_code, ctx.hints.prdlst_nm)
    routed, w2 = route_facts(facts, targets, name_to_key)
    return {"crop_targets": targets, "crop_facts": routed, "warnings": w1 + w2}
