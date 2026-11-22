#!/usr/bin/env bash
# 打包 macOS 应用：dist/AI Workbench.app
#   ./scripts/build_app.sh            # 打包
#   ./scripts/build_app.sh --install  # 打包、退出正在运行的应用、安装到 /Applications 并重新打开
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
"$PY" -m PyInstaller --noconfirm --clean --log-level ERROR \
  --distpath dist --workpath build packaging/AIWorkbench.spec 2>&1 | grep -v "conda-meta" || true

APP="dist/AI Workbench.app"
[ -d "$APP" ] || { echo "打包失败" >&2; exit 1; }
# 本地使用：临时签名（ad-hoc），避免 macOS 认为应用已损坏
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "已生成 $APP（版本 $("$PY" -c 'import sys; sys.path.insert(0,"src"); import workbench; print(workbench.__version__)')）"

if [ "${1:-}" = "--install" ]; then
  DEST="/Applications/AI Workbench.app"
  # 先正常退出（有任务在执行时应用会弹窗确认），最多等 20 秒
  if pgrep -f "$DEST/Contents/MacOS/AIWorkbench" >/dev/null; then
    osascript -e 'tell application id "com.zhilideng.ai-workbench" to quit' >/dev/null 2>&1 || true
    for _ in $(seq 1 40); do pgrep -f "$DEST/Contents/MacOS/AIWorkbench" >/dev/null || break; sleep 0.5; done
    if pgrep -f "$DEST/Contents/MacOS/AIWorkbench" >/dev/null; then
      echo "应用仍在运行（可能有任务在执行，等待确认退出），未替换。" >&2; exit 1
    fi
  fi
  rm -rf "$DEST"
  cp -R "$APP" /Applications/
  open "$DEST"
  echo "已安装并打开 $DEST"
fi
