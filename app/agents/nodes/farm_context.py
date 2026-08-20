"""farmos 작물 목록(최우선 호출) → FarmContext. 실패 시 hints → none 으로 강등."""

from __future__ import annotations

import logging

from ...clients.farmos import FarmosAuthError
from ..deps import get_deps
from ..schemas import CropRef, FarmContext
from ..state import PipelineState
from ._common import err

log = logging.getLogger(__name__)


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
        fc = _from_hints(ctx)
        fc.status = "disabled"
        return {"farm": fc, "warnings": ["farmos 미사용(토큰 없음) — 힌트/전사만으로 생성"] if not ctx.farm_access_token else []}
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
