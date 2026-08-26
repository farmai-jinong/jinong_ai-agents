"""가드레일 검증 + PipelineResult 조립 (예외 없음)."""

from __future__ import annotations

import re

from ...schemas.pipeline import DiaryArtifact, PipelineResult, ReportArtifact
from ..deps import get_deps
from ..prompts.loader import PROMPT_VERSION
from ..schemas import DiaryResult
from ..state import PipelineState

_HANGUL = re.compile(r"[가-힣]")


def hangul_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 1.0
    return sum(1 for c in letters if _HANGUL.match(c)) / len(letters)


def diary_to_artifact(d: DiaryResult) -> DiaryArtifact:
    return DiaryArtifact(
        prdlst_code=d.prdlst_code, prdlst_nm=d.prdlst_nm, diary_date=d.diary_date, status=d.status,
        markdown=d.markdown,
        structured={
            "schema_version": "1",
            "prefill": d.prefill.model_dump() if d.prefill else None,
            "prefill_ready": d.prefill_ready,
            "mapping": d.mapping.model_dump(),
            "gsNm": d.gs_nm, "growingSeasonStartDe": d.growing_season_start,
            "existing_diary_id": d.existing_diary_id, "content": d.content,
            "warnings": d.warnings, "evidence": d.evidence,
            "verify": d.verify.model_dump() if d.verify else None,
        })


def assemble(state: PipelineState, config) -> PipelineResult:  # type: ignore[no-untyped-def]
    deps = get_deps(config)
    diaries = sorted(state.get("diaries") or [], key=lambda d: (d.prdlst_code or "zzz", d.prdlst_nm))
    report = state.get("report")
    sr = state.get("speaker_roles")
    speaker_map: dict[str, str] = {}
    if sr:
        by_file = {f.file_index: f for f in sr.files}
        for key in state["raw"].speakers:
            try:
                fi, letter = key.split(":", 1)
                f = by_file.get(int(fi[1:]))
                role = f.mapping.get(letter) if f and f.confidence >= 0.6 else None
                speaker_map[key] = role if role in ("farmer", "consultant") else "unknown"
            except Exception:  # noqa: BLE001
                speaker_map[key] = "unknown"
    usage_list = state.get("usage") or []
    usage = {"calls": len(usage_list),
             "prompt_tokens": sum(u.prompt_tokens for u in usage_list),
             "completion_tokens": sum(u.completion_tokens for u in usage_list),
             "total_tokens": sum(u.total_tokens for u in usage_list),
             "by_call": [u.model_dump() for u in usage_list]}
    warnings = list(dict.fromkeys(state.get("warnings") or []))
    for e in state.get("errors") or []:
        warnings.append(f"[{e.node}] {e.message}")
    for d in diaries:
        if d.markdown and hangul_ratio(d.markdown) < 0.5:
            warnings.append(f"{d.prdlst_nm}: 한국어 비율 낮음 — 검토 필요")
    if report and report.markdown and hangul_ratio(report.markdown) < 0.5:
        warnings.append("보고서: 한국어 비율 낮음 — 검토 필요")
    facts = state.get("facts")
    farm = state.get("farm")
    model = None
    for u in usage_list:
        if u.model:
            model = u.model
            break
    return PipelineResult(
        diaries=[diary_to_artifact(d) for d in diaries],
        report=ReportArtifact(markdown=report.markdown, structured=report.json_.model_dump()) if report else None,
        speaker_map=speaker_map, facts=facts.model_dump() if facts else None, warnings=warnings,
        usage=usage, model=model or deps.settings.llm_model, prompt_version=PROMPT_VERSION,
        farmos_status=(farm.status if farm else "disabled"),
    )


async def finalize(state: PipelineState, config) -> dict:  # type: ignore[no-untyped-def]
    # 결과 조립은 graph.run 에서 assemble() 로 수행 (상태에 큰 객체를 또 넣지 않음)
    return {}
