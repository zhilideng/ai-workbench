"""桌面应用入口：`python -m workbench` 或打包后的 AI Workbench.app。"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

from workbench import APP_NAME, __version__
from workbench.core.settings import configure
from workbench.core.shell import adopt_login_path

log = logging.getLogger("workbench")


def _setup_logging(log_dir: Path, debug: bool) -> None:
    handler = logging.handlers.RotatingFileHandler(log_dir / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    if debug:
        root.addHandler(logging.StreamHandler())
    root.setLevel(logging.DEBUG if debug else logging.INFO)


def _alert(title: str, text: str, buttons: tuple[str, ...] = ("好",)) -> int:
    """原生提示框，返回按下的按钮序号（0 起）。必须在主线程调用。"""
    from AppKit import NSAlert, NSApplication
    NSApplication.sharedApplication()
    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(text)
    for b in buttons:
        a.addButtonWithTitle_(b)
    return int(a.runModal()) - 1000   # NSAlertFirstButtonReturn = 1000


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="workbench", description=f"{APP_NAME} {__version__}")
    p.add_argument("--debug", action="store_true", help="打开 Web 检查器，日志同时输出到终端")
    p.add_argument("--data-dir", type=Path, help="数据目录（默认 ~/Library/Application Support/AIWorkbench）")
    # 从 Finder 启动时 macOS 可能附带 -psn_… 参数，忽略未知参数
    args, _ = p.parse_known_args(argv)
    return args


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    settings = configure(args.data_dir)
    _setup_logging(settings.paths.logs, args.debug)
    log.info("%s %s 启动，数据目录 %s", APP_NAME, __version__, settings.paths.data)

    from workbench.desktop.instance import SingleInstance
    lock = SingleInstance(settings.paths.data / ".lock")
    if not lock.acquire():
        _alert(f"{APP_NAME} 已在运行", "同一个数据目录只能打开一个工作台。")
        return

    adopt_login_path()   # 必须在查找 claude / codex 之前

    import webview
    from webview.menu import Menu, MenuAction, MenuSeparator

    from workbench.api import create_app
    from workbench.desktop.bridge import Bridge
    from workbench.desktop.security import TokenGuard, new_token
    from workbench.desktop.server import EmbeddedServer, preferred_port

    state: dict = {}
    token = new_token()
    app = TokenGuard(create_app(on_runner=lambda r: state.update(runner=r), warm=True), token)
    server = EmbeddedServer(app, port=preferred_port(str(settings.paths.data)))
    server.start()
    log.info("本地服务 %s", server.url)

    bridge = Bridge()
    window = webview.create_window(
        APP_NAME, f"{server.url}/?wb_token={token}", js_api=bridge,
        width=1380, height=900, min_size=(960, 620), background_color="#F4F5F7", text_select=True)
    bridge._attach(window)

    def on_closing():
        runner = state.get("runner")
        n = runner.active_count if runner else 0
        if n and _alert(f"有 {n} 个任务正在执行", "退出会停止这些任务（远程服务器上的进程也会被结束）。",
                        ("退出", "取消")) != 0:
            return False   # 取消关闭
        return True

    window.events.closing += on_closing

    def js(code: str):
        return lambda: window.evaluate_js(code)

    menu = [
        Menu("工作台", [
            MenuAction("新任务", js("window.wb && window.wb.newTask()")),
            MenuAction("切换项目…", js("window.wb && window.wb.switchProject()")),
            MenuSeparator(),
            MenuAction("选择项目目录…", js("window.wb && window.wb.chooseProjectsRoot()")),
            MenuAction("在访达中打开数据目录", lambda: bridge.reveal(str(settings.paths.data))),
            MenuSeparator(),
            MenuAction("重新载入", lambda: window.load_url(f"{server.url}/")),
        ]),
    ]

    try:
        webview.start(debug=args.debug, private_mode=False, storage_path=str(settings.paths.data / "webview"),
                      menu=menu, localization={"global.quitConfirmation": "确定退出？"})
    finally:
        log.info("窗口已关闭，正在停止本地服务")
        server.stop()
        lock.release()


if __name__ == "__main__":
    main()
