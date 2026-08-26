"""음성 테스트케이스 평가 CLI.

    python -m app.agents.voice_eval [--audio-dir ~/Downloads/recordings] [--cases a,b]
        [--stages stt,pipeline,judge] [--force stt|pipeline|judge|all] [--out out/voice-eval]
        [--provider gemini] [--judge-provider gemini] [--judge-model gemini-3.5-pro] [--judge-repeat 1]
        [--farmos-fixture DIR | --farmos-token TOKEN] [--baseline out/voice-eval/summary.json]
        [--materialize] [--no-gate] [--no-convert] [--dump-prompts]

단계별로 산출물을 캐시한다 — `stt.json` 이 있으면 게이트웨이를 다시 부르지 않으므로, 프롬프트를 고친 뒤에는
`--stages pipeline,judge` 로 싸게 재평가할 수 있다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from ...config import Settings
from ...schemas.pipeline import PipelineResult
from ..deps import Deps
from ..graph import LangGraphPipeline
from ..interface import PipelineEmpty
from ..prompts.loader import PROMPT_VERSION
from ..run import FIXTURES, load_fixture, make_llm
from ..tools.fake_farmos import FakeFarmosClient
from . import audio as audio_mod
from . import judge as judge_mod
from . import metrics, report, stt_score, transcribe
from .cases import TESTCASES, VoiceCase, load_cases

log = logging.getLogger("voice_eval")
STAGES = ("stt", "pipeline", "judge")
AUDIO_MAP_PATH = TESTCASES / "audio_map.json"


# --------------------------------------------------------------------------- stage 1: 전사
async def stage_stt(case: VoiceCase, out: Path, settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    raw_path = out / "stt.json"
    if args.force in ("stt", "all") or not raw_path.exists():
        if case.audio is None:
            raise FileNotFoundError(f"{case.name}: 녹음 파일을 찾지 못했다 (--audio-dir 확인)")
        upload = audio_mod.prepare(case.audio, out / "audio.wav", convert=not args.no_convert)
        log.info("%s: 전사 요청 %s", case.name, upload.name)
        raw = await transcribe.transcribe(settings, upload)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    result = transcribe.parse_raw(transcribe.load_raw(raw_path))

    tr = transcribe.build_transcript(case.call_id(), result, key=(case.audio.name if case.audio else "cached.wav"))
    ctx = case.context(with_farmos=bool(args.farmos_fixture or args.farmos_token))
    transcribe.write_fixture(case, tr, ctx, out / "fixture.json")
    transcribe.write_transcript_md(tr, out / "stt.md")

    sc = stt_score.score(reference=case.reference_text, hypothesis=result.text or tr.text,
                         keywords=case.stt_keywords, expect=case.expect,
                         segments=result.segments, duration_sec=tr.total_duration_sec)
    (out / "stt_score.json").write_text(json.dumps(sc.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    return sc.to_dict()


# --------------------------------------------------------------------------- stage 2: 파이프라인
async def stage_pipeline(case: VoiceCase, out: Path, settings: Settings,
                         args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    fixture = out / "fixture.json"
    if not fixture.exists():
        raise FileNotFoundError(f"{case.name}: fixture.json 없음 — 먼저 --stages stt 로 전사할 것")
    result_path = out / "result.json"
    if args.force in ("pipeline", "all") or not result_path.exists():
        transcript, ctx = load_fixture(str(fixture))
        llm = make_llm(settings, args.provider, str(fixture), None)
        farmos_factory = None
        if args.farmos_fixture:
            fx = args.farmos_fixture
            farmos_factory = lambda tok: FakeFarmosClient(fx)  # noqa: E731
            ctx.farm_access_token = ctx.farm_access_token or "fixture"
        elif args.farmos_token:
            from ...clients.farmos import FarmosClient
            ctx.farm_access_token = args.farmos_token
            farmos_factory = lambda tok: FarmosClient(settings.farmos_base_url, tok,  # noqa: E731
                                                      timeout=settings.farmos_timeout)
        else:
            ctx.farm_access_token = None
        deps = Deps(settings=settings, llm=llm, farmos_factory=farmos_factory, prompt_version=PROMPT_VERSION,
                    dump_dir=str(out) if args.dump_prompts else None)
        try:
            result = await LangGraphPipeline(settings, deps).run(transcript, ctx)
        except PipelineEmpty as e:
            raise RuntimeError(f"PipelineEmpty: {e}") from e
        result_path.write_text(result.model_dump_json(indent=1), encoding="utf-8")
        if result.facts:
            (out / "facts.json").write_text(json.dumps(result.facts, ensure_ascii=False, indent=1), encoding="utf-8")
        for d in result.diaries:
            (out / f"diary_{d.prdlst_code or d.prdlst_nm}.md").write_text(d.markdown, encoding="utf-8")
        if result.report:
            (out / "report.md").write_text(result.report.markdown, encoding="utf-8")
    else:
        result = PipelineResult(**json.loads(result_path.read_text(encoding="utf-8")))

    diary = metrics._diary_for(result, case.prdlst_code)
    return metrics.score(case, result), (diary.markdown if diary else "")


# --------------------------------------------------------------------------- stage 3: judge
async def stage_judge(case: VoiceCase, out: Path, settings: Settings, args: argparse.Namespace,
                      diary_md: str, llm: Any) -> dict[str, Any]:
    judge_path = out / "judge.json"
    if args.force not in ("judge", "all") and judge_path.exists():
        return json.loads(judge_path.read_text(encoding="utf-8"))
    transcript, _ctx = load_fixture(str(out / "fixture.json"))
    verdict = await judge_mod.judge(llm, case, diary_md, transcript.text, settings=settings,
                                    repeat=args.judge_repeat or settings.judge_repeat,
                                    dump_dir=str(out) if args.dump_prompts else None)
    judge_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")
    return verdict


# --------------------------------------------------------------------------- 오디오 매칭
async def attach_audio(cases: list[VoiceCase], out_root: Path, settings: Settings,
                       args: argparse.Namespace) -> None:
    """케이스에 녹음 파일을 붙인다 — 파일명 → audio_map.json → (남으면) 전사 유사도."""
    files = audio_mod.list_audio(Path(args.audio_dir).expanduser())
    if not files:
        log.warning("녹음 파일 없음: %s (캐시된 stt.json 만으로 진행)", args.audio_dir)
        return
    matched = audio_mod.match_by_name([c.name for c in cases], files,
                                      audio_mod.load_audio_map(AUDIO_MAP_PATH))
    for c in cases:
        c.audio = matched.get(c.name)
    missing = [c for c in cases if c.audio is None and not (out_root / c.name / "stt.json").exists()]
    leftover = [f for f in files if f not in set(matched.values())]
    if not (missing and leftover):
        return
    log.warning("파일명·audio_map 으로 못 정한 케이스 %s / 남은 파일 %s — 전사해서 대본 유사도로 배정한다",
                [c.name for c in missing], [f.name for f in leftover])
    scratch = out_root / "_unmatched"
    scratch.mkdir(parents=True, exist_ok=True)
    hyp: dict[str, str] = {}
    for f in leftover:
        cache = scratch / f"{f.stem}.json"
        if not cache.exists():
            upload = audio_mod.prepare(f, scratch / f"{f.stem}.wav", convert=not args.no_convert)
            cache.write_text(json.dumps(await transcribe.transcribe(settings, upload), ensure_ascii=False),
                             encoding="utf-8")
        hyp[f.name] = transcribe.parse_raw(transcribe.load_raw(cache)).text
    assignment = audio_mod.resolve_by_similarity(hyp, {c.name: c.reference_text for c in missing})
    by_name = {f.name: f for f in leftover}
    for fname, case_name in assignment.items():
        case = next(c for c in cases if c.name == case_name)
        case.audio = by_name[fname]
        dest = out_root / case_name / "stt.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copyfile(scratch / f"{by_name[fname].stem}.json", dest)
        log.warning("유사도 배정: %s → %s (audio_map.json 에 적어두면 다음부터 바로 매칭된다)",
                    fname, case_name)


def verify_audio_match(case: VoiceCase, stt_dict: dict[str, Any]) -> str | None:
    """대본 유사도가 너무 낮으면 오디오 오배치 의심 — 경고 문자열을 돌려준다."""
    sim = stt_dict.get("similarity", 0)
    return None if sim >= 55 else f"대본 유사도 {sim} — 다른 케이스의 녹음일 수 있음"


# --------------------------------------------------------------------------- 실행
async def run_case(case: VoiceCase, out_root: Path, settings: Settings, args: argparse.Namespace,
                   stages: set[str], judge_llm: Any) -> dict[str, Any]:
    out = out_root / case.name
    out.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {"case": case.name}
    try:
        if "stt" in stages or not (out / "stt_score.json").exists():
            row["stt"] = await stage_stt(case, out, settings, args)
        else:
            row["stt"] = json.loads((out / "stt_score.json").read_text(encoding="utf-8"))
        warn = verify_audio_match(case, row["stt"])
        if warn:
            log.warning("%s: %s", case.name, warn)
            row["audio_warning"] = warn

        diary_md = ""
        if "pipeline" in stages or "judge" in stages:
            row["pipeline"], diary_md = await stage_pipeline(case, out, settings, args)
        if "judge" in stages and diary_md:
            row["judge"] = await stage_judge(case, out, settings, args, diary_md, judge_llm)
    except Exception as e:  # noqa: BLE001 — 한 케이스 실패가 나머지를 막지 않는다
        log.exception("%s 실패", case.name)
        row["error"] = f"{type(e).__name__}: {e}"
    return row


def materialize(cases: list[VoiceCase], out_root: Path) -> None:
    """전사 픽스처를 `python -m app.agents.eval` 이 바로 쓰도록 리포지토리에 편입 (케이스 README 4번).

    `calls/`(전사) + `golden/*.expect.json`(기대치) 에 더해, 실행 결과의 facts 와 화자 역할을 캔드 응답으로
    같이 떨군다 — 그래야 `--provider fake` 로도 LLM 없이 렌더·매핑을 점검할 수 있다(기존 픽스처와 동일 규약).
    """
    roles_path = FIXTURES / "golden" / "speaker_roles.json"
    roles = json.loads(roles_path.read_text(encoding="utf-8")) if roles_path.exists() else {}
    for c in cases:
        src = out_root / c.name / "fixture.json"
        if not src.exists():
            continue
        stem = f"voice_{c.name}"
        shutil.copyfile(src, FIXTURES / "calls" / f"{stem}.json")
        shutil.copyfile(c.dir / "expect.json", FIXTURES / "golden" / f"{stem}.expect.json")
        facts = out_root / c.name / "facts.json"
        if facts.exists():
            shutil.copyfile(facts, FIXTURES / "golden" / f"{stem}.facts.json")
        result = out_root / c.name / "result.json"
        if result.exists():
            smap = json.loads(result.read_text(encoding="utf-8")).get("speaker_map") or {}
            roles[stem] = {"files": [{
                "file_index": 0, "confidence": 1.0, "rationale": f"{c.name} 실녹음 전사 실행 결과",
                "roles": [{"letter": k.split(":")[-1], "role": v if v != "unknown" else "other"}
                          for k, v in smap.items()],
            }]}
        log.info("materialize: %s", stem)
    roles_path.write_text(json.dumps(roles, ensure_ascii=False, indent=1), encoding="utf-8")


async def amain(args: argparse.Namespace) -> int:
    settings = Settings(_env_file=".env" if Path(".env").exists() else None)
    settings.pipeline_impl = "langgraph"
    stages = {s.strip() for s in args.stages.split(",") if s.strip()}
    if unknown := stages - set(STAGES):
        print(f"알 수 없는 stage: {unknown} (가능: {STAGES})", file=sys.stderr)
        return 2

    cases = load_cases(args.cases.split(",") if args.cases else None)
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    await attach_audio(cases, out_root, settings, args)

    judge_llm = None
    if "judge" in stages:
        judge_llm = judge_mod.make_judge_llm(settings, args.judge_provider, args.judge_model)
        log.info("judge 모델: %s (파이프라인: %s)",
                 getattr(judge_llm, "model_name", None) or settings.judge_model, settings.llm_model)
        try:
            await judge_mod.probe_judge(judge_llm)
        except RuntimeError as e:
            print(e, file=sys.stderr)
            return 2

    rows = [await run_case(c, out_root, settings, args, stages, judge_llm) for c in cases]

    summary = report.aggregate(rows)
    thresholds = report.load_thresholds()
    baseline = None
    if args.baseline and Path(args.baseline).exists():
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8")).get("summary")
    fails = report.gate(summary, thresholds)
    md = report.render(rows, summary, thresholds, baseline, fails)
    (out_root / "report.md").write_text(md, encoding="utf-8")
    (out_root / "summary.json").write_text(
        json.dumps({"summary": summary, "thresholds": thresholds, "gate_failures": fails, "cases": rows},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    if args.materialize:
        materialize(cases, out_root)

    print(md.split("## 2.")[0])
    print(f"→ {out_root / 'report.md'}")
    if fails and not args.no_gate:
        print("게이트 실패: " + "; ".join(fails), file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="음성 테스트케이스 평가 (STT 정확도 + 영농일지 LLM judge)")
    ap.add_argument("--audio-dir", default="~/Downloads/recordings")
    ap.add_argument("--cases", help="쉼표 구분 케이스 이름 (기본 전체)")
    ap.add_argument("--stages", default=",".join(STAGES))
    ap.add_argument("--force", choices=[*STAGES, "all"], help="캐시 무시하고 해당 단계 재실행")
    ap.add_argument("--out", default="out/voice-eval")
    ap.add_argument("--provider", choices=["openai", "jinong", "gemini", "fake"], default=None)
    ap.add_argument("--judge-provider", choices=["openai", "jinong", "gemini", "fake"], default=None)
    ap.add_argument("--judge-model")
    ap.add_argument("--judge-repeat", type=int, default=0, help="0 = 설정값(JUDGE_REPEAT)")
    ap.add_argument("--farmos-fixture", default=str(FIXTURES / "farmos"))
    ap.add_argument("--farmos-token", help="실 farmos 조회 (농가 JWT)")
    ap.add_argument("--no-farmos", action="store_true")
    ap.add_argument("--baseline", help="이전 summary.json — 델타 비교")
    ap.add_argument("--materialize", action="store_true", help="픽스처를 tests/agents/fixtures 에 복사")
    ap.add_argument("--no-gate", action="store_true", help="임계값 미달이어도 exit 0")
    ap.add_argument("--no-convert", action="store_true", help="ffmpeg 변환 없이 원본 업로드")
    ap.add_argument("--dump-prompts", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.no_farmos:
        args.farmos_fixture = None
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
