"""PutDiaryDTO prefill + markdown 렌더 → DiaryResult."""

from __future__ import annotations

from ...deps import get_deps
from ...prompts.loader import PROMPT_VERSION
from ...render.markdown import render_diary
from ...schemas import DbyhsSingle, DiaryResult, MappingReport, PrvnbeNPesti, PutDiaryDTO, UserFarmworkVO
from ...state import CropDiaryState

# 상단 격려 줄의 고정 문구 — LLM 이 근거 있는 한 줄을 못 냈을 때 / 빈 일지일 때. 형식(항상 2줄 블록)을 고정하기 위한 값.
FALLBACK_PRAISE = "오늘도 수고 많으셨어요 🌱"
EMPTY_PRAISE = "이번 통화에는 기록할 농작업이 없었어요. 다음 통화도 응원할게요 🌱"


def build_prefill(diary_date: str, prdlst_code: str | None, rep: MappingReport, content: str, refs) -> PutDiaryDTO:  # type: ignore[no-untyped-def]
    dto = PutDiaryDTO(diaryDate=diary_date, prdlstCode=prdlst_code, content=content)
    existing = refs.detail if refs is not None and refs.detail and refs.detail.get("diaryId") else None
    if existing:
        dto.diaryId = existing.get("diaryId")
        # 기존 체크 유지 (자식 목록은 전체 교체 — §3.7 #3)
        for fw in existing.get("userFarmworkList") or []:
            if fw.get("checked"):
                dto.userFarmworkList.append(UserFarmworkVO(**{k: fw.get(k) for k in ("userFarmworkId", "userFarmworkNm", "userAdded", "use", "checked") if fw.get(k) is not None}))
        for d in existing.get("dbyhsList") or []:
            try:
                dto.dbyhsList.append(DbyhsSingle(**{k: str(d.get(k) or "") for k in DbyhsSingle.model_fields}))
            except Exception:  # noqa: BLE001
                pass
        for pp in existing.get("prvnbeNPestiList") or []:
            dto.prvnbeNPestiList.append(PrvnbeNPesti(**{k: str(pp.get(k) or "") for k in PrvnbeNPesti.model_fields}))
        if existing.get("content") and content:
            dto.content = f"{existing['content']}\n\n{content}"
        elif existing.get("content"):
            dto.content = existing["content"]
    have_fw = {x.userFarmworkId for x in dto.userFarmworkList}
    for m in rep.farmworks:
        if m.status == "matched" and m.payload:
            fid = m.payload.get("userFarmworkId")
            if fid in have_fw:
                continue
            dto.userFarmworkList.append(UserFarmworkVO(userFarmworkId=fid, userFarmworkNm=str(m.payload.get("userFarmworkNm") or m.name),
                                                       userAdded=bool(m.payload.get("userAdded", False)), use=True, checked=True))
            have_fw.add(fid)
    have_db = {(x.dbyhsCode, x.occrrncStepCode) for x in dto.dbyhsList}
    for m in rep.pests:
        if m.status == "matched" and m.payload.get("dbyhsCode"):
            key = (m.payload["dbyhsCode"], str(m.payload.get("occrrncStepCode")))
            if key in have_db:
                continue
            dto.dbyhsList.append(DbyhsSingle(**{k: str(m.payload.get(k) or "") for k in DbyhsSingle.model_fields}))
            have_db.add(key)
    have_pp = {(x.prvnbeCode, x.pestiCode) for x in dto.prvnbeNPestiList}
    for m in rep.products:
        if m.category == "농약" and m.status == "matched" and m.payload.get("pestiCode"):
            key = (str(m.payload.get("prvnbeCode") or ""), str(m.payload["pestiCode"]))
            if key in have_pp:
                continue
            dto.prvnbeNPestiList.append(PrvnbeNPesti(prvnbeTypeCode=str(m.payload.get("prvnbeTypeCode") or ""),
                                                     prvnbeCode=str(m.payload.get("prvnbeCode") or ""),
                                                     prvnbeNm=str(m.payload.get("prvnbeNm") or ""),
                                                     pestiCode=str(m.payload["pestiCode"]), pestiNm=str(m.payload.get("pestiNm") or "")))
            have_pp.add(key)
    return dto


def collect_evidence(rep: MappingReport, content_ev: list[int], cf) -> list[int]:  # type: ignore[no-untyped-def]
    ev: list[int] = list(content_ev)
    for fam in (rep.farmworks, rep.pests, rep.products):
        for m in fam:
            ev.extend(m.evidence)
    for group in (cf.observations, cf.follow_ups, cf.products):
        for x in group:
            ev.extend(x.evidence)
    return sorted(set(e for e in ev if isinstance(e, int)))


async def render_diary_node(state: CropDiaryState, config) -> dict:  # type: ignore[no-untyped-def]
    deps = get_deps(config)
    target = state["target"]
    cf = state["crop_facts"]
    rep: MappingReport = state.get("mapping") or MappingReport()
    refs = state.get("refs")
    content = state.get("content")
    warnings: list[str] = list(cf.warnings)
    farm = state.get("farm")
    if refs is not None:
        gs, gss, ex = refs.gs_nm, refs.growing_season_start, refs.existing_diary_id
    else:
        gs, gss, ex = None, None, None
    if not target.resolved:
        status = "UNRESOLVED_CROP"
    elif cf.is_empty():
        status = "EMPTY"
    else:
        status = "OK"
        if state.get("refs_status") in ("unavailable", "partial", "disabled") and (farm is None or farm.source != "farmos"):
            status = "PARTIAL"
            warnings.append("farmos 표준 코드 매핑 없이 생성됨(prefill 불가)")
        elif state.get("refs_status") == "partial":
            status = "PARTIAL"
    for fam in (rep.farmworks, rep.pests, rep.products):
        for m in fam:
            for w in m.warnings:
                warnings.append(f"{m.source}: {w}")
    content_text = content.content if content else ""
    prefill = None
    prefill_ready = False
    if status in ("OK", "PARTIAL") and target.prdlst_code and refs is not None:
        prefill = build_prefill(state["diary_date"], target.prdlst_code, rep, content_text, refs)
        prefill_ready = state.get("refs_status") == "ok" and not [m for m in rep.pests + rep.products if m.status == "ambiguous"]
    existing_fw: list[str] = []
    if refs is not None and refs.detail and refs.detail.get("diaryId"):
        existing_fw = [str(f.get("userFarmworkNm")) for f in (refs.detail.get("userFarmworkList") or []) if f.get("checked")]
    praise = EMPTY_PRAISE if status in ("EMPTY", "UNRESOLVED_CROP") else ((content.praise if content else None) or FALLBACK_PRAISE)
    d = DiaryResult(prdlst_code=target.prdlst_code, prdlst_nm=target.prdlst_nm, diary_date=state["diary_date"],
                    status=status, gs_nm=gs, growing_season_start=gss, existing_diary_id=ex, existing_farmworks=existing_fw, prefill=prefill,
                    prefill_ready=prefill_ready, mapping=rep, content=content_text, warnings=warnings,
                    summary_line=state.get("call_summary") or "", praise=praise,
                    evidence=collect_evidence(rep, content.evidence if content else [], cf))
    d.markdown = render_diary(d, state["ctx"], state["transcript"], cf, model=getattr(deps.llm, "model_name", None) or deps.settings.llm_model,
                              prompt_version=PROMPT_VERSION, now=deps.clock())
    return {"diary": d, "diaries": [d]}
