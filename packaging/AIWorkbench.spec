# PyInstaller 配置：生成 dist/AI Workbench.app。用 scripts/build_app.sh 调用。
# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT / "src"))
from workbench import APP_ID, APP_NAME, __version__  # noqa: E402

hidden = collect_submodules("uvicorn") + collect_submodules("workbench") + ["webview.platforms.cocoa"]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    datas=[(str(ROOT / "src" / "workbench" / "web"), "workbench/web")],
    hiddenimports=hidden,
    excludes=["tkinter", "webview.platforms.qt", "webview.platforms.gtk", "webview.platforms.winforms",
              "webview.platforms.edgechromium", "webview.platforms.cef", "PyQt5", "PyQt6", "PySide2", "PySide6",
              "numpy", "pandas", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AIWorkbench", console=False,
          argv_emulation=False, target_arch=None, codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="AIWorkbench")
app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon=str(ROOT / "packaging" / "AppIcon.icns"),
    bundle_identifier=APP_ID,
    version=__version__,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,   # 跟随系统深色模式
        # 「在终端中打开」SSH 连接时用 AppleScript 控制「终端」
        "NSAppleEventsUsageDescription": "用于在「终端」中打开 SSH 连接或接管任务会话。",
    },
)
