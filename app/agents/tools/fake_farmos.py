"""픽스처 디렉터리 기반 FakeFarmosClient (테스트/드라이런).

레이아웃: <dir>/crops.json, <dir>/<prdlstCode>/{detail,palette,dbyhs,prvnbe,pesti_all}.json
`raise_on` 에 엔드포인트명을 넣으면 해당 호출이 예외를 던진다(강등 경로 테스트).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...clients.farmos import FarmosAuthError, FarmosError, FarmosRefs, expand_dbyhs


class FakeFarmosClient:
    def __init__(self, fixture_dir: str | Path, *, raise_on: set[str] | None = None, auth_fail: bool = False) -> None:
        self.dir = Path(fixture_dir)
        self.raise_on = raise_on or set()
        self.auth_fail = auth_fail
        self.calls: list[str] = []

    def _load(self, *parts: str, default: Any = None) -> Any:
        p = self.dir.joinpath(*parts)
        if not p.exists():
            return default
        return json.loads(p.read_text(encoding="utf-8"))

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.auth_fail:
            raise FarmosAuthError("fake auth fail", 401, name)
        if name in self.raise_on:
            raise FarmosError(f"fake failure: {name}", 500, name)

    async def list_crops(self) -> list[dict[str, Any]]:
        self._check("list_crops")
        return list(self._load("crops.json", default=[]))

    async def diary_detail(self, date: str, prdlst_code: str) -> dict[str, Any] | None:
        self._check("detail")
        return self._load(prdlst_code, "detail.json", default={"diaryId": None})

    async def farmwork_palette(self, date: str, prdlst_code: str) -> list[dict[str, Any]]:
        self._check("farmworks")
        return list(self._load(prdlst_code, "palette.json", default=[]))

    async def dbyhs_list(self, prdlst_code: str):  # type: ignore[no-untyped-def]
        self._check("dbyhs")
        return [expand_dbyhs(r) for r in self._load(prdlst_code, "dbyhs.json", default=[])]

    async def prvnbe_list(self, prdlst_code: str):  # type: ignore[no-untyped-def]
        self._check("prvnbe")
        d = self._load(prdlst_code, "prvnbe.json", default={}) or {}
        return list(d.get("prvnbeTypeList") or []), list(d.get("prvnbeList") or [])

    async def pesti_list(self, prdlst_code: str, prvnbe_code: str | None = None) -> list[dict[str, Any]]:
        self._check("pesti_all")
        rows = list(self._load(prdlst_code, "pesti_all.json", default=[]))
        return rows

    async def month_markers(self, yyyy_mm: str, prdlst_code: str) -> dict[str, Any]:
        self._check("month")
        return {}

    async def fetch_refs(self, date: str, prdlst_code: str) -> FarmosRefs:
        refs = FarmosRefs(prdlst_code=prdlst_code, diary_date=date)
        for name, fn in (("detail", lambda: self.diary_detail(date, prdlst_code)),
                         ("farmworks", lambda: self.farmwork_palette(date, prdlst_code)),
                         ("dbyhs", lambda: self.dbyhs_list(prdlst_code)),
                         ("prvnbe", lambda: self.prvnbe_list(prdlst_code)),
                         ("pesti_all", lambda: self.pesti_list(prdlst_code))):
            try:
                out = await fn()
                refs.status[name] = "ok"
            except FarmosAuthError:
                raise
            except Exception as e:  # noqa: BLE001
                refs.status[name] = f"error:{e}"
                continue
            if name == "detail":
                refs.detail = out if isinstance(out, dict) else None
            elif name == "farmworks":
                refs.farmworks = list(out)
            elif name == "dbyhs":
                refs.dbyhs = list(out)
            elif name == "prvnbe":
                refs.prvnbe_types, refs.prvnbe = out
            elif name == "pesti_all":
                refs.pesti_all = list(out)
        return refs

    async def probe(self) -> bool:
        return True
