"""백엔드(farmos) PostgreSQL **읽기 전용** 클라이언트 — dev 전용(`BACKEND_DB_URL`).

통화 참여자·농가 등록 작물·표준 품목을 통화 시작 payload 스냅샷 대신 백엔드 DB 에서 직접 읽는다.
연결 정보 SSOT: `~/dev/Hatchery_serving/.env`(DB_HOST/PORT/USER/PASSWORD). 지농서버에서는 사설 IP(172.31.1.109)만 열린다.

읽기 전용 보장은 **우리 쪽**에만 있다(계정이 superuser): 세션 `default_transaction_read_only=on` + SQLAlchemy
`postgresql_readonly` 실행 옵션 + 이 파일의 SELECT 만. 스키마는 항상 한정한다(백엔드 search_path 와 무관).
쿼리는 farmos 백엔드 MyBatis 매퍼를 재현한다(`kafka-gateway` `DiaryDAOMapper.xml findUserPrdlstList`,
`CmmDAOMapper.xml findPrdlstCodeList`, `VoiceTalkMapper.xml checkIsConsultant`) — 원본이 바뀌면 여기도 따라간다.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

log = logging.getLogger(__name__)

CONSULTANT_JOB_CODE = "001001004"   # smartfarm.tb_user.user_job_secode — 백엔드 checkIsConsultant 와 동일
FARMER_JOB_CODE = "001001003"


class BackendDbError(Exception):
    """조회 실패(연결·타임아웃·SQL). 호출자는 fail-open — 스냅샷 유지 + 경고."""


def safe_url(url: str) -> str:
    """비밀번호를 뺀 `host:port/db` — 헬스·로그용. 자격증명 부분(`://…@`)을 먼저 통째로 떼어내 특수문자에 흔들리지 않게."""
    try:
        u = urlsplit(re.sub(r"://.*@", "://", url, count=1))
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""
        return f"{host}{port}{u.path or ''}"
    except Exception:  # noqa: BLE001
        return "<invalid-url>"


_SQL_CALL = text("""
SELECT h.call_id, h.status, h.stt_status, h.recording_file_path, h.answer_time, h.end_time, h.duration_seconds, h.updated_at,
       h.sender_engn_id, h.sender_user_id, s.user_nm AS sender_nm, s.user_job_secode AS sender_job,
       h.receiver_engn_id, h.receiver_user_id, r.user_nm AS receiver_nm, r.user_job_secode AS receiver_job
FROM voicetalk.tb_voice_talk_history h
LEFT JOIN smartfarm.tb_user s ON s.user_id = h.sender_user_id AND s.engn_id::text = h.sender_engn_id
LEFT JOIN smartfarm.tb_user r ON r.user_id = h.receiver_user_id AND r.engn_id::text = h.receiver_engn_id
WHERE h.call_id = :call_id
""")

# 백엔드 findUserPrdlstList 재현 — 백엔드는 대표코드까지 group by 라 같은 작물이 2 행 날 수 있어 코드별 1 행(대표 여부 MAX)으로 접는다.
_SQL_FARM_CROPS = text("""
SELECT g.prdlst_code, tsp.mlsfc_code_nm AS prdlst_nm, g.reprsnt_prdlst_cnt
FROM (
    SELECT left(tfp.prdlst, 4) || 'MM' AS prdlst_code,
           MAX(CASE WHEN left(tfp.prdlst, 4) || 'MM' = tf2.reprsnt_prdlst_code THEN 1 ELSE 0 END) AS reprsnt_prdlst_cnt
    FROM smartfarm.tb_user tu
    JOIN smartfarm.tb_frmhs tf  ON tf.user_id = tu.user_id AND tf.engn_id = tu.engn_id AND tf.use_yn = 'Y'
    JOIN smartfarm.tb_frlnd tf2 ON tf2.frmhs_id = tf.frmhs_id AND tf2.user_id = tf.user_id AND tf2.engn_id = tf.engn_id
                               AND tf2.use_yn = 'Y'
    JOIN smartfarm.tb_frlnd_prdlst tfp ON tfp.frlnd_innb = tf2.frlnd_innb AND tfp.frmhs_id = tf2.frmhs_id
                                       AND tfp.user_id = tf2.user_id AND tfp.engn_id = tf2.engn_id
    WHERE tu.use_yn = 'Y' AND tu.user_id = :user_id AND tu.engn_id = :engn_id
    GROUP BY left(tfp.prdlst, 4)
) g
LEFT JOIN smartfarm.tb_stdr_prdlst tsp ON tsp.prdlst_code = g.prdlst_code AND tsp.use_yn = 'Y'
ORDER BY g.reprsnt_prdlst_cnt DESC, g.prdlst_code
""")

_SQL_PRDLSTS = text("""
SELECT prdlst_code, lclas_code_nm, mlsfc_code_nm
FROM smartfarm.tb_stdr_prdlst
WHERE use_yn = 'Y' AND prdlst_code LIKE '%MM'
ORDER BY prdlst_code
""")

_SQL_READONLY = text("SELECT current_setting('transaction_read_only')")


class BackendDbClient:
    PRDLSTS_TTL_S = 3600.0

    def __init__(self, url: str, *, timeout: float = 5.0, pool_size: int = 2) -> None:
        self.url = url
        self.timeout = timeout
        self.engine: AsyncEngine = create_async_engine(
            url, pool_size=pool_size, max_overflow=1, pool_pre_ping=True, pool_recycle=1800,
            connect_args={
                "timeout": timeout,
                "server_settings": {
                    "default_transaction_read_only": "on",
                    "statement_timeout": str(int(timeout * 1000)),
                    "application_name": "jinong-ai-agents",
                },
            },
        )
        self._prdlsts_cache: list[dict[str, Any]] | None = None
        self._prdlsts_at = 0.0

    def safe_url(self) -> str:
        return safe_url(self.url)

    async def _rows(self, stmt, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
        try:
            async with self.engine.connect() as conn:
                conn = await conn.execution_options(postgresql_readonly=True)
                res = await conn.execute(stmt, params or {})
                return [dict(r) for r in res.mappings().all()]
        except Exception as e:  # noqa: BLE001 — 드라이버/네트워크 예외를 한 종류로(비밀 URL 은 메시지에 없다)
            raise BackendDbError(f"{type(e).__name__}: {str(e)[:200]}") from e

    # --- 통화 -------------------------------------------------------------
    async def call(self, call_id: str) -> dict[str, Any] | None:
        """`voicetalk.tb_voice_talk_history` 1 행 + 양측 `tb_user`(이름·업무 구분코드). 없으면 None."""
        rows = await self._rows(_SQL_CALL, {"call_id": call_id})
        return rows[0] if rows else None

    # --- 농가 등록 작물 -----------------------------------------------------
    async def farm_crops(self, engn_id: str, user_id: str) -> list[dict[str, Any]]:
        """farmos `list_crops()` 와 같은 모양 `{prdlstCode, prdlstNm, reprsntPrdlstCnt, use}`. 이름 없는 코드는 버린다."""
        try:
            engn = int(str(engn_id).strip())
        except ValueError as e:
            raise BackendDbError(f"engn_id must be int-like: {engn_id!r}") from e
        rows = await self._rows(_SQL_FARM_CROPS, {"engn_id": engn, "user_id": str(user_id)})
        out: list[dict[str, Any]] = []
        for r in rows:
            if not r.get("prdlst_nm"):
                continue
            out.append({"prdlstCode": r["prdlst_code"], "prdlstNm": str(r["prdlst_nm"]),
                        "reprsntPrdlstCnt": int(r.get("reprsnt_prdlst_cnt") or 0), "use": True})
        return out

    async def farm_context(self, engn_id: str, user_id: str) -> list[dict[str, Any]]:
        """`ApBackendLike.farm_context` 와 같은 시그니처(대체 주입용)."""
        return await self.farm_crops(engn_id, user_id)

    # --- 표준 품목 ----------------------------------------------------------
    async def prdlsts(self) -> list[dict[str, Any]]:
        """`ApBackendClient.prdlsts()` 와 같은 모양·필터(M 그룹, use_yn=Y, 종자류 제외). 프로세스 내 1 시간 캐시."""
        now = time.monotonic()
        if self._prdlsts_cache is not None and now - self._prdlsts_at < self.PRDLSTS_TTL_S:
            return self._prdlsts_cache
        rows = await self._rows(_SQL_PRDLSTS)
        out: list[dict[str, Any]] = []
        for r in rows:
            nm = r.get("mlsfc_code_nm")
            if not nm or not r.get("prdlst_code") or "종자" in str(r.get("lclas_code_nm") or ""):
                continue
            out.append({"prdlstCode": str(r["prdlst_code"]).strip(), "prdlstNm": str(nm)})
        self._prdlsts_cache, self._prdlsts_at = out, now
        return out

    # --- 헬스 ---------------------------------------------------------------
    async def probe(self) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            rows = await self._rows(_SQL_READONLY)
            ro = bool(rows) and str(rows[0].get("current_setting")) == "on"
            return {"ok": ro, "read_only": ro, "latency_ms": round((time.monotonic() - t0) * 1000),
                    "url": self.safe_url()}
        except BackendDbError as e:
            return {"ok": False, "read_only": None, "error": str(e), "url": self.safe_url()}

    async def close(self) -> None:
        await self.engine.dispose()
