"""farmos(kafka-gateway) 영농일지 **읽기 전용** 클라이언트.

계약: ~/Documents/영농일지_생성과정_설명서.md §3.2/§3.3. 응답 봉투 `{timestamp, statusCode, message, data}`.
인증은 통화 시작 payload 로 받은 **농가 JWT** (`Authorization: Bearer`). 이 서비스는 절대 쓰기(PUT/DELETE)
하지 않는다 — 영농일지 저장은 농가가 앱에서 확인 후 수행(동의서 §3).

주의(§3.7 #11): `list_crops()` 를 **가장 먼저** 호출해야 생육단계 기본행이 시드되어 detail 이 500 나지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


class FarmosError(Exception):
    def __init__(self, message: str, status: int | None = None, path: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.path = path


class FarmosAuthError(FarmosError):
    """401/403 — 토큰 만료/무효. 파이프라인은 warning 으로 강등."""


@dataclass
class DbyhsStep:
    index: int
    step_nm: str
    step_code: str
    step_desc: str
    step_desc_code: str


@dataclass
class DbyhsRow:
    dbyhs_code: str
    dbyhs_nm: str
    steps: list[DbyhsStep] = field(default_factory=list)

    def single(self, index: int) -> dict[str, Any]:
        """저장용 단일값 6필드 (같은 인덱스로 맞춤)."""
        i = max(0, min(index, len(self.steps) - 1))
        s = self.steps[i]
        return {
            "dbyhsCode": self.dbyhs_code, "dbyhsNm": self.dbyhs_nm,
            "occrrncStepNm": s.step_nm, "occrrncStepCode": s.step_code,
            "occrrncStepDesc": s.step_desc, "occrrncStepDescCode": s.step_desc_code,
        }


def expand_dbyhs(row: dict[str, Any]) -> DbyhsRow:
    """`"미발생|2%미만|…"` 파이프 결합 행을 단계 리스트로 전개."""
    nms = str(row.get("occrrncStepNm") or "").split("|")
    codes = str(row.get("occrrncStepCode") or "").split("|")
    descs = str(row.get("occrrncStepDesc") or "").split("|")
    dcodes = str(row.get("occrrncStepDescCode") or "").split("|")
    n = max(len(nms), len(codes), len(descs), len(dcodes))

    def at(xs: list[str], i: int) -> str:
        return xs[i] if i < len(xs) else (xs[-1] if xs else "")

    steps = [DbyhsStep(i, at(nms, i), at(codes, i), at(descs, i), at(dcodes, i)) for i in range(n)]
    return DbyhsRow(str(row.get("dbyhsCode") or ""), str(row.get("dbyhsNm") or ""), steps)


@dataclass
class FarmosRefs:
    """작물 1개 × 날짜 1개에 대한 참조 데이터 묶음."""
    prdlst_code: str
    diary_date: str
    detail: dict[str, Any] | None = None           # DiaryVO (diaryId None 이면 없음)
    farmworks: list[dict[str, Any]] = field(default_factory=list)   # UserFarmworkVO
    dbyhs: list[DbyhsRow] = field(default_factory=list)
    prvnbe_types: list[dict[str, Any]] = field(default_factory=list)
    prvnbe: list[dict[str, Any]] = field(default_factory=list)
    pesti_all: list[dict[str, Any]] = field(default_factory=list)   # plsCmmnCode 없이 전체
    status: dict[str, str] = field(default_factory=dict)            # endpoint → ok | error:<msg>

    @property
    def gs_nm(self) -> str | None:
        return (self.detail or {}).get("gsNm") or None

    @property
    def growing_season_start(self) -> str | None:
        return (self.detail or {}).get("growingSeasonStartDe") or None

    @property
    def existing_diary_id(self) -> int | None:
        return (self.detail or {}).get("diaryId")


class FarmosClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0,
                 client: httpx.AsyncClient | None = None, retries: int = 2) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self._client = client
        self._own = client is None

    async def __aenter__(self) -> "FarmosClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = await self._c().get(url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = e
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code in (401, 403):
                raise FarmosAuthError(f"farmos auth failed ({resp.status_code})", resp.status_code, path)
            if resp.status_code >= 500:
                last = FarmosError(f"farmos {resp.status_code}: {resp.text[:200]}", resp.status_code, path)
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise FarmosError(f"farmos {resp.status_code}: {resp.text[:200]}", resp.status_code, path)
            try:
                body = resp.json()
            except ValueError as e:
                raise FarmosError(f"farmos non-json response: {resp.text[:200]}", resp.status_code, path) from e
            if isinstance(body, dict) and "statusCode" in body:
                sc = body.get("statusCode")
                if sc not in (200, "200"):
                    if sc in (401, 403, "401", "403"):
                        raise FarmosAuthError(str(body.get("message")), int(sc), path)
                    raise FarmosError(f"farmos statusCode {sc}: {body.get('message')}", int(sc) if str(sc).isdigit() else None, path)
                return body.get("data")
            return body
        assert last is not None
        raise last if isinstance(last, FarmosError) else FarmosError(f"farmos unreachable: {last}", None, path)

    # --- endpoints (설명서 §3.2) --------------------------------------------
    async def list_crops(self) -> list[dict[str, Any]]:
        return list(await self._get("/m/diary/user/prdlsts/list") or [])

    async def diary_detail(self, date: str, prdlst_code: str) -> dict[str, Any] | None:
        return await self._get(f"/m/diary/{date}/{prdlst_code}/detail")

    async def farmwork_palette(self, date: str, prdlst_code: str) -> list[dict[str, Any]]:
        return list(await self._get(f"/m/diary/{date}/{prdlst_code}/user-farmwork/list",
                                    {"allSearch": "true"}) or [])

    async def dbyhs_list(self, prdlst_code: str) -> list[DbyhsRow]:
        rows = await self._get(f"/m/diary/{prdlst_code}/dhyhs-stdr/list") or []   # 경로 오타 'dhyhs' 가 실제 스펙
        return [expand_dbyhs(r) for r in rows]

    async def prvnbe_list(self, prdlst_code: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        data = await self._get(f"/m/diary/{prdlst_code}/prvnbe-stdr/list") or {}
        return list(data.get("prvnbeTypeList") or []), list(data.get("prvnbeList") or [])

    async def pesti_list(self, prdlst_code: str, prvnbe_code: str | None = None) -> list[dict[str, Any]]:
        params = {"plsCmmnCode": prvnbe_code} if prvnbe_code else None
        return list(await self._get(f"/m/diary/{prdlst_code}/pesti-stdr/list", params) or [])

    async def month_markers(self, yyyy_mm: str, prdlst_code: str) -> dict[str, Any]:
        return dict(await self._get(f"/m/diary/{yyyy_mm}/{prdlst_code}/reg/list") or {})

    async def fetch_refs(self, date: str, prdlst_code: str) -> FarmosRefs:
        """작물별 참조 데이터 일괄 조회 — 엔드포인트별 독립 실패 허용."""
        refs = FarmosRefs(prdlst_code=prdlst_code, diary_date=date)

        async def run(name: str, coro):  # type: ignore[no-untyped-def]
            try:
                out = await coro
                refs.status[name] = "ok"
                return out
            except FarmosAuthError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("farmos %s failed for %s: %s", name, prdlst_code, e)
                refs.status[name] = f"error:{e}"
                return None

        detail, palette, dbyhs, prvnbe, pesti_all = await asyncio.gather(
            run("detail", self.diary_detail(date, prdlst_code)),
            run("farmworks", self.farmwork_palette(date, prdlst_code)),
            run("dbyhs", self.dbyhs_list(prdlst_code)),
            run("prvnbe", self.prvnbe_list(prdlst_code)),
            run("pesti_all", self.pesti_list(prdlst_code)),
        )
        refs.detail = detail if isinstance(detail, dict) else None
        refs.farmworks = list(palette or [])
        refs.dbyhs = list(dbyhs or [])
        if prvnbe:
            refs.prvnbe_types, refs.prvnbe = prvnbe
        refs.pesti_all = list(pesti_all or [])
        return refs

    async def probe(self) -> bool:
        """도달성만 확인(토큰 무관). 헬스용."""
        try:
            r = await self._c().get(f"{self.base_url}/m/diary/user/prdlsts/list",
                                    headers={"Authorization": f"Bearer {self.token}"})
            return r.status_code < 500
        except Exception:  # noqa: BLE001
            return False
