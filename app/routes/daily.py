"""/v1/daily-diaries — 날짜별(멀티콜) 영농일지: 트리거 / 조회 / 산출물 / 재생성."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from ..auth import require_api_key
from ..db import repo
from ..errors import ApiError
from ..runtime import Runtime
from ..schemas.daily import (
    DailyDiaryCreateRequest,
    DailyDiaryDetail,
    DailyDiaryListResponse,
    DailyRegenerateRequest,
)
from ..services.artifacts import UNRESOLVED
from ..services.daily import DailyDiaryService
from ..services.results import daily_detail, daily_list_item
from .calls import _artifact_response

router = APIRouter(prefix="/v1/daily-diaries", tags=["daily-diaries"], dependencies=[Depends(require_api_key)])


def _rt(request: Request) -> Runtime:
    return request.app.state.rt


def _wake(rt: Runtime) -> None:
    if rt.worker is not None:
        rt.worker.wake()


async def _detail_response(rt: Runtime, diary_id: str, status_code: int, note: str | None = None,
                           inline: bool = True) -> JSONResponse:
    async with rt.db.session() as s:
        dd = await repo.get_daily(s, diary_id)
        if dd is None:
            raise ApiError("DAILY_NOT_FOUND", f"daily diary {diary_id} not found", 404)
        detail = await daily_detail(s, dd, inline=inline, note=note)
    return JSONResponse(status_code=status_code, content=json.loads(detail.model_dump_json()))


@router.post("", response_model=DailyDiaryDetail)
async def create_daily(req: DailyDiaryCreateRequest, request: Request) -> JSONResponse:
    rt = _rt(request)
    tr = await DailyDiaryService(rt).create(req)
    if tr.wake:
        _wake(rt)
    return await _detail_response(rt, tr.daily.diary_id, tr.http_status, tr.note)


@router.post("/{diary_id}/regenerate", response_model=DailyDiaryDetail)
async def regenerate_daily(diary_id: str, request: Request, req: DailyRegenerateRequest | None = None) -> JSONResponse:
    rt = _rt(request)
    tr = await DailyDiaryService(rt).regenerate(diary_id, req or DailyRegenerateRequest())
    _wake(rt)
    return await _detail_response(rt, diary_id, tr.http_status, tr.note)


@router.get("", response_model=DailyDiaryListResponse)
async def list_daily(request: Request, diary_date: str | None = None, status: str | None = None,
                     limit: int = Query(default=50, ge=1, le=200), cursor: str | None = None) -> DailyDiaryListResponse:
    rt = _rt(request)
    async with rt.db.session() as s:
        rows = await repo.list_daily(s, diary_date=diary_date, status=status, limit=limit, cursor=cursor)
        items = [daily_list_item(d) for d in rows]
    return DailyDiaryListResponse(items=items, next_cursor=items[-1].diary_id if len(items) == limit else None)


@router.get("/{diary_id}", response_model=DailyDiaryDetail)
async def get_daily(diary_id: str, request: Request, inline: bool = True) -> JSONResponse:
    return await _detail_response(_rt(request), diary_id, 200, inline=inline)


@router.get("/{diary_id}/transcript")
async def get_daily_transcript(diary_id: str, request: Request) -> Response:
    rt = _rt(request)
    async with rt.db.session() as s:
        dd = await repo.get_daily(s, diary_id)
        if dd is None:
            raise ApiError("DAILY_NOT_FOUND", f"daily diary {diary_id} not found", 404)
        art = await repo.get_daily_artifact(s, diary_id, "transcript")
    if art is None:
        raise ApiError("NOT_READY", "transcript not merged yet", 404)
    body = await rt.s3.get_json(art.s3_key)
    return JSONResponse(content=body)


@router.get("/{diary_id}/artifacts/diary/{prdlst_code}")
async def get_daily_diary_artifact(diary_id: str, prdlst_code: str, request: Request, format: str = "md") -> Response:
    rt = _rt(request)
    kind = "diary_json" if format == "json" else "diary_md"
    code = prdlst_code or UNRESOLVED
    async with rt.db.session() as s:
        art = await repo.get_daily_artifact(s, diary_id, kind, code)
    if art is None:
        raise ApiError("NOT_READY", f"diary for {prdlst_code} not available", 404)
    return await _artifact_response(rt, art.s3_key, art.content, kind.endswith("json"))
