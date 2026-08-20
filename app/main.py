"""지농 AI Agent (⑧) — FastAPI 앱 조립.

전화 시작/녹음 수신/종료 이벤트를 받아 ⑥ 게이트웨이 STT → LangGraph 파이프라인으로 영농일지·컨설팅
보고서 초안을 만든다. 모델/GPU 없음. 단일 프로세스(--workers 1) 전제: 워커 세마포어·폴러가 프로세스 로컬.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from . import __version__
from .config import Settings, get_settings
from .errors import install_error_handlers
from .routes import calls, health
from .runtime import Runtime

if TYPE_CHECKING:  # pragma: no cover
    from .agents.interface import DiaryReportPipeline


def create_app(settings: Settings | None = None, *, pipeline: "DiaryReportPipeline | None" = None,
               start_worker: bool = True) -> FastAPI:
    s = settings or get_settings()
    logging.basicConfig(level=getattr(logging, s.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # fail-closed: 키 없이 실수로 뜨지 않게 (deploy.sh 가 빈 .env.example 을 복사하는 경우 등)
    if not s.auth_enabled and not s.allow_no_auth:
        raise RuntimeError(
            "AGENT_API_KEY is empty — refusing to start unauthenticated. "
            "Set it in .env (or ALLOW_NO_AUTH=1 for local dev only)."
        )

    rt = Runtime.build(s, pipeline=pipeline)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await rt.startup(start_worker=start_worker)
        yield
        await rt.shutdown()

    app = FastAPI(title="지농 AI Agent", version=__version__, lifespan=lifespan)
    app.state.rt = rt
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(calls.router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, log_level=s.log_level, workers=1)


if __name__ == "__main__":
    main()
