"""작물별 farmos 참조 데이터 (detail / 농작업 팔레트 / 병해충 표준 / 방제대상 / 약제 전체)."""

from __future__ import annotations

import logging

from ....clients.farmos import FarmosAuthError
from ...deps import get_deps
from ...state import CropDiaryState
from .._common import err

log = logging.getLogger(__name__)


async def fetch_refs(state: CropDiaryState, config) -> dict:  # type: ignore[no-untyped-def]
    deps = get_deps(config)
    ctx = state["ctx"]
    target = state["target"]
    farm = state.get("farm")
    if (not deps.farmos_factory or not ctx.farm_access_token or not target.prdlst_code
            or not farm or farm.source != "farmos"):
        return {"refs": None, "refs_status": "disabled"}
    try:
        client = deps.farmos_factory(ctx.farm_access_token)
        refs = await client.fetch_refs(state["diary_date"], target.prdlst_code)
        ok = all(v == "ok" for v in refs.status.values())
        return {"refs": refs, "refs_status": "ok" if ok else "partial",
                "warnings": [] if ok else [f"{target.prdlst_nm}: farmos 일부 조회 실패 ({', '.join(k for k, v in refs.status.items() if v != 'ok')})"]}
    except FarmosAuthError as e:
        return {"refs": None, "refs_status": "unavailable", "errors": [err("fetch_refs", e)],
                "warnings": [f"{target.prdlst_nm}: farmos 인증 실패"]}
    except Exception as e:  # noqa: BLE001
        log.warning("fetch_refs failed: %s", e)
        return {"refs": None, "refs_status": "unavailable", "errors": [err("fetch_refs", e)],
                "warnings": [f"{target.prdlst_nm}: farmos 조회 실패"]}
