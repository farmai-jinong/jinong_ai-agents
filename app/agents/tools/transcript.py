"""전사 포맷/청크 도구 — 프롬프트용 텍스트, 시간 표기, turn 청크."""

from __future__ import annotations

from ..schemas import Turn

ROLE_KO = {"farmer": "농가", "consultant": "컨설턴트", "unknown": "화자", "other": "제3자"}


def fmt_ts(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def role_label(turn: Turn, n_files: int = 1) -> str:
    if turn.role in ("farmer", "consultant"):
        return ROLE_KO[turn.role]
    if n_files > 1:
        return f"화자{turn.speaker_letter}(파일{turn.file_index + 1})"
    return f"화자{turn.speaker_letter}"


def format_turns(turns: list[Turn], n_files: int = 1) -> str:
    return "\n".join(f"#{t.tid} [{fmt_ts(t.abs_start)}] {role_label(t, n_files)}: {t.text}" for t in turns)


def est_tokens(text: str) -> int:
    # 한국어 대략 1.6자/토큰 (tiktoken 없이 보수적으로)
    return int(len(text) / 1.6) + 1


def chunk_turns(turns: list[Turn], max_tokens: int, overlap: int = 6) -> list[list[Turn]]:
    """turn 경계로 청크. 각 청크는 대략 max_tokens 이하, 앞 청크 마지막 `overlap` turn 을 겹친다."""
    if not turns:
        return []
    chunks: list[list[Turn]] = []
    cur: list[Turn] = []
    cur_tok = 0
    for t in turns:
        tk = est_tokens(t.text) + 8
        if cur and cur_tok + tk > max_tokens:
            chunks.append(cur)
            cur = cur[-overlap:] if overlap > 0 else []
            cur_tok = sum(est_tokens(x.text) + 8 for x in cur)
        cur.append(t)
        cur_tok += tk
    if cur:
        chunks.append(cur)
    return chunks


def excerpt(turns: list[Turn], head: int = 12, mid: int = 6, tail: int = 4) -> list[Turn]:
    if len(turns) <= head + mid + tail:
        return list(turns)
    body = turns[head:-tail] if tail else turns[head:]
    step = max(1, len(body) // (mid + 1))
    picked = [body[i] for i in range(step, len(body), step)][:mid]
    return turns[:head] + picked + (turns[-tail:] if tail else [])
