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
        # 실제 템플릿(render/templates/diary.md.j2)과 같은 골격: 상단 요약·격려 인용 블록 → 표 → 고정 섹션, H1 없음.
        # internal(근거 포함) / public(근거·코드·참고 제거) 두 벌 — 실파이프라인의 render 변형과 같은 제거 규칙.
        head = f"> 📝 **통화 요약** · (fake) 세그먼트 {len(transcript.segments)}건\n> 💬 오늘도 수고 많으셨어요 🌱\n\n"
        body = (f"## 주요 농작업\n- 언급 없음\n\n"
                f"## 기타 기록사항\n[AI 초안·FAKE] 통화 세그먼트 {len(transcript.segments)}건\n\n")
        diary_md = (head + f"| 항목 | 값 |\n|---|---|\n| 작성일자 | {diary_date} |\n| 작물 | {name} ({code}) |\n\n"
                    + body + f"## 근거 발화\n{lines}\n\n## 참고\n- 없음\n")
        diary_md_public = (head + f"| 항목 | 값 |\n|---|---|\n| 작성일자 | {diary_date} |\n| 작물 | {name} |\n\n" + body)
        report_head = f"# 컨설팅 보고서 — {diary_date}\n\n## 대화 요약\n(fake) 세그먼트 {len(transcript.segments)}건\n"
        report_md = report_head + f"\n## 근거 발화\n{lines}\n"
        report_md_public = report_head
        return PipelineResult(
            diaries=[DiaryArtifact(prdlst_code=code, prdlst_nm=name, diary_date=diary_date,
                                   status="OK", markdown=diary_md, markdown_public=diary_md_public,
                                   structured={"schema_version": "1", "prefill": None, "prefill_ready": False,
                                               "mapping": {}, "warnings": ["fake pipeline"]})],
            report=ReportArtifact(markdown=report_md, markdown_public=report_md_public, structured={"summary": "fake", "keywords": [],
                                                                  "action_items": [], "sections": {}}),
            speaker_map={k: "unknown" for k in transcript.speakers},
            warnings=["fake pipeline"], model="fake", prompt_version="0", farmos_status="disabled",
        )
