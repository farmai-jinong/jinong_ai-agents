"""드라이런 CLI — 서버 없이 픽스처(MergedTranscript+CallContext JSON)로 파이프라인을 돌린다.

    python -m app.agents.run --transcript tests/agents/fixtures/calls/strawberry_botrytis.json \
        [--no-farmos | --farmos-fixture tests/agents/fixtures/farmos | --farmos-token $TOKEN --farmos-base-url URL] \
        [--provider openai|jinong|gemini|fake] [--facts out/facts.json] [--only extract|diary|report] \
        [--out out/<call_id>] [--dump-prompts]

`--provider fake` 는 golden/<fixture>.facts.json 을 캔드 응답으로 쓴다(LLM 없이 렌더/매핑 점검).
`--facts` 는 추출을 건너뛰고 주어진 CallFacts 로 일지/보고서만 다시 만든다(프롬프트 반복 비용 절감).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from ..clients.farmos import FarmosClient
from ..clients.llm import make_chat_model
from ..config import Settings
from ..schemas.pipeline import CallContext
from ..schemas.transcript import MergedTranscript
from .deps import Deps
from .graph import LangGraphPipeline
from .interface import PipelineEmpty
from .prompts.loader import PROMPT_VERSION
from .tools.fake_farmos import FakeFarmosClient
from .tools.fake_llm import FakeChatModel

log = logging.getLogger("agents.run")
FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "agents" / "fixtures"


def load_fixture(path: str) -> tuple[MergedTranscript, CallContext]:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return MergedTranscript(**d["transcript"]), CallContext(**d["ctx"])


def fake_llm_for(fixture_path: str, facts_override: dict | None = None) -> FakeChatModel:
    stem = Path(fixture_path).stem
    responses: dict[str, Any] = {}
    gold = FIXTURES / "golden"
    facts_file = gold / f"{stem}.facts.json"
    if not facts_file.exists():
        # 같은 첫 토큰(strawberry_…) 을 공유하는 golden 으로 폴백
        head = stem.split("_")[0]
        cands = sorted(gold.glob(f"{head}_*.facts.json"))
        if cands:
            facts_file = cands[0]
    if facts_override is not None:
        responses["extract"] = facts_override
    elif facts_file.exists():
        responses["extract"] = json.loads(facts_file.read_text(encoding="utf-8"))
    roles_file = gold / "speaker_roles.json"
    if roles_file.exists():
        roles = json.loads(roles_file.read_text(encoding="utf-8"))
        for k, v in roles.items():
            if stem.startswith(k):
                responses["speaker_roles"] = v
    # 캔드 응답이 없는 서술 패스는 실패시켜 결정적 대체 경로를 태운다
    fail = {k for k in ("report", "diary_content") if k not in responses}
    return FakeChatModel(responses=responses, fail_kinds=fail)


def make_llm(settings: Settings, provider: str | None, fixture: str, facts_override: dict | None):
    if provider == "fake":
        return fake_llm_for(fixture, facts_override)
    if provider in ("openai", "jinong", "gemini"):
        settings.llm_provider = provider
        if provider == "jinong" and not os.environ.get("LLM_BASE_URL"):
            settings.llm_base_url = "https://jinong-stt.jinongservice.co.kr/v1"
            settings.llm_model = os.environ.get("LLM_MODEL", "exaone45")
        if provider == "gemini":
            # Vertex AI: GOOGLE_APPLICATION_CREDENTIALS(SA 키) 필요. 프로젝트/모델은 env 우선.
            settings.llm_model = os.environ.get("LLM_MODEL") or ("gemini-3.5-flash" if not settings.llm_model.startswith("gemini") else settings.llm_model)
            settings.gcp_project_id = os.environ.get("GCP_PROJECT_ID") or settings.gcp_project_id or "jinong-lab-llm"
    if settings.llm_provider != "gemini" and not settings.llm_api_key:
        settings.llm_api_key = os.environ.get("OPENAI_API_KEY", "")
    return make_chat_model(settings)


class _FactsInjectingLLM:
    """--facts: 추출 호출만 캔드 응답으로 가로채고 나머지는 실제 LLM."""

    def __init__(self, real: Any, facts: dict) -> None:
        self.real = real
        self.fake = FakeChatModel(responses={"extract": facts})
        self.model_name = getattr(real, "model_name", None)

    async def ainvoke(self, messages, **kw):  # type: ignore[no-untyped-def]
        from .tools.fake_llm import detect_kind
        if detect_kind(messages) == "extract":
            return await self.fake.ainvoke(messages, **kw)
        return await self.real.ainvoke(messages, **kw)

    def with_structured_output(self, schema, **kw):  # type: ignore[no-untyped-def]
        real_r = self.real.with_structured_output(schema, **kw)

        class _R:
            async def ainvoke(self, messages, **kw2):  # type: ignore[no-untyped-def]
                from .tools.fake_llm import detect_kind
                if detect_kind(messages) == "extract":
                    raise NotImplementedError("facts injected")
                return await real_r.ainvoke(messages, **kw2)
        return _R()

    def bind(self, **kw):  # type: ignore[no-untyped-def]
        outer = self

        class _B:
            async def ainvoke(self, messages, **kw2):  # type: ignore[no-untyped-def]
                return await outer.ainvoke(messages, **kw2)
        return _B()


async def amain(args: argparse.Namespace) -> int:
    settings = Settings(_env_file=".env" if Path(".env").exists() else None)
    settings.pipeline_impl = "langgraph"
    if args.dump_prompts:
        settings.prompt_dump_dir = args.out
    facts_override = json.loads(Path(args.facts).read_text(encoding="utf-8")) if args.facts else None
    transcript, ctx = load_fixture(args.transcript)
    llm = make_llm(settings, args.provider, args.transcript, facts_override)
    if facts_override is not None and args.provider != "fake":
        llm = _FactsInjectingLLM(llm, facts_override)

    farmos_factory = None
    if args.farmos_fixture:
        fx = args.farmos_fixture
        farmos_factory = lambda tok: FakeFarmosClient(fx)  # noqa: E731
        if not ctx.farm_access_token:
            ctx.farm_access_token = "fixture"
    elif args.farmos_token:
        base = args.farmos_base_url or settings.farmos_base_url
        farmos_factory = lambda tok: FarmosClient(base, tok, timeout=settings.farmos_timeout)  # noqa: E731
        ctx.farm_access_token = args.farmos_token
    elif not args.no_farmos and ctx.farm_access_token and ctx.farm_access_token != "tok-fake":
        farmos_factory = lambda tok: FarmosClient(settings.farmos_base_url, tok, timeout=settings.farmos_timeout)  # noqa: E731
    else:
        ctx.farm_access_token = None

    deps = Deps(settings=settings, llm=llm, farmos_factory=farmos_factory, prompt_version=PROMPT_VERSION,
                dump_dir=args.out if args.dump_prompts else None)
    pipeline = LangGraphPipeline(settings, deps)
    out_dir = Path(args.out or f"out/{ctx.call_id}")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = await pipeline.run(transcript, ctx)
    except PipelineEmpty as e:
        print(f"EMPTY: {e}")
        (out_dir / "result.json").write_text(json.dumps({"status": "EMPTY", "reason": str(e)}, ensure_ascii=False), encoding="utf-8")
        return 0
    (out_dir / "result.json").write_text(result.model_dump_json(indent=1), encoding="utf-8")
    if result.facts:
        (out_dir / "facts.json").write_text(json.dumps(result.facts, ensure_ascii=False, indent=1), encoding="utf-8")
    for d in result.diaries:
        stem = d.prdlst_code or d.prdlst_nm
        (out_dir / f"diary_{stem}.md").write_text(d.markdown, encoding="utf-8")
        (out_dir / f"diary_{stem}.json").write_text(json.dumps(d.structured, ensure_ascii=False, indent=1), encoding="utf-8")
    if result.report:
        (out_dir / "report.md").write_text(result.report.markdown, encoding="utf-8")
        (out_dir / "report.json").write_text(json.dumps(result.report.structured, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"== {ctx.call_id} → {out_dir}")
    print(f"diaries: {[(d.prdlst_code, d.prdlst_nm, d.status) for d in result.diaries]}")
    print(f"speaker_map: {result.speaker_map} | farmos: {result.farmos_status} | model: {result.model}")
    print(f"usage: {result.usage.get('calls')} calls, {result.usage.get('total_tokens')} tokens")
    if result.warnings:
        print("warnings:")
        for w in result.warnings:
            print(f"  - {w}")
    if not args.quiet:
        for d in result.diaries:
            print("\n" + d.markdown)
        if result.report:
            print("\n" + result.report.markdown)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="jinong_ai-agents 파이프라인 드라이런")
    ap.add_argument("--transcript", required=True, help="MergedTranscript+CallContext JSON 픽스처")
    ap.add_argument("--provider", choices=["openai", "jinong", "gemini", "fake"], default=None)
    ap.add_argument("--no-farmos", action="store_true")
    ap.add_argument("--farmos-fixture", help="FakeFarmosClient 픽스처 디렉터리")
    ap.add_argument("--farmos-token", help="dev farmos 농가 JWT")
    ap.add_argument("--farmos-base-url")
    ap.add_argument("--facts", help="CallFacts JSON — 추출 생략")
    ap.add_argument("--only", choices=["extract", "diary", "report"], help="(예약) 부분 실행")
    ap.add_argument("--out")
    ap.add_argument("--dump-prompts", action="store_true")
    ap.add_argument("--quiet", "-q", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
