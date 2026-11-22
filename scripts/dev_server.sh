#!/usr/bin/env bash
# 开发调试：不开桌面窗口，只在浏览器里调前端（http://127.0.0.1:8765，无访问令牌）。
# 数据目录用 WB_DATA_DIR 指定，默认 ./.devdata，避免改动应用的真实数据。
cd "$(dirname "$0")/.."
export WB_DATA_DIR="${WB_DATA_DIR:-$PWD/.devdata}"
exec .venv/bin/python -m uvicorn --app-dir src --factory workbench.api:create_app \
  --host 127.0.0.1 --port "${WB_PORT:-8765}" --reload --reload-dir src
