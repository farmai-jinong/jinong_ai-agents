"""프로세스 런타임 묶음 — settings/db/s3/stt/pipeline/worker 를 한 객체로 (app.state.rt).

라우트·워커·테스트가 같은 진입점을 쓰도록 하고, 테스트에서는 필드를 바꿔 끼운다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .clients.storage import StorageClient, build_storage
from .clients.stt import SttClient
from .config import Settings
from .db.engine import Database

if TYPE_CHECKING:  # pragma: no cover
    from .agents.interface import DiaryReportPipeline
    from .worker.runner import Worker


@dataclass
class Runtime:
    settings: Settings
    db: Database
    s3: StorageClient
    stt: SttClient
    pipeline: "DiaryReportPipeline"
    worker: "Worker | None" = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(cls, settings: Settings, *, pipeline: "DiaryReportPipeline | None" = None) -> "Runtime":
        from .agents import build_pipeline

        return cls(
            settings=settings,
            db=Database(settings.db_url),
            s3=build_storage(settings),
            stt=SttClient(settings),
            pipeline=pipeline or build_pipeline(settings),
        )

    async def startup(self, *, start_worker: bool = True) -> None:
        from .worker.runner import Worker

        await self.db.init()
        await self.stt.startup()
        self.worker = Worker(self)
        if start_worker:
            await self.worker.start()

    async def shutdown(self) -> None:
        if self.worker is not None:
            await self.worker.stop()
        await self.stt.shutdown()
        await self.db.close()
