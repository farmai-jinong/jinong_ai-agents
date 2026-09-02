"""PipelineResult → S3 + artifacts 테이블 저장, 그리고 GET 응답용 result 조립."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..db import repo
from ..db.models import Artifact, Call, DailyArtifact, DailyDiary
from ..runtime import Runtime
from ..schemas.calls import DiaryView, ReportView, ResultView, SummaryView
from ..schemas.daily import DailyResultView
from ..schemas.pipeline import CallSummaryResult, PipelineResult

UNRESOLVED = "unresolved"


def _artifact_code(prdlst_code: str | None, used: set[str]) -> str:
    """저장용 작물코드. 미확정(None)은 UNRESOLVED — 한 결과에 미확정 일지가 여럿이면
    unresolved-2, unresolved-3 … 으로 뒤를 붙여 UNIQUE(diary_id/call_id, kind, prdlst_code)
    와 S3 키 충돌을 막는다(중복 실코드 방어 겸용)."""
    base = prdlst_code or UNRESOLVED
    code, n = base, 1
    while code in used:
        n += 1
        code = f"{base}-{n}"
    used.add(code)
    return code


def _view_code(stored: str) -> str | None:
    """저장 코드 → 응답 prdlst_code (unresolved 계열은 None)."""
    return None if stored == UNRESOLVED or stored.startswith(UNRESOLVED + "-") else stored


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _inline(text: str, max_kb: int) -> str | None:
    return text if len(text.encode("utf-8")) <= max_kb * 1024 else None


async def persist_result(rt: Runtime, s: AsyncSession, call: Call, result: PipelineResult,
                         transcript_key: str, summary: CallSummaryResult | None = None) -> list[Artifact]:
    """S3 에 md/json 저장 후 artifacts 행 전체 교체. result.json 스냅샷도 저장.

    통화 단순요약(`summary`)도 여기서 같이 쓴다 — `replace_artifacts` 가 통화 artifact 를 전량
    교체하므로 바깥에서 따로 저장하면 다음 실행에 사라진다.
    """
    keys = rt.s3.keys
    max_kb = rt.settings.result_inline_max_kb
    run = call.generation_run
    items: list[Artifact] = []

    async def add(kind: str, key: str, text: str, *, prdlst_code: str = "", prdlst_nm: str | None = None,
                  diary_date: str | None = None, diary_status: str | None = None,
                  content_type: str = "text/markdown; charset=utf-8") -> None:
        await rt.s3.put_text(key, text, content_type=content_type)
        items.append(Artifact(call_id=call.call_id, generation_run=run, kind=kind, prdlst_code=prdlst_code,
                              prdlst_nm=prdlst_nm, diary_date=diary_date, diary_status=diary_status, s3_key=key,
                              content=_inline(text, max_kb), sha256=_sha(text), bytes=len(text.encode("utf-8"))))

    result_snapshot: dict[str, Any] = {
        "call_id": call.call_id, "generation_run": run, "model": result.model,
        "prompt_version": result.prompt_version, "farmos_status": result.farmos_status,
        "speaker_map": result.speaker_map, "warnings": result.warnings, "usage": result.usage,
        "transcript_key": transcript_key, "diaries": [], "report": None, "summary": None,
    }
    used_codes: set[str] = set()
    for d in result.diaries:
        code = _artifact_code(d.prdlst_code, used_codes)
        kmd, kjs = keys.diary_md(call.call_id, code), keys.diary_json(call.call_id, code)
        kint = keys.diary_md_internal(call.call_id, code)
        payload = {"prdlst_code": d.prdlst_code, "prdlst_nm": d.prdlst_nm, "diary_date": d.diary_date,
                   "status": d.status, **d.structured}
        meta = dict(prdlst_code=code, prdlst_nm=d.prdlst_nm, diary_date=d.diary_date, diary_status=d.status)
        await add("diary_md", kmd, d.markdown_public, **meta)            # 전달용(근거·코드·내부 메타 제거)
        await add("diary_md_internal", kint, d.markdown, **meta)         # 내부 저장용(근거 포함 정본)
        await add("diary_json", kjs, json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                  content_type="application/json; charset=utf-8", **meta)
        result_snapshot["diaries"].append({**payload, "markdown": d.markdown_public, "markdown_internal": d.markdown,
                                           "s3_key_md": kmd, "s3_key_md_internal": kint, "s3_key_json": kjs})
    if result.report is not None:
        kmd, kjs = keys.report_md(call.call_id), keys.report_json(call.call_id)
        kint = keys.report_md_internal(call.call_id)
        await add("report_md", kmd, result.report.markdown_public)
        await add("report_md_internal", kint, result.report.markdown)
        await add("report_json", kjs, json.dumps(result.report.structured, ensure_ascii=False, indent=2, default=str),
                  content_type="application/json; charset=utf-8")
        result_snapshot["report"] = {**result.report.structured, "markdown": result.report.markdown_public,
                                     "markdown_internal": result.report.markdown,
                                     "s3_key_md": kmd, "s3_key_md_internal": kint, "s3_key_json": kjs}
    if summary is not None and summary.markdown.strip():
        kmd, kjs = keys.summary_md(call.call_id), keys.summary_json(call.call_id)
        payload = summary.model_dump(exclude={"markdown"})
        await add("summary_md", kmd, summary.markdown)
        await add("summary_json", kjs, json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                  content_type="application/json; charset=utf-8")
        result_snapshot["summary"] = {**payload, "markdown": summary.markdown,
                                      "s3_key_md": kmd, "s3_key_json": kjs}
    kres = keys.result_json(call.call_id)
    await add("result_json", kres, json.dumps(result_snapshot, ensure_ascii=False, indent=2, default=str),
              content_type="application/json; charset=utf-8")
    items.append(Artifact(call_id=call.call_id, generation_run=run, kind="transcript", prdlst_code="",
                          s3_key=transcript_key))
    await repo.replace_artifacts(s, call.call_id, items)
    return items


async def persist_daily_result(rt: Runtime, s: AsyncSession, dd: DailyDiary, result: PipelineResult,
                               transcript_key: str) -> list[DailyArtifact]:
    """persist_result 의 daily 버전 — diaries 만 저장 (보고서 없음), daily_* 키·daily_artifacts 사용."""
    keys = rt.s3.keys
    max_kb = rt.settings.result_inline_max_kb
    run = dd.generation_run
    items: list[DailyArtifact] = []

    async def add(kind: str, key: str, text: str, *, prdlst_code: str = "", prdlst_nm: str | None = None,
                  diary_date: str | None = None, diary_status: str | None = None,
                  content_type: str = "text/markdown; charset=utf-8") -> None:
        await rt.s3.put_text(key, text, content_type=content_type)
        items.append(DailyArtifact(diary_id=dd.diary_id, generation_run=run, kind=kind, prdlst_code=prdlst_code,
                                   prdlst_nm=prdlst_nm, diary_date=diary_date, diary_status=diary_status, s3_key=key,
                                   content=_inline(text, max_kb), sha256=_sha(text), bytes=len(text.encode("utf-8"))))

    result_snapshot: dict[str, Any] = {
        "diary_id": dd.diary_id, "diary_date": dd.diary_date, "call_ids": list(dd.call_ids_json or []),
        "generation_run": run, "model": result.model,
        "prompt_version": result.prompt_version, "farmos_status": result.farmos_status,
        "speaker_map": result.speaker_map, "warnings": result.warnings, "usage": result.usage,
        "transcript_key": transcript_key, "diaries": [],
    }
    used_codes: set[str] = set()
    for d in result.diaries:
        code = _artifact_code(d.prdlst_code, used_codes)
        kmd, kjs = keys.daily_diary_md(dd.diary_id, code), keys.daily_diary_json(dd.diary_id, code)
        kint = keys.daily_diary_md_internal(dd.diary_id, code)
        payload = {"prdlst_code": d.prdlst_code, "prdlst_nm": d.prdlst_nm, "diary_date": d.diary_date,
                   "status": d.status, **d.structured}
        meta = dict(prdlst_code=code, prdlst_nm=d.prdlst_nm, diary_date=d.diary_date, diary_status=d.status)
        await add("diary_md", kmd, d.markdown_public, **meta)
        await add("diary_md_internal", kint, d.markdown, **meta)
        await add("diary_json", kjs, json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                  content_type="application/json; charset=utf-8", **meta)
        result_snapshot["diaries"].append({**payload, "markdown": d.markdown_public, "markdown_internal": d.markdown,
                                           "s3_key_md": kmd, "s3_key_md_internal": kint, "s3_key_json": kjs})
    kres = keys.daily_result_json(dd.diary_id)
    await add("result_json", kres, json.dumps(result_snapshot, ensure_ascii=False, indent=2, default=str),
              content_type="application/json; charset=utf-8")
    items.append(DailyArtifact(diary_id=dd.diary_id, generation_run=run, kind="transcript", prdlst_code="",
                               s3_key=transcript_key))
    await repo.replace_daily_artifacts(s, dd.diary_id, items)
    return items


def artifact_keys(artifacts: list[Artifact] | list[DailyArtifact]) -> dict[str, Any]:
    """콜백에 싣는 산출물 S3 키(본문 없음) — 저장된 행에서 그대로 읽는다(`unresolved-N` 재계산 없음).

    `s3_key_md` = 전달용(근거 제거), `s3_key_md_internal` = 근거 포함 정본(`artifacts/internal/`). 버킷명은 싣지 않는다.
    """
    by_kind = {(a.kind, a.prdlst_code): a for a in artifacts}
    diaries: list[dict[str, Any]] = []
    for a in artifacts:
        if a.kind != "diary_md":
            continue
        ai = by_kind.get(("diary_md_internal", a.prdlst_code))
        diaries.append({"prdlst_code": _view_code(a.prdlst_code), "prdlst_nm": a.prdlst_nm, "status": a.diary_status,
                        "s3_key_md": a.s3_key, "s3_key_md_internal": ai.s3_key if ai else None})
    out: dict[str, Any] = {"diaries": diaries}
    r = by_kind.get(("report_md", ""))
    if r is not None:
        ri = by_kind.get(("report_md_internal", ""))
        out["report"] = {"s3_key_md": r.s3_key, "s3_key_md_internal": ri.s3_key if ri else None}
    return out


def build_result_view(call: Call, artifacts: list[Artifact], *, inline: bool = True) -> ResultView | None:
    if call.status != "COMPLETED":
        return None
    by_kind: dict[tuple[str, str], Artifact] = {(a.kind, a.prdlst_code): a for a in artifacts}
    diaries: list[DiaryView] = []
    for a in artifacts:
        if a.kind != "diary_md":
            continue
        js = by_kind.get(("diary_json", a.prdlst_code))
        ai = by_kind.get(("diary_md_internal", a.prdlst_code))
        structured = None
        if inline and js and js.content:
            try:
                structured = json.loads(js.content)
            except ValueError:
                structured = None
        diaries.append(DiaryView(
            prdlst_code=_view_code(a.prdlst_code), prdlst_nm=a.prdlst_nm,
            diary_date=a.diary_date, status=a.diary_status,
            markdown=a.content if inline else None, structured=structured,
            s3_key_md=a.s3_key, s3_key_json=js.s3_key if js else a.s3_key.replace(".md", ".json"),
            s3_key_md_internal=ai.s3_key if ai else None,
        ))
    report = None
    rmd = by_kind.get(("report_md", ""))
    if rmd:
        rjs = by_kind.get(("report_json", ""))
        ri = by_kind.get(("report_md_internal", ""))
        structured = None
        if inline and rjs and rjs.content:
            try:
                structured = json.loads(rjs.content)
            except ValueError:
                structured = None
        report = ReportView(markdown=rmd.content if inline else None, structured=structured,
                            s3_key_md=rmd.s3_key, s3_key_json=rjs.s3_key if rjs else rmd.s3_key.replace(".md", ".json"),
                            s3_key_md_internal=ri.s3_key if ri else None)
    summary = None
    smd = by_kind.get(("summary_md", ""))
    if smd:
        sjs = by_kind.get(("summary_json", ""))
        structured = None
        if inline and sjs and sjs.content:
            try:
                structured = json.loads(sjs.content)
            except ValueError:
                structured = None
        summary = SummaryView(markdown=smd.content if inline else None, structured=structured,
                              s3_key_md=smd.s3_key,
                              s3_key_json=sjs.s3_key if sjs else smd.s3_key.replace(".md", ".json"))
    tr = by_kind.get(("transcript", ""))
    res = by_kind.get(("result_json", ""))
    return ResultView(transcript_key=tr.s3_key if tr else None, speaker_map=call.speaker_map_json or {},
                      diaries=diaries, report=report, summary=summary,
                      result_key=res.s3_key if res else None)


def build_daily_result_view(dd: DailyDiary, artifacts: list[DailyArtifact], *, inline: bool = True) -> DailyResultView | None:
    if dd.status != "COMPLETED":
        return None
    by_kind: dict[tuple[str, str], DailyArtifact] = {(a.kind, a.prdlst_code): a for a in artifacts}
    diaries: list[DiaryView] = []
    for a in artifacts:
        if a.kind != "diary_md":
            continue
        js = by_kind.get(("diary_json", a.prdlst_code))
        ai = by_kind.get(("diary_md_internal", a.prdlst_code))
        structured = None
        if inline and js and js.content:
            try:
                structured = json.loads(js.content)
            except ValueError:
                structured = None
        diaries.append(DiaryView(
            prdlst_code=_view_code(a.prdlst_code), prdlst_nm=a.prdlst_nm,
            diary_date=a.diary_date, status=a.diary_status,
            markdown=a.content if inline else None, structured=structured,
            s3_key_md=a.s3_key, s3_key_json=js.s3_key if js else a.s3_key.replace(".md", ".json"),
            s3_key_md_internal=ai.s3_key if ai else None,
        ))
    tr = by_kind.get(("transcript", ""))
    res = by_kind.get(("result_json", ""))
    return DailyResultView(transcript_key=tr.s3_key if tr else None, speaker_map=dd.speaker_map_json or {},
                           diaries=diaries, result_key=res.s3_key if res else None)
