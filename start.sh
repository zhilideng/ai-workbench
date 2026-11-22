#!/usr/bin/env bash
# 启动个人工作台：http://127.0.0.1:${WB_PORT:-8765}
cd "$(dirname "$0")"
exec python3 -m uvicorn app:app --host 127.0.0.1 --port "${WB_PORT:-8765}"
