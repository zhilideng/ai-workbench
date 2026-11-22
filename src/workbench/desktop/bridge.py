"""暴露给前端的原生能力（window.pywebview.api.*）。只放需要 macOS 原生界面 / 系统调用的功能，
业务数据仍然走 HTTP API。"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import webview

from workbench.core.errors import WorkbenchError
from workbench.core.settings import get_settings
from workbench.services import tasks


class Bridge:
    def __init__(self):
        self._window: Optional[webview.Window] = None

    def _attach(self, window: webview.Window) -> None:
        self._window = window

    def choose_directory(self, title: str = "选择项目目录") -> Optional[str]:
        """原生「选择文件夹」对话框；取消返回 None。"""
        if not self._window:
            return None
        start = str(get_settings().projects_root or Path.home())
        res = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        return res[0] if res else None

    def open_task_file(self, task_id: int, path: str) -> dict:
        """用系统默认应用打开 AI 回复里引用的本地文件（与 /api/tasks/:id/file 相同的访问限制）。"""
        try:
            f = tasks.resolve_file(int(task_id), path)
        except WorkbenchError as e:
            return {"ok": False, "error": e.message}
        subprocess.run(["open", str(f)], check=False)
        return {"ok": True}

    def reveal(self, path: str) -> dict:
        """在访达中显示目录（仅限数据目录和项目目录内）。"""
        p = Path(path).expanduser().resolve()
        roots = [get_settings().paths.data] + ([get_settings().projects_root] if get_settings().projects_root else [])
        if not p.exists() or not any(p == r or p.is_relative_to(r) for r in roots):
            return {"ok": False, "error": "路径不存在或不允许打开"}
        subprocess.run(["open", "-R", str(p)] if p.is_file() else ["open", str(p)], check=False)
        return {"ok": True}
