"""SQLite 가 tz 를 버린 naive datetime 이 파이프라인 컨텍스트에서 UTC 로 재태깅되는지."""

from datetime import UTC, datetime

from app.db.models import Call
from app.worker.generate_job import build_context


def test_build_context_retags_naive_as_utc():
    call = Call(call_id="c1", started_at=datetime(2026, 8, 19, 3, 20), ended_at=None,
                participants_json=[{"role": "farmer", "user_id": "u", "name": "n"}],
                metadata_json={"hints": {"prdlst_code": "0804MM", "ignored": 1}}, generation_run=1)
    ctx = build_context(call)
    assert ctx.started_at is not None and ctx.started_at.tzinfo is UTC
    assert ctx.started_at.hour == 3
    assert ctx.hints.prdlst_code == "0804MM"
    assert ctx.ended_at is None
