#!/usr/bin/env bash
# 开发模式：后端 7800（热重载）+ 前端 Vite 5173（/api 代理到后端）
set -euo pipefail
cd "$(dirname "$0")"

PORT="${SYNAPSE_PORT:-7800}"
VITE_PORT="${VITE_PORT:-5173}"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
export SYNAPSE_PORT="$PORT"

if [ ! -x backend/.venv/bin/python ]; then
  echo "▶ 首次运行：创建后端虚拟环境并安装依赖…"
  uv venv --python 3.12 backend/.venv
  uv pip install --python backend/.venv/bin/python -r backend/requirements.txt
fi
if [ ! -d frontend/node_modules ]; then
  (cd frontend && npm install --no-audit --no-fund)
fi

backend/.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port "$PORT" --reload &
BACK_PID=$!
cleanup() { kill "$BACK_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "▶ 后端 http://127.0.0.1:${PORT}  |  前端 http://127.0.0.1:${VITE_PORT}"
cd frontend
VITE_PROXY_TARGET="http://127.0.0.1:${PORT}" npm run dev -- --port "$VITE_PORT"
