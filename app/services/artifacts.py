"""PipelineResult → S3 + artifacts 테이블 저장, 그리고 GET 응답용 result 조립."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..db import repo
from ..db.models import Artifact, Call, DailyArtifact, DailyDiary
from ..runtime import Runtime
from ..schemas.calls import DiaryView, ReportView, ResultView
from ..schemas.daily import DailyResultView
from ..schemas.pipeline import PipelineResult

UNRESOLVED = "unresolved"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _inline(text: str, max_kb: int) -> str | None:
    return text if len(text.encode("utf-8")) <= max_kb * 1024 else None


async def persist_result(rt: Runtime, s: AsyncSession, call: Call, result: PipelineResult,
                         transcript_key: str) -> list[Artifact]:
    """S3 에 md/json 저장 후 artifacts 행 전체 교체. result.json 스냅샷도 저장."""
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
        "transcript_key": transcript_key, "diaries": [], "report": None,
    }
    for d in result.diaries:
        code = d.prdlst_code or UNRESOLVED
        kmd, kjs = keys.diary_md(call.call_id, code), keys.diary_json(call.call_id, code)
        payload = {"prdlst_code": d.prdlst_code, "prdlst_nm": d.prdlst_nm, "diary_date": d.diary_date,
                   "status": d.status, **d.structured}
        await add("diary_md", kmd, d.markdown, prdlst_code=code, prdlst_nm=d.prdlst_nm,
                  diary_date=d.diary_date, diary_status=d.status)
        await add("diary_json", kjs, json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                  prdlst_code=code, prdlst_nm=d.prdlst_nm, diary_date=d.diary_date, diary_status=d.status,
                  content_type="application/json; charset=utf-8")
        result_snapshot["diaries"].append({**payload, "markdown": d.markdown, "s3_key_md": kmd, "s3_key_json": kjs})
    if result.report is not None:
        kmd, kjs = keys.report_md(call.call_id), keys.report_json(call.call_id)
        await add("report_md", kmd, result.report.markdown)
        await add("report_json", kjs, json.dumps(result.report.structured, ensure_ascii=False, indent=2, default=str),
                  content_type="application/json; charset=utf-8")
        result_snapshot["report"] = {**result.report.structured, "markdown": result.report.markdown,
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
    for d in result.diaries:
        code = d.prdlst_code or UNRESOLVED
        kmd, kjs = keys.daily_diary_md(dd.diary_id, code), keys.daily_diary_json(dd.diary_id, code)
        payload = {"prdlst_code": d.prdlst_code, "prdlst_nm": d.prdlst_nm, "diary_date": d.diary_date,
                   "status": d.status, **d.structured}
        await add("diary_md", kmd, d.markdown, prdlst_code=code, prdlst_nm=d.prdlst_nm,
                  diary_date=d.diary_date, diary_status=d.status)
        await add("diary_json", kjs, json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                  prdlst_code=code, prdlst_nm=d.prdlst_nm, diary_date=d.diary_date, diary_status=d.status,
                  content_type="application/json; charset=utf-8")
        result_snapshot["diaries"].append({**payload, "markdown": d.markdown, "s3_key_md": kmd, "s3_key_json": kjs})
    kres = keys.daily_result_json(dd.diary_id)
    await add("result_json", kres, json.dumps(result_snapshot, ensure_ascii=False, indent=2, default=str),
              content_type="application/json; charset=utf-8")
    items.append(DailyArtifact(diary_id=dd.diary_id, generation_run=run, kind="transcript", prdlst_code="",
                               s3_key=transcript_key))
    await repo.replace_daily_artifacts(s, dd.diary_id, items)
    return items


def build_result_view(call: Call, artifacts: list[Artifact], *, inline: bool = True) -> ResultView | None:
    if call.status != "COMPLETED":
        return None
    by_kind: dict[tuple[str, str], Artifact] = {(a.kind, a.prdlst_code): a for a in artifacts}
    diaries: list[DiaryView] = []
    for a in artifacts:
        if a.kind != "diary_md":
            continue
        js = by_kind.get(("diary_json", a.prdlst_code))
        structured = None
        if inline and js and js.content:
            try:
                structured = json.loads(js.content)
            except ValueError:
                structured = None
        diaries.append(DiaryView(
            prdlst_code=None if a.prdlst_code == UNRESOLVED else a.prdlst_code, prdlst_nm=a.prdlst_nm,
            diary_date=a.diary_date, status=a.diary_status,
            markdown=a.content if inline else None, structured=structured,
            s3_key_md=a.s3_key, s3_key_json=js.s3_key if js else a.s3_key.replace(".md", ".json"),
        ))
    report = None
    rmd = by_kind.get(("report_md", ""))
    if rmd:
        rjs = by_kind.get(("report_json", ""))
        structured = None
        if inline and rjs and rjs.content:
            try:
                structured = json.loads(rjs.content)
            except ValueError:
                structured = None
        report = ReportView(markdown=rmd.content if inline else None, structured=structured,
                            s3_key_md=rmd.s3_key, s3_key_json=rjs.s3_key if rjs else rmd.s3_key.replace(".md", ".json"))
    tr = by_kind.get(("transcript", ""))
    res = by_kind.get(("result_json", ""))
    return ResultView(transcript_key=tr.s3_key if tr else None, speaker_map=call.speaker_map_json or {},
                      diaries=diaries, report=report, result_key=res.s3_key if res else None)


def build_daily_result_view(dd: DailyDiary, artifacts: list[DailyArtifact], *, inline: bool = True) -> DailyResultView | None:
    if dd.status != "COMPLETED":
        return None
    by_kind: dict[tuple[str, str], DailyArtifact] = {(a.kind, a.prdlst_code): a for a in artifacts}
    diaries: list[DiaryView] = []
    for a in artifacts:
        if a.kind != "diary_md":
            continue
        js = by_kind.get(("diary_json", a.prdlst_code))
        structured = None
        if inline and js and js.content:
            try:
                structured = json.loads(js.content)
            except ValueError:
                structured = None
        diaries.append(DiaryView(
            prdlst_code=None if a.prdlst_code == UNRESOLVED else a.prdlst_code, prdlst_nm=a.prdlst_nm,
            diary_date=a.diary_date, status=a.diary_status,
            markdown=a.content if inline else None, structured=structured,
            s3_key_md=a.s3_key, s3_key_json=js.s3_key if js else a.s3_key.replace(".md", ".json"),
        ))
    tr = by_kind.get(("transcript", ""))
    res = by_kind.get(("result_json", ""))
    return DailyResultView(transcript_key=tr.s3_key if tr else None, speaker_map=dd.speaker_map_json or {},
                           diaries=diaries, result_key=res.s3_key if res else None)
