#!/usr/bin/env bash
# Synapse · 知脉 — 一键启动（构建前端 + 启动 API/Web 单端口服务）
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

PORT="${SYNAPSE_PORT:-7800}"
export SYNAPSE_PORT="$PORT"

if [ ! -x backend/.venv/bin/python ]; then
  echo "▶ 首次运行：创建后端虚拟环境并安装依赖…"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 backend/.venv 2>/dev/null || uv venv backend/.venv
    uv pip install --python backend/.venv/bin/python -r backend/requirements.txt
  else
    python3 -m venv backend/.venv
    backend/.venv/bin/python -m pip install --upgrade pip
    backend/.venv/bin/python -m pip install -r backend/requirements.txt
  fi
fi

if [ ! -f frontend/dist/index.html ] || [ "${SYNAPSE_REBUILD:-0}" = "1" ]; then
  echo "▶ 构建前端…"
  (cd frontend && { [ -d node_modules ] || npm install --no-audit --no-fund; } && npm run build)
fi

echo "▶ Synapse · 知脉已启动 → http://127.0.0.1:${PORT}"
exec backend/.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port "$PORT"
