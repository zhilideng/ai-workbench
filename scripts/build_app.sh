#!/usr/bin/env bash
# 打包 macOS 应用：dist/AI Workbench.app
#   ./scripts/build_app.sh            # 打包
#   ./scripts/build_app.sh --install  # 打包并复制到 /Applications
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "创建虚拟环境 .venv"
  python3 -m venv .venv
fi
"$PY" -m pip install -q -e ".[dev]"

[ -f packaging/AppIcon.icns ] || "$PY" scripts/make_icon.py

rm -rf build dist
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath dist --workpath build packaging/AIWorkbench.spec

APP="dist/AI Workbench.app"
# 本地使用：临时签名（ad-hoc），避免 macOS 认为应用已损坏
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "已生成 $APP"

if [ "${1:-}" = "--install" ]; then
  rm -rf "/Applications/AI Workbench.app"
  cp -R "$APP" /Applications/
  echo "已安装到 /Applications/AI Workbench.app"
fi
