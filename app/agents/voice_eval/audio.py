"""케이스 ↔ 녹음 파일 매칭, ffmpeg 변환.

매칭 순서:
  1. 파일명에 케이스 이름이 들어 있으면 그 케이스 (`녹음대본_<case>.m4a`)
  2. `audio_map.json` (케이스 → 파일명) 의 명시적 지정
  3. 남은 파일 ↔ 남은 케이스는 전사 후 대본 유사도로 배정 (`resolve_by_similarity`)
1/2 로 정한 매칭도 전사가 생기면 유사도로 **검증**하고, 어긋나면 경고를 남긴다(파일명 오배치 방어).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from rapidfuzz import fuzz

log = logging.getLogger("voice_eval.audio")

AUDIO_EXT = {".m4a", ".wav", ".ogg", ".mp3", ".flac", ".aac", ".webm"}
GATEWAY_SAFE_EXT = {".wav", ".ogg"}         # 게이트웨이에서 검증된 형식 (그 외는 415 위험 → 변환)
_NONWORD = re.compile(r"[^0-9A-Za-z가-힣]+")


def list_audio(audio_dir: Path) -> list[Path]:
    if not audio_dir.exists():
        return []
    return sorted(p for p in audio_dir.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT
                  and not p.name.startswith("."))


def load_audio_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, str)}


def match_by_name(cases: list[str], files: list[Path], audio_map: dict[str, str]) -> dict[str, Path]:
    """파일명 규칙 + audio_map 으로 정할 수 있는 것만 배정한다."""
    by_name = {p.name: p for p in files}
    out: dict[str, Path] = {}
    used: set[Path] = set()
    for case in cases:
        hit = next((p for p in files if case in p.name and p not in used), None)
        if hit is not None:
            out[case] = hit
            used.add(hit)
    for case, fname in audio_map.items():
        if case in out or case not in cases:
            continue
        p = by_name.get(fname)
        if p is None or p in used:
            if p is None:
                log.warning("audio_map: %s → %s 파일이 %s 에 없음", case, fname, files[0].parent if files else "?")
            continue
        out[case] = p
        used.add(p)
    return out


def similarity(reference: str, hypothesis: str) -> float:
    """대본 ↔ 전사 유사도 0~100. 애드리브·길이 차를 견디도록 token_set_ratio."""
    return fuzz.token_set_ratio(_NONWORD.sub(" ", reference), _NONWORD.sub(" ", hypothesis))


def resolve_by_similarity(pending: dict[str, str], references: dict[str, str]) -> dict[str, str]:
    """{파일키: 전사} × {케이스: 대본} → {파일키: 케이스}. 유사도 내림차순 탐욕 배정(1:1)."""
    pairs = sorted(
        ((similarity(references[c], t), f, c) for f, t in pending.items() for c in references),
        reverse=True,
    )
    out: dict[str, str] = {}
    taken: set[str] = set()
    for _score, f, c in pairs:
        if f in out or c in taken:
            continue
        out[f] = c
        taken.add(c)
    return out


def prepare(src: Path, dest: Path, *, convert: bool = True) -> Path:
    """게이트웨이로 올릴 파일을 만든다 — 기본은 16kHz mono wav 변환(m4a 415 회피).

    변환 결과는 캐시하고, 원본이 더 새로우면 다시 만든다.
    """
    if not convert and src.suffix.lower() in GATEWAY_SAFE_EXT:
        return src
    if not convert:
        log.warning("%s: --no-convert 인데 게이트웨이 미검증 형식(%s) 그대로 전송", src.name, src.suffix)
        return src
    if shutil.which("ffmpeg") is None:
        log.warning("ffmpeg 없음 — %s 를 변환 없이 전송", src.name)
        return src
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "16000", str(dest)]
    subprocess.run(cmd, check=True)
    return dest
