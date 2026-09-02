"""테스트케이스 로딩 — 대본 파싱, 오디오 매칭, source.json → CallContext.

케이스 디렉터리(`tests/agents/testcases/voice/<case>/`) 구성은 그 README 가 SSOT:
  script.md          녹음 대본 (머리말 표 → `---` → 발화 → `---` → 검증 포인트 매핑)
  expected_diary.md  정답 일지 (문자열 정답이 아니라 채점 기준 — LLM judge 용)
  expect.json        기대 추출 (app/agents/eval.py 와 동일 스키마 + 선택 키 stt_keywords)
  source.json        원본 팜스올 일지(verbatim) + 보강 항목
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ...schemas.pipeline import CallContext, CallHints, Participant

TESTCASES = Path(__file__).resolve().parents[3] / "tests" / "agents" / "testcases" / "voice"
KST = timezone(timedelta(hours=9))

# 대본 발화 줄: **농장주:** …  /  **컨설턴트:** …
_UTTERANCE = re.compile(r"^\*\*(?P<who>[^:*]+)\s*:\*\*\s*(?P<text>.+?)\s*$")
_SPEAKER_ROLE = {"농장주": "farmer", "농가": "farmer", "컨설턴트": "consultant", "상담사": "consultant"}


@dataclass
class Utterance:
    who: str                 # 대본 표기 그대로 ("농장주")
    role: str                # farmer | consultant | unknown
    text: str


@dataclass
class VoiceCase:
    name: str
    dir: Path
    script_md: str
    expected_diary: str
    expect: dict[str, Any]
    source: dict[str, Any]
    utterances: list[Utterance] = field(default_factory=list)
    audio: Path | None = None

    # ------------------------------------------------------------------ 파생
    @property
    def origin(self) -> dict[str, Any]:
        """원본 일지 필드 — `enriched` 를 위에 덮는다.

        tomato_harvest 는 인접 일지 2건 합성이라 `original` 대신 `original_merged_from`(리스트)을 쓰고
        확정 날짜·작물은 `enriched` 에만 있다(케이스 README 의 보강 규칙).
        """
        base: dict[str, Any] = dict(self.source.get("original") or {})
        if not base:
            merged = self.source.get("original_merged_from") or []
            if merged:
                base = dict(merged[0])
        base.update({k: v for k, v in (self.source.get("enriched") or {}).items() if v is not None})
        return base

    @property
    def reference_text(self) -> str:
        """STT 채점 기준 — 발화 본문만 이어붙인 것."""
        return " ".join(u.text for u in self.utterances)

    @property
    def first_role(self) -> str:
        return self.utterances[0].role if self.utterances else "unknown"

    @property
    def prdlst_code(self) -> str | None:
        return self.origin.get("prdlst_code")

    @property
    def diary_date(self) -> str | None:
        return self.origin.get("diary_date")

    @property
    def crop_name(self) -> str:
        """expect.json 의 diary_status 키(=prdlstCode)와 짝이 되는 작물명은 정답 일지의 `| 작물 | 이름 (코드) |` 표 행에 있다
        (구형 정답의 `# 영농일지 — 이름` 제목도 폴백으로 읽는다)."""
        m = re.search(r"^\|\s*작물\s*\|\s*([^\(|\n]+)", self.expected_diary, re.M) \
            or re.search(r"^#\s*영농일지\s*—\s*([^\(\n]+)", self.expected_diary, re.M)
        return (m.group(1).strip() if m else "") or "작물"

    @property
    def stt_keywords(self) -> list[str]:
        """핵심어 = expect.json 의 products + pests 이름 + 선택 키 stt_keywords (중복 제거, 순서 보존)."""
        out: list[str] = []
        for p in self.expect.get("products", []):
            out.append(p)
        for p in self.expect.get("pests", []):
            out.append(p[0] if isinstance(p, list) else p)
        out.extend(self.expect.get("stt_keywords", []))
        seen: set[str] = set()
        return [k for k in out if k and not (k in seen or seen.add(k))]

    def call_id(self) -> str:
        return f"voice-{self.name.replace('_', '-')}"

    def context(self, *, with_farmos: bool) -> CallContext:
        """원본 일지 날짜를 통화 시각으로 삼는다 — 생육단계·정식일 비교가 의미를 갖도록."""
        orig = self.origin
        d = self.diary_date
        started = datetime.strptime(d, "%Y-%m-%d").replace(hour=10, tzinfo=KST) if d else None
        return CallContext(
            call_id=self.call_id(),
            started_at=started,
            ended_at=started + timedelta(minutes=5) if started else None,
            participants=[
                Participant(role="farmer", user_id=str(orig.get("engn_id") or "f001"), name=orig.get("user_id")),
                Participant(role="consultant", user_id="c001", name="컨설턴트"),
            ],
            farm={"farm_nm": f"{orig.get('user_id') or ''}농장".strip()},
            farm_access_token="fixture" if with_farmos else None,
            hints=CallHints(prdlst_code=self.prdlst_code, prdlst_nm=self.crop_name),
        )


# --------------------------------------------------------------------------- 대본 파싱
def parse_script(md: str) -> list[Utterance]:
    """발화 블록만 뽑는다 — 머리말 표와 하단 `## 검증 포인트 매핑` 은 기준 텍스트가 아니다.

    구조가 `머리말 --- 발화 --- 검증표` 라서 두 번째 `---` 이후는 버리면 되지만, 대본마다 구분선 개수가
    다를 수 있어 `**화자:**` 패턴 + `##` 헤딩 종료로 방어적으로 자른다.
    """
    out: list[Utterance] = []
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("## ") and out:
            break                                   # 발화 블록 뒤의 검증표 시작
        m = _UTTERANCE.match(s)
        if not m:
            continue
        who = m.group("who").strip()
        text = m.group("text").strip()
        if not text or "|" in text:                 # 표 안의 굵은 글씨 방어
            continue
        out.append(Utterance(who=who, role=_SPEAKER_ROLE.get(who, "unknown"), text=text))
    return out


# --------------------------------------------------------------------------- 로딩
def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def load_case(name: str, root: Path = TESTCASES) -> VoiceCase:
    d = root / name
    script = _read(d / "script.md")
    return VoiceCase(
        name=name, dir=d, script_md=script,
        expected_diary=_read(d / "expected_diary.md"),
        expect=json.loads(_read(d / "expect.json")),
        source=json.loads(_read(d / "source.json")),
        utterances=parse_script(script),
    )


def case_names(root: Path = TESTCASES) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "script.md").exists())


def load_cases(names: list[str] | None = None, root: Path = TESTCASES) -> list[VoiceCase]:
    return [load_case(n, root) for n in (names or case_names(root))]
