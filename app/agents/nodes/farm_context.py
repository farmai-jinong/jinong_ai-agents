"""작물 목록 → FarmContext. 농가 JWT(farmos) → AP 백엔드 research API → hints → none 순으로 강등.

AP 백엔드 경로(백엔드 문서 §3)는 토큰 없이 **작물 코드까지** 확정해 준다 — 다만 방제대상·약제·농작업
팔레트는 주지 않으므로 `fetch_refs` 는 여전히 돌지 않고 일지는 `PARTIAL`(prefill 없음)로 남는다.
"""

from __future__ import annotations

import logging

from ...clients.ap_backend import ApBackendAuthError
from ...clients.farmos import FarmosAuthError
from ..deps import get_deps
from ..schemas import CropRef, FarmContext
from ..state import PipelineState
from ._common import err

log = logging.getLogger(__name__)


def farmer_key(ctx) -> tuple[str, str] | None:  # type: ignore[no-untyped-def]
    """농가 복합 키 (engn_id, user_id). participants 의 farmer 우선, 없으면 hints 의 farmer_* 대체.

    둘 다 있어야 조회가 된다 — user_id 단독 식별은 금지(백엔드 문서 §1).
    """
    for p in ctx.participants or []:
        if p.role == "farmer" and p.engn_id and p.user_id:
            return str(p.engn_id), str(p.user_id)
    h = ctx.hints
    if h.farmer_engn_id and h.farmer_user_id:
        return str(h.farmer_engn_id), str(h.farmer_user_id)
    return None


def _from_hints(ctx) -> FarmContext:  # type: ignore[no-untyped-def]
    h = ctx.hints
    crops: list[CropRef] = []
    if h.farmer_crops:
        for c in h.farmer_crops:
            crops.append(CropRef(prdlstCode=c.get("prdlstCode"), prdlstNm=str(c.get("prdlstNm") or ""),
                                 reprsntPrdlstCnt=c.get("reprsntPrdlstCnt")))
    elif h.prdlst_nm or h.prdlst_code:
        crops.append(CropRef(prdlstCode=h.prdlst_code, prdlstNm=h.prdlst_nm or h.prdlst_code or "", reprsntPrdlstCnt=1))
    crops = [c for c in crops if c.prdlstNm]
    return FarmContext(crops=crops, source="hints" if crops else "none", status="unavailable" if crops else "disabled")


async def load_farm_context(state: PipelineState, config) -> dict:  # type: ignore[no-untyped-def]
    deps = get_deps(config)
    ctx = state["ctx"]
    if not deps.farmos_factory or not ctx.farm_access_token:
        return await _without_token(deps, ctx)
    try:
        client = deps.farmos_factory(ctx.farm_access_token)
        rows = await client.list_crops()
        crops = [CropRef(prdlstCode=r.get("prdlstCode"), prdlstNm=str(r.get("prdlstNm") or ""),
                         reprsntPrdlstCnt=r.get("reprsntPrdlstCnt"), use=r.get("use"))
                 for r in rows if r.get("prdlstNm")]
        if not crops:
            fc = _from_hints(ctx)
            fc.status = "partial"
            return {"farm": fc, "warnings": ["farmos 작물 목록이 비어 있음"]}
        return {"farm": FarmContext(crops=crops, source="farmos", status="ok")}
    except FarmosAuthError as e:
        fc = _from_hints(ctx)
        fc.status = "unavailable"
        fc.error = str(e)
        return {"farm": fc, "errors": [err("load_farm_context", e)],
                "warnings": ["farmos 인증 실패 — 표준 코드 매핑 없이 생성"]}
    except Exception as e:  # noqa: BLE001
        log.warning("farmos list_crops failed: %s", e)
        fc = _from_hints(ctx)
        fc.status = "unavailable"
        fc.error = str(e)
        return {"farm": fc, "errors": [err("load_farm_context", e)],
                "warnings": ["farmos 조회 실패 — 표준 코드 매핑 없이 생성"]}


async def _without_token(deps, ctx) -> dict:  # type: ignore[no-untyped-def]
    """농가 JWT 가 없을 때 — AP 백엔드 research API → hints 순."""
    key = farmer_key(ctx)
    if deps.ap_backend is None or key is None:
        fc = _from_hints(ctx)
        fc.status = "disabled"
        why = "토큰 없음" if deps.ap_backend is None else "토큰·농가 복합 키 없음"
        return {"farm": fc, "warnings": [f"farmos 미사용({why}) — 힌트/전사만으로 생성"]}
    engn_id, user_id = key
    try:
        rows = await deps.ap_backend.farm_context(engn_id, user_id)
    except ApBackendAuthError as e:
        fc = _from_hints(ctx)
        fc.status = "unavailable"
        fc.error = str(e)
        return {"farm": fc, "errors": [err("load_farm_context", e)],
                "warnings": ["AP 백엔드 인증 실패 — 힌트/전사만으로 생성"]}
    except Exception as e:  # noqa: BLE001
        log.warning("ap-backend farm_context failed: %s", e)
        fc = _from_hints(ctx)
        fc.status = "unavailable"
        fc.error = str(e)
        return {"farm": fc, "errors": [err("load_farm_context", e)],
                "warnings": ["AP 백엔드 작물 조회 실패 — 힌트/전사만으로 생성"]}
    crops = [CropRef(prdlstCode=r.get("prdlstCode"), prdlstNm=str(r.get("prdlstNm") or ""),
                     reprsntPrdlstCnt=r.get("reprsntPrdlstCnt"))
             for r in rows if r.get("prdlstNm")]
    if not crops:
        fc = _from_hints(ctx)
        fc.status = "partial"
        return {"farm": fc, "warnings": [f"AP 백엔드 작물 목록이 비어 있음(engn:{engn_id})"]}
    # 토큰이 없으므로 방제대상·약제 표준은 못 읽는다 → prefill 불가는 render_diary 가 경고로 남긴다.
    return {"farm": FarmContext(crops=crops, source="ap_backend", status="partial"),
            "warnings": ["farmos 미사용(토큰 없음) — AP 백엔드 작물 목록으로 코드만 확정(prefill 불가)"]}
