# 지농 AI Agent (⑧) — 통화 기반 영농일지·컨설팅 보고서 생성. 모델/GPU 없음(STT·LLM 은 ⑥ 게이트웨이 경유).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    DB_PATH=/data/agent.db

WORKDIR /app

# deps first for layer caching (ffmpeg 불필요 — 게이트웨이가 오디오를 정규화한다)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

VOLUME ["/data"]
EXPOSE 8080

# liveness — no upstream calls
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).status==200 else 1)"

# --workers 1 필수: 워커 폴러/세마포어가 프로세스 로컬
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
