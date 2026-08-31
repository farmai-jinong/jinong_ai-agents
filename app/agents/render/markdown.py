"""Markdown 렌더 — Jinja2 템플릿 + 필터. 산출물 헤딩은 앱 화면 블록/동의서 §4 와 일치."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ...schemas.pipeline import CallContext
from ..schemas import CropFacts, DiaryResult, NormalizedTranscript, PestFact, ReportNarrative, SpeakerRoleResult, Turn
from ..tools.transcript import ROLE_KO, fmt_ts, role_label

_TPL = Path(__file__).parent / "templates"
KST = ZoneInfo("Asia/Seoul")

WHEN_KO = {"applied": "투입함", "planned": "투입 예정", "recommended": "권고됨", "unknown": "시점 불명",
           "today": "오늘", "ongoing": "계속", "past": "이전", "unknown_fw": "시점 불명"}


def evref(ev: list[int] | None) -> str:
    ev = [e for e in (ev or []) if isinstance(e, int)]
    return f" (근거: {', '.join(f'#{e}' for e in ev)})" if ev else ""


def _names(cands: list[dict[str, Any]]) -> str:
    return ", ".join(str(c.get("name")) for c in cands[:3] if c.get("name"))


def _role_ko(t: Turn) -> str:
    return role_label(t, 99 if t.role == "unknown" else 1) if isinstance(t, Turn) else str(t)


@lru_cache
def env() -> Environment:
    e = Environment(loader=FileSystemLoader(str(_TPL)), autoescape=select_autoescape(default=False),
                    trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    e.filters["evref"] = evref
    e.filters["names"] = _names
    e.filters["ts"] = fmt_ts
    e.filters["role_ko"] = _role_ko
    e.filters["when_ko"] = lambda w: WHEN_KO.get(w, w)
    e.filters["owner_ko"] = lambda o: ROLE_KO.get(o, o)
    return e


def kst(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST).strftime(fmt)


def call_when(ctx: CallContext) -> str:
    a, b = kst(ctx.started_at), kst(ctx.ended_at, "%H:%M")
    if a and b:
        return f"{a} ~ {b}"
    return a or kst(ctx.ended_at) or ""


def cited_turns(transcript: NormalizedTranscript, evidence: list[int], cap: int = 80) -> list[Turn]:
    seen: set[int] = set()
    out: list[Turn] = []
    for e in sorted(x for x in evidence if isinstance(x, int)):
        if e in seen:
            continue
        t = transcript.by_tid(e)
        if t:
            out.append(t)
            seen.add(e)
        if len(out) >= cap:
            break
    return out


def _dedupe_plans(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from rapidfuzz import fuzz

    from ..mapping.matcher import normalize
    kept: list[dict[str, Any]] = []
    for p in plans:
        ev = set(p.get("evidence") or [])
        dup = False
        for k in kept:
            kev = set(k.get("evidence") or [])
            if ev and kev and ev <= kev and fuzz.partial_ratio(normalize(p["text"]), normalize(k["text"])) >= 60:
                dup = True
                break
            if fuzz.ratio(normalize(p["text"]), normalize(k["text"])) >= 80:
                dup = True
                break
        if not dup:
            kept.append(p)
    return kept


def _duration(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}분 {s:02d}초"


# --------------------------------------------------------------------------- diary
def render_diary(d: DiaryResult, ctx: CallContext, transcript: NormalizedTranscript, crop_facts: CropFacts,
                 *, model: str | None, prompt_version: str, now: datetime) -> str:
    template = "diary_empty.md.j2" if d.status in ("EMPTY", "UNRESOLVED_CROP") else "diary.md.j2"
    products = [p for p in crop_facts.products]
    plans: list[dict[str, Any]] = []
    for f in crop_facts.farmworks:
        if f.when == "planned":
            plans.append({"text": f.name + (f" — {f.detail}" if f.detail else ""), "hint": f.date_hint, "evidence": f.evidence})
        # past 작업은 미래 섹션에 넣지 않는다 — 기타 기록사항(diary_content)이 시점을 붙여 서술한다
    for p in crop_facts.products:
        if p.when in ("planned", "recommended"):
            plans.append({"text": f"{p.name} {WHEN_KO[p.when]}" + (f" ({p.target})" if p.target else ""),
                          "hint": None, "evidence": p.evidence})
    for fu in crop_facts.follow_ups:
        plans.append({"text": fu.text, "hint": fu.when_hint, "evidence": fu.evidence})
    for a in crop_facts.actions:
        if a.status in ("agreed", "planned") and a.actor == "farmer":
            plans.append({"text": a.text, "hint": a.due_hint, "evidence": a.evidence})
    plans = _dedupe_plans(plans)
    prevention: list[PestFact] = [p for p in crop_facts.pests if p.status == "예방언급"]
    ev: list[int] = list(d.evidence)
    unverified = [w for w in d.warnings if "근거" in w]
    return env().get_template(template).render(
        d=d, m=d.mapping, call=ctx, call_when=call_when(ctx), products=products, plans=plans,
        prevention=prevention, unverified=unverified, cited=cited_turns(transcript, ev),
        generated_at=kst(now), model=model, prompt_version=prompt_version,
    )


# --------------------------------------------------------------------------- report
def speaker_summary(sr: SpeakerRoleResult | None) -> str:
    if not sr or not sr.files:
        return "미식별"
    parts = []
    conf = 1.0
    for f in sr.files:
        conf = min(conf, f.confidence)
        mp = ", ".join(f"{k}→{ROLE_KO.get(v, v)}" for k, v in f.mapping.items())
        parts.append(f"파일{f.file_index + 1}: {mp}")
    return "; ".join(parts) + f" (신뢰도 {conf:.2f})"


def render_report(n: ReportNarrative, ctx: CallContext, transcript: NormalizedTranscript, *,
                  speaker_roles: SpeakerRoleResult | None, crops: list[str], warnings: list[str],
                  model: str | None, prompt_version: str, now: datetime) -> str:
    ev: list[int] = []
    for sec in (n.farm_status, n.issues, n.advice, n.farmer_actions, n.follow_ups):
        for b in sec:
            ev.extend(b.evidence)
    for a in n.action_items:
        ev.extend(a.evidence)
    return env().get_template("report.md.j2").render(
        n=n, call=ctx, call_when=call_when(ctx),
        call_date=kst(ctx.started_at or ctx.ended_at, "%Y-%m-%d") or "",
        farmer_name=ctx.name_of("farmer"), consultant_name=ctx.name_of("consultant"),
        duration=_duration(transcript.duration_sec), n_files=transcript.n_files,
        speaker_summary=speaker_summary(speaker_roles), crops=crops, warnings=warnings,
        cited=cited_turns(transcript, ev), generated_at=kst(now), model=model, prompt_version=prompt_version,
    )
