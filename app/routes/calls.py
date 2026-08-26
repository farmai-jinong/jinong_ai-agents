"""/v1/calls — 전화 시작 / 녹음 수신 / 종료 / 조회 / 산출물 / 재생성."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ..auth import require_api_key
from ..db import repo
from ..errors import ApiError
from ..runtime import Runtime
from ..schemas.calls import (
    AudioReceivedRequest,
    CallDetail,
    CallEndRequest,
    CallListResponse,
    CallStartRequest,
    RegenerateRequest,
)
from ..services.artifacts import UNRESOLVED
from ..services.calls import CallService
from ..services.results import audio_ack, call_detail, list_item

router = APIRouter(prefix="/v1/calls", tags=["calls"], dependencies=[Depends(require_api_key)])


def _rt(request: Request) -> Runtime:
    return request.app.state.rt


def _wake(rt: Runtime) -> None:
    if rt.worker is not None:
        rt.worker.wake()


async def _detail_response(rt: Runtime, call_id: str, status_code: int, note: str | None = None,
                           inline: bool = True) -> JSONResponse:
    async with rt.db.session() as s:
        call = await repo.get_call(s, call_id)
        if call is None:
            raise ApiError("CALL_NOT_FOUND", f"call {call_id} not found", 404)
        detail = await call_detail(s, call, inline=inline, note=note)
    return JSONResponse(status_code=status_code, content=json.loads(detail.model_dump_json()))


@router.post("", response_model=CallDetail)
async def start_call(req: CallStartRequest, request: Request) -> JSONResponse:
    rt = _rt(request)
    tr = await CallService(rt).start(req)
    return await _detail_response(rt, tr.call.call_id, tr.http_status, tr.note)


@router.post("/{call_id}/audio")
async def audio_received(call_id: str, req: AudioReceivedRequest, request: Request) -> JSONResponse:
    rt = _rt(request)
    tr = await CallService(rt).audio_received(call_id, req)
    if tr.wake:
        _wake(rt)
    async with rt.db.session() as s:
        call = await repo.get_call(s, call_id)
        assert call is not None and tr.audio is not None
        a = await repo.get_audio(s, tr.audio.id)
        assert a is not None
        ack = await audio_ack(s, call, a, tr.note)
    return JSONResponse(status_code=tr.http_status, content=json.loads(ack.model_dump_json()))


@router.post("/{call_id}/end", response_model=CallDetail)
async def end_call(call_id: str, request: Request, req: CallEndRequest | None = None) -> JSONResponse:
    rt = _rt(request)
    tr = await CallService(rt).end(call_id, req or CallEndRequest())
    if tr.wake:
        _wake(rt)
    return await _detail_response(rt, call_id, tr.http_status, tr.note)


@router.post("/{call_id}/regenerate", response_model=CallDetail)
async def regenerate(call_id: str, request: Request, req: RegenerateRequest | None = None) -> JSONResponse:
    rt = _rt(request)
    tr = await CallService(rt).regenerate(call_id, req or RegenerateRequest())
    _wake(rt)
    return await _detail_response(rt, call_id, tr.http_status, tr.note)


@router.get("", response_model=CallListResponse)
async def list_calls(request: Request, status: str | None = None, state: str | None = None,
                     limit: int = Query(default=50, ge=1, le=200), cursor: str | None = None) -> CallListResponse:
    rt = _rt(request)
    async with rt.db.session() as s:
        try:
            calls = await repo.list_calls(s, status=status, state=state, limit=limit, cursor=cursor)
        except ValueError:
            raise ApiError("INVALID_CURSOR", "malformed cursor", 422) from None
        items = [list_item(c) for c in calls]
    next_cursor = repo.make_list_cursor(calls[-1].created_at, calls[-1].call_id) if len(calls) == limit else None
    return CallListResponse(items=items, next_cursor=next_cursor)


@router.get("/{call_id}", response_model=CallDetail)
async def get_call(call_id: str, request: Request, inline: bool = True) -> JSONResponse:
    return await _detail_response(_rt(request), call_id, 200, inline=inline)


@router.get("/{call_id}/transcript")
async def get_transcript(call_id: str, request: Request) -> Response:
    rt = _rt(request)
    async with rt.db.session() as s:
        call = await repo.get_call(s, call_id)
        if call is None:
            raise ApiError("CALL_NOT_FOUND", f"call {call_id} not found", 404)
        art = await repo.get_artifact(s, call_id, "transcript")
    if art is None:
        raise ApiError("NOT_READY", "transcript not merged yet", 404)
    body = await rt.s3.get_json(art.s3_key)
    return JSONResponse(content=body)


@router.get("/{call_id}/artifacts/report")
async def get_report(call_id: str, request: Request, format: str = "md") -> Response:
    rt = _rt(request)
    kind = "report_json" if format == "json" else "report_md"
    async with rt.db.session() as s:
        art = await repo.get_artifact(s, call_id, kind)
    if art is None:
        raise ApiError("NOT_READY", "report not available", 404)
    return await _artifact_response(rt, art.s3_key, art.content, kind.endswith("json"))


@router.get("/{call_id}/artifacts/summary")
async def get_summary(call_id: str, request: Request, format: str = "md") -> Response:
    """통화 단순요약 — 콜백으로 보낸 것과 같은 본문(백엔드가 콜백을 놓쳤을 때 재조회용)."""
    rt = _rt(request)
    kind = "summary_json" if format == "json" else "summary_md"
    async with rt.db.session() as s:
        art = await repo.get_artifact(s, call_id, kind)
    if art is None:
        raise ApiError("NOT_READY", "summary not available", 404)
    return await _artifact_response(rt, art.s3_key, art.content, kind.endswith("json"))


@router.get("/{call_id}/artifacts/diary/{prdlst_code}")
async def get_diary(call_id: str, prdlst_code: str, request: Request, format: str = "md") -> Response:
    rt = _rt(request)
    kind = "diary_json" if format == "json" else "diary_md"
    code = prdlst_code or UNRESOLVED
    async with rt.db.session() as s:
        art = await repo.get_artifact(s, call_id, kind, code)
    if art is None:
        raise ApiError("NOT_READY", f"diary for {prdlst_code} not available", 404)
    return await _artifact_response(rt, art.s3_key, art.content, kind.endswith("json"))


async def _artifact_response(rt: Runtime, key: str, inline: str | None, is_json: bool) -> Response:
    text = inline
    if text is None:
        data = await rt.s3.get_bytes(rt.s3.bucket, key)
        text = data.decode("utf-8")
    if is_json:
        return JSONResponse(content=json.loads(text))
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")
