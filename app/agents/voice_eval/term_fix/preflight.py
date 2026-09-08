"""사전점검 — 하네스 한 바퀴를 돌릴 자원이 실제로 있는가.

    python -m app.agents.voice_eval.term_fix.preflight [--calls 421] [--host jinong_gpu] [--no-llm]

한 바퀴 = ①(필요시) 원격 base/서빙 전사 ②반입 ③LLM 후보정 ④처방 비교. 각 단계가 요구하는 자원을
**추정이 아니라 실측 단가**로 환산해 확인한다(단가는 2026-09-08 421통화 실측: 통화당 in 21.8k tok · 2.7초).

확인 항목
  원격  GPU 여유(디코드용) · 디스크 여유 · 서빙 4종 헬스(:8100/:8102/:8104/:8105) · 카탈로그 존재
  로컬  디스크 여유 · 카탈로그 경로 · LLM 자격증명과 실제 1콜(--no-llm 로 생략)
  환산  통화 수 → 입력 토큰 · LLM 벽시계 · 산출물 용량

종료코드 0=진행 가능, 1=경고만, 2=막힘.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ....config import Settings
from ...term_fix.catalog import GAPS_PATH
from . import coordinate as coord

# 2026-09-08 실측 단가 (out/tf-p1-{eval,train}-all/summary.json 421통화 합산)
TOK_PER_CALL = 21_800
SEC_PER_CALL = 2.7
OUT_MB_PER_CALL = 0.75
GPU_DECODE_GIB = 12          # base 디코드 1랭크가 잡는 여유(모델 4GiB + 활성화·버퍼)
DISK_GIB_PER_HOUR_AUDIO = 0.3

REMOTE = r'''
import json, os, shutil, subprocess, sys, urllib.request
B = os.environ.get("JINONG_GPU_BASE", "/NHNHOME/WORKSPACE/26mafra001_A/BASE/jinong")
out = {"base": B}
try:
    q = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used,memory.total,utilization.gpu",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    gpus = []
    for line in q.strip().splitlines():
        i, used, total, util = [x.strip() for x in line.split(",")]
        gpus.append({"index": int(i), "free_gib": round((int(total) - int(used)) / 1024, 1),
                     "util": int(util)})
    out["gpus"] = gpus
except Exception as e:
    out["gpus_error"] = str(e)
try:
    du = shutil.disk_usage(B)
    out["disk_free_gib"] = round(du.free / 1024**3, 1)
except Exception as e:
    out["disk_error"] = str(e)
health = {}
for name, url in (("8100 asr", "http://127.0.0.1:8100/v1/models"),
                  ("8102 diar", "http://127.0.0.1:8102/v1/models"),
                  ("8104 pyannote", "http://127.0.0.1:8104/v1/models"),
                  ("8105 ctx", "http://127.0.0.1:8105/health")):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            health[name] = r.status
    except Exception as e:
        health[name] = f"실패: {type(e).__name__}"
out["health"] = health
cat = os.path.join(B, "stt-serve/catalog/catalog.jsonl")
out["catalog"] = {"path": cat, "exists": os.path.exists(cat),
                  "mb": round(os.path.getsize(cat) / 1024**2, 1) if os.path.exists(cat) else 0}
json.dump(out, sys.stdout, ensure_ascii=False)
'''


def remote_probe(host: str, timeout: float) -> dict:
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=10", host, "python3", "-"],
                       input=REMOTE, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"원격 조사 실패(rc={r.returncode}): {r.stderr.strip()[-300:]}")
    return json.loads(r.stdout)


def llm_smoke(settings: Settings) -> tuple[bool, str]:
    """자격증명·할당량을 실제 1콜로 확인한다 — 421통화를 태우기 전에 알아야 할 유일한 것."""
    try:
        from ....clients.llm import make_chat_model
        if settings.llm_provider == "gemini":
            settings.gcp_project_id = os.environ.get("GCP_PROJECT_ID") or settings.gcp_project_id or "jinong-lab-llm"
        llm = make_chat_model(settings)
        t0 = time.perf_counter()
        res = llm.invoke("한 단어로만 답하라: 딸기")
        return True, f"{settings.llm_provider}/{settings.llm_model} 응답 {time.perf_counter() - t0:.1f}초"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"[:220]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.agents.voice_eval.term_fix.preflight",
                                description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--calls", type=int, default=421, help="한 바퀴에 돌릴 통화 수")
    p.add_argument("--host", default="jinong_gpu")
    p.add_argument("--decode", action="store_true", help="원격 전사도 새로 뜬다고 가정(GPU 요구 포함)")
    p.add_argument("--no-llm", action="store_true", help="LLM 실호출 생략")
    p.add_argument("--timeout", type=float, default=90.0)
    a = p.parse_args(argv)

    blocked: list[str] = []
    warned: list[str] = []
    print(f"== 사전점검 · 통화 {a.calls}개 한 바퀴 ==\n")

    # ---------------- 원격
    try:
        r = remote_probe(a.host, a.timeout)
    except Exception as e:  # noqa: BLE001
        print(f"[원격] 조사 실패 — {e}")
        blocked.append("원격 접속")
        r = {}
    if r:
        gpus = r.get("gpus") or []
        free = sorted((g["free_gib"] for g in gpus), reverse=True)
        print("[원격 GPU]  " + " · ".join(f"#{g['index']} 여유 {g['free_gib']}GiB/util {g['util']}%" for g in gpus))
        if a.decode:
            usable = [f for f in free if f >= GPU_DECODE_GIB]
            print(f"            디코드에 필요한 여유 {GPU_DECODE_GIB}GiB 이상: {len(usable)}장")
            if not usable:
                blocked.append(f"디코드용 GPU 없음(여유 {GPU_DECODE_GIB}GiB 이상 0장)")
        disk = r.get("disk_free_gib")
        print(f"[원격 디스크] 여유 {disk}GiB")
        if disk is not None and disk < 50:
            blocked.append(f"원격 디스크 여유 부족({disk}GiB)")
        h = r.get("health") or {}
        print("[서빙]      " + " · ".join(f"{k}={v}" for k, v in h.items()))
        bad = [k for k, v in h.items() if v != 200]
        if bad:
            (blocked if "8105 ctx" in bad or "8100 asr" in bad else warned).append(f"서빙 이상: {', '.join(bad)}")
        c = r.get("catalog") or {}
        print(f"[카탈로그]  원격 {c.get('mb')}MB · 존재={c.get('exists')}")
        if not c.get("exists"):
            blocked.append("원격 카탈로그 없음")

    # ---------------- 좌표
    try:
        cc = coord.serving_coordinate(coord.probe_serving(a.host, a.timeout))
        print(f"\n[좌표]      {cc.digest()} · 모델 {Path(cc.model).name} · 엔진 {cc.engine}")
        if "base" in cc.model:
            warned.append("서빙 diar 이 아직 base 다 — 승격 반영 전 좌표")
    except Exception as e:  # noqa: BLE001
        warned.append(f"좌표 조사 실패: {type(e).__name__}")

    # ---------------- 로컬
    s = Settings()
    du = shutil.disk_usage(".")
    print(f"\n[로컬 디스크] 여유 {du.free / 1024**3:.0f}GiB · 산출물 예상 {a.calls * OUT_MB_PER_CALL:.0f}MB")
    if du.free / 1024**3 < 5:
        blocked.append("로컬 디스크 여유 부족")
    cat_local = Path(s.term_fix_catalog_path) if s.term_fix_catalog_path else \
        Path.home() / "dev/jinong/jinong_gpu/stt-serve/catalog/catalog.jsonl"
    print(f"[로컬 카탈로그] {cat_local} · 존재={cat_local.exists()}")
    if not cat_local.exists():
        blocked.append("로컬 카탈로그 경로 없음(TERM_CATALOG_PATH)")
    print(f"[구멍 목록]  {GAPS_PATH.name} · 존재={GAPS_PATH.exists()}")

    if a.no_llm:
        print("[LLM]       실호출 생략(--no-llm)")
    else:
        ok, msg = llm_smoke(s)
        print(f"[LLM]       {'정상' if ok else '실패'} — {msg}")
        if not ok:
            blocked.append("LLM 호출 불가")

    # ---------------- 환산
    tok = a.calls * TOK_PER_CALL
    print(f"\n[한 바퀴 환산] 입력 토큰 {tok / 1e6:.1f}M · 출력 ~{a.calls * 260 / 1e3:.0f}k"
          f" · LLM 벽시계 {a.calls * SEC_PER_CALL / 60:.0f}분(순차)")
    if a.decode:
        print(f"               + 원격 전사: GPU 2장 기준 발화 12,596 ≈ 40분")
    print("               처방 비교(tune)는 LLM 0콜 — 캐시 위에서 초 단위")

    print()
    for w in warned:
        print(f"  ⚠︎ {w}")
    for b in blocked:
        print(f"  ✖ {b}")
    if blocked:
        print("\n=> 막힘. 위 항목을 풀고 다시.")
        return 2
    print("\n=> 진행 가능." + (" (경고 있음)" if warned else ""))
    return 1 if warned else 0


if __name__ == "__main__":
    sys.exit(main())
