"""좌표(coordinate) — 이 전사가 **무엇으로** 만들어졌는지 기록하고 대조한다.

이 하네스가 두 번 크게 틀린 곳이 전부 좌표였다.

1. 2026-09-08 오전: `deploy-base-k10-jinong` 덤프를 "agents base 좌표" 로 알고 판정 기준을 잡았는데,
   그건 정답 대본에서 뽑은 bias 목록을 프롬프트에 넣은 **오라클 팔**이었다. base 용어 recall 이 .84 로
   부풀어 헤드룸이 4배 축소돼 보였고, 그 위에서 정한 절대 하한이 진짜 좌표(.35)에서는 무의미했다.
2. 2026-09-08 오후: `:8102`(화자분리)가 승격 체크포인트를 안 타고 base 에 묶여 있었다. 그걸 :8100 위임으로
   바꾸자 같은 세트에서 도메인 용어 recall 이 .3526 → .5769 로 뛰었다 — **후보정이 고쳐야 할 문제 자체가
   달라졌다.**

그래서 산출물마다 좌표를 붙여 두고, 다른 좌표의 수치를 나란히 놓으려 할 때 **경고가 뜨게** 한다.
좌표는 값 몇 개가 아니라 "이 숫자를 다시 만들려면 무엇이 같아야 하는가" 의 목록이다.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SERVING_PROBE = r'''
import json, os, re, subprocess, sys
B = os.environ.get("JINONG_GPU_BASE", "/NHNHOME/WORKSPACE/26mafra001_A/BASE/jinong")
out = {"base": B}
try:
    ps = subprocess.run(["pgrep", "-fa", "qwen_(diar_server|asr_serve_ctx|ctx_server)"],
                        capture_output=True, text=True).stdout
except Exception:
    ps = ""
if not ps:
    ps = subprocess.run(["bash", "-lc", "pgrep -fa 'qwen_diar_server|qwen_asr_serve_ctx|qwen_ctx_server'"],
                        capture_output=True, text=True).stdout
out["processes"] = [l.split(" ", 1)[1] for l in ps.strip().splitlines() if " " in l]
served = os.path.join(B, "models/qwen3-asr-served")
out["served_model"] = os.path.realpath(served) if os.path.exists(served) else None
cat = os.path.join(B, "stt-serve/catalog/catalog.jsonl")
if os.path.exists(cat):
    import hashlib
    h = hashlib.md5()
    with open(cat, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    out["catalog_md5"] = h.hexdigest()
    out["catalog_lines"] = sum(1 for _ in open(cat, encoding="utf-8"))
json.dump(out, sys.stdout, ensure_ascii=False)
'''


@dataclass
class Coordinate:
    """산출물 한 벌을 만든 조건. 같은 좌표끼리만 나란히 놓을 수 있다."""
    source: str = ""                  # 전사가 어디서 왔나 (덤프 태그 · 서빙 URL · 픽스처 경로)
    kind: str = ""                    # eval-dump | serving | fixture
    model: str = ""                   # 실제로 디코드한 가중치
    engine: str = ""                  # in-process greedy | FunASR | vLLM(:8100) | ctx layer …
    context: str = "none"             # none | retriever | oracle-bias  ← 오라클을 섞지 않기 위한 필드
    catalog_md5: str = ""
    gold_rule: str = ""               # catalog | bias
    notes: list[str] = field(default_factory=list)

    def key(self) -> str:
        return "|".join([self.kind, self.model, self.engine, self.context, self.gold_rule])

    def digest(self) -> str:
        return hashlib.sha1(self.key().encode()).hexdigest()[:8]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"key": self.key(), "digest": self.digest()}


def probe_serving(host: str = "jinong_gpu", timeout: float = 60.0) -> dict[str, Any]:
    """원격 서빙이 지금 무엇으로 돌고 있는지 — 교체 뒤 좌표가 바뀌었는지 알아채는 자리."""
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=10", host, "python3", "-"],
                       input=SERVING_PROBE, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"서빙 조사 실패: {r.stderr.strip()[-400:]}")
    return json.loads(r.stdout)


def serving_coordinate(probe: dict[str, Any], gold_rule: str = "catalog") -> Coordinate:
    """조사 결과를 좌표로. `:8102` 가 `--asr-url` 로 도는지가 승격 반영 여부다."""
    procs = probe.get("processes") or []
    diar = next((p for p in procs if "qwen_diar_server" in p), "")
    delegated = "--asr-url" in diar
    model = probe.get("served_model") or "?"
    return Coordinate(
        source="serving :8105 → :8102", kind="serving",
        model=(model if delegated else "Qwen/Qwen3-ASR-1.7B (base, FunASR 레지스트리 고정)"),
        engine=("pyannote turns + :8100 vLLM 위임" if delegated else "pyannote turns + FunASR in-process"),
        context="retriever", catalog_md5=probe.get("catalog_md5", ""), gold_rule=gold_rule,
        notes=[f"diar launch: {diar[:160]}"] if diar else ["diar 프로세스를 못 찾았다"])


def load(path: Path) -> Coordinate | None:
    p = Path(path)
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    return Coordinate(**{k: v for k, v in d.items() if k in Coordinate.__dataclass_fields__})


def save(path: Path, c: Coordinate) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(c.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")


def compare(a: Coordinate, b: Coordinate) -> list[str]:
    """두 좌표가 어디서 다른지 — 비면 나란히 놓아도 된다."""
    diffs = []
    for f in ("kind", "model", "engine", "context", "catalog_md5", "gold_rule"):
        x, y = getattr(a, f), getattr(b, f)
        if x != y:
            diffs.append(f"{f}: {x or '—'} ≠ {y or '—'}")
    return diffs
