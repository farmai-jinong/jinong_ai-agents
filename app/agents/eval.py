"""픽스처 평가 하네스 — 실제 LLM(또는 fake)로 픽스처를 돌려 기대 항목 재현율·근거 유효율·토큰을 표로 출력.

    python -m app.agents.eval [--provider openai|jinong|fake] [--fixtures strawberry_botrytis,tomato_two_files_speaker_flip]
                              [--farmos-fixture tests/agents/fixtures/farmos] [--out out/eval]

기대값은 tests/agents/fixtures/golden/<name>.expect.json:
  {"farmworks": ["관수","적엽"], "pests": [["잿빛곰팡이병","1"]], "products": ["사파이어"], "diary_status": {"0804MM":"OK"}}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from ..config import Settings
from .deps import Deps
from .graph import LangGraphPipeline
from .interface import PipelineEmpty
from .mapping.matcher import normalize
from .run import FIXTURES, load_fixture, make_llm
from .tools.fake_farmos import FakeFarmosClient

_HANGUL = re.compile(r"[가-힣]")


def _expect(name: str) -> dict[str, Any]:
    p = FIXTURES / "golden" / f"{name}.expect.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _recall(expected: list[str], got: list[str], family: str | None = None) -> tuple[int, int]:
    g = {normalize(x, family) for x in got}
    hit = sum(1 for e in expected if normalize(e, family) in g or any(normalize(e, family) in x or x in normalize(e, family) for x in g))
    return hit, len(expected)


def _evidence_validity(result) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    facts = result.facts or {}
    total = valid = 0
    for v in facts.values():
        if isinstance(v, list):
            for it in v:
                if isinstance(it, dict) and "evidence" in it:
                    total += 1
                    if it["evidence"]:
                        valid += 1
    return valid, total


async def run_one(name: str, provider: str | None, farmos_fixture: str | None, out: Path) -> dict[str, Any]:
    fixture = FIXTURES / "calls" / f"{name}.json"
    settings = Settings(_env_file=".env" if Path(".env").exists() else None)
    transcript, ctx = load_fixture(str(fixture))
    llm = make_llm(settings, provider, str(fixture), None)
    factory = (lambda tok: FakeFarmosClient(farmos_fixture)) if (farmos_fixture and "no_farmos" not in name) else None
    if factory and not ctx.farm_access_token:
        ctx.farm_access_token = "fixture"
    if not factory:
        ctx.farm_access_token = None
    pipe = LangGraphPipeline(settings, Deps(settings=settings, llm=llm, farmos_factory=factory))
    row: dict[str, Any] = {"fixture": name}
    try:
        res = await pipe.run(transcript, ctx)
    except PipelineEmpty as e:
        row.update(status="EMPTY", reason=str(e))
        return row
    exp = _expect(name)
    facts = res.facts or {}
    fw_got = [f["name"] for f in facts.get("farmworks", [])]
    pest_got = [p["name"] for p in facts.get("pests", [])]
    prod_got = [p["name"] for p in facts.get("products", [])]
    row["farmworks_recall"] = _recall(exp.get("farmworks", []), fw_got, "farmwork")
    row["pests_recall"] = _recall([p[0] if isinstance(p, list) else p for p in exp.get("pests", [])], pest_got, "pest")
    row["products_recall"] = _recall(exp.get("products", []), prod_got, "product")
    row["evidence_valid"] = _evidence_validity(res)
    row["diaries"] = {d.prdlst_code or d.prdlst_nm: d.status for d in res.diaries}
    row["diary_status_ok"] = all(row["diaries"].get(k) == v for k, v in exp.get("diary_status", {}).items())
    mapped = sum(1 for d in res.diaries for fam in ("farmworks", "pests", "products") for m in d.structured["mapping"][fam] if m["status"] == "matched")
    row["mapped"] = mapped
    row["speaker_map"] = res.speaker_map
    row["tokens"] = res.usage.get("total_tokens")
    row["calls"] = res.usage.get("calls")
    row["model"] = res.model
    row["warnings"] = len(res.warnings)
    md_all = "\n".join(d.markdown for d in res.diaries) + (res.report.markdown if res.report else "")
    letters = [c for c in md_all if c.isalpha()]
    row["hangul_ratio"] = round(sum(1 for c in letters if _HANGUL.match(c)) / max(1, len(letters)), 3)
    d = out / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(res.model_dump_json(indent=1), encoding="utf-8")
    for di in res.diaries:
        (d / f"diary_{di.prdlst_code or di.prdlst_nm}.md").write_text(di.markdown, encoding="utf-8")
    if res.report:
        (d / "report.md").write_text(res.report.markdown, encoding="utf-8")
    return row


async def amain(args: argparse.Namespace) -> int:
    names = args.fixtures.split(",") if args.fixtures else sorted(p.stem for p in (FIXTURES / "calls").glob("*.json"))
    out = Path(args.out)
    rows = []
    for n in names:
        try:
            rows.append(await run_one(n, args.provider, args.farmos_fixture, out))
        except Exception as e:  # noqa: BLE001
            rows.append({"fixture": n, "status": "ERROR", "reason": f"{type(e).__name__}: {e}"})
    print(f"{'fixture':38} {'fw':>6} {'pest':>6} {'prod':>6} {'evid':>7} {'map':>4} {'tok':>7} {'calls':>5} {'ko':>5} status")
    for r in rows:
        if "farmworks_recall" not in r:
            print(f"{r['fixture']:38} {'-':>6} {'-':>6} {'-':>6} {'-':>7} {'-':>4} {'-':>7} {'-':>5} {'-':>5} {r.get('status')} {r.get('reason','')}")
            continue
        f = lambda t: f"{t[0]}/{t[1]}"  # noqa: E731
        print(f"{r['fixture']:38} {f(r['farmworks_recall']):>6} {f(r['pests_recall']):>6} {f(r['products_recall']):>6} "
              f"{f(r['evidence_valid']):>7} {r['mapped']:>4} {str(r['tokens']):>7} {str(r['calls']):>5} {r['hangul_ratio']:>5} "
              f"{r['diaries']} {'OK' if r['diary_status_ok'] else 'MISMATCH'} warn={r['warnings']}")
    (out / "eval.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["openai", "jinong", "fake"], default="fake")
    ap.add_argument("--fixtures")
    ap.add_argument("--farmos-fixture", default=str(FIXTURES / "farmos"))
    ap.add_argument("--out", default="out/eval")
    args = ap.parse_args(argv)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
