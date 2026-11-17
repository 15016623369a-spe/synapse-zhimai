# syntax=docker/dockerfile:1

# ---------- 前端构建 ----------
FROM node:20-bookworm-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------- 运行时 ----------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SYNAPSE_DATA_DIR=/app/data

# PyMuPDF / RapidOCR / onnxruntime 运行所需的系统库
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN python -m pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY --from=web /web/dist frontend/dist
COPY data/demo_docs data/demo_docs
COPY data/demo_questions.md data/demo_questions.md
COPY scripts/reset_demo.py scripts/reset_demo.py

# 非 root 运行；/app/data 是命名卷挂载点，首次启动自动包含 demo 文档
RUN useradd --create-home --shell /usr/sbin/nologin synapse \
    && mkdir -p /app/data \
    && chown -R synapse:synapse /app

USER synapse
WORKDIR /app
EXPOSE 7800

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:7800/api/health', timeout=3)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "7800"]
