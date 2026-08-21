"""결정적 가짜 파이프라인 — 테스트/로컬(LLM 없이) 용. PIPELINE_IMPL=fake."""

from __future__ import annotations

from ..schemas.pipeline import CallContext, DiaryArtifact, PipelineResult, ReportArtifact
from ..schemas.transcript import MergedTranscript
from .interface import PipelineEmpty


class FakePipeline:
    async def run(self, transcript: MergedTranscript, ctx: CallContext) -> PipelineResult:
        if transcript.is_empty:
            raise PipelineEmpty("no speech")
        date = (ctx.started_at or ctx.ended_at)
        diary_date = ctx.hints.diary_date or (date.date().isoformat() if date else "1970-01-01")
        code = ctx.hints.prdlst_code or "0000MM"
        name = ctx.hints.prdlst_nm or "미확정작물"
        lines = "\n".join(f"- [{s.speaker_key}] {s.text}" for s in transcript.segments[:20])
        diary_md = (
            f"# 영농일지 — {name} ({diary_date})\n\n## 주요 농작업\n- 언급 없음\n\n"
            f"## 기타 기록사항\n[AI 초안·FAKE] 통화 세그먼트 {len(transcript.segments)}건\n\n"
            f"## 근거 발화\n{lines}\n"
        )
        report_md = (
            f"# 컨설팅 보고서 — {diary_date}\n\n## 대화 요약\n(fake) 세그먼트 {len(transcript.segments)}건\n\n"
            f"## 근거 발화\n{lines}\n"
        )
        return PipelineResult(
            diaries=[DiaryArtifact(prdlst_code=code, prdlst_nm=name, diary_date=diary_date,
                                   status="OK", markdown=diary_md,
                                   structured={"schema_version": "1", "prefill": None, "prefill_ready": False,
                                               "mapping": {}, "warnings": ["fake pipeline"]})],
            report=ReportArtifact(markdown=report_md, structured={"summary": "fake", "keywords": [],
                                                                  "action_items": [], "sections": {}}),
            speaker_map={k: "unknown" for k in transcript.speakers},
            warnings=["fake pipeline"], model="fake", prompt_version="0", farmos_status="disabled",
        )
