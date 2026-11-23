"""应用配置：数据目录、项目根目录、并发上限等。

优先级：环境变量 > 数据目录下的 settings.json（应用内可修改）> 默认值。
数据默认放在 ~/Library/Application Support/AIWorkbench，与安装位置无关。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_DATA_DIR = Path.home() / "Library" / "Application Support" / "AIWorkbench"


@dataclass(frozen=True)
class Paths:
    data: Path

    @property
    def db(self) -> Path:
        return self.data / "workbench.db"

    @property
    def runs(self) -> Path:
        return self.data / "runs"

    @property
    def uploads(self) -> Path:
        return self.data / "uploads"

    @property
    def worktrees(self) -> Path:
        return self.data / "worktrees"

    @property
    def bin(self) -> Path:
        return self.data / "bin"

    @property
    def settings_file(self) -> Path:
        return self.data / "settings.json"

    @property
    def logs(self) -> Path:
        return self.data / "logs"

    def ensure(self) -> None:
        for d in (self.data, self.runs, self.uploads, self.worktrees, self.bin, self.logs):
            d.mkdir(parents=True, exist_ok=True)


class Settings:
    """进程内单例。可变项（如项目根目录）持久化到 settings.json。"""

    def __init__(self, data_dir: Optional[Path] = None):
        self.paths = Paths(Path(data_dir or os.environ.get("WB_DATA_DIR") or DEFAULT_DATA_DIR).expanduser().resolve())
        self.paths.ensure()
        self._lock = threading.Lock()
        self._store: dict[str, Any] = self._load()

    # ---- 持久化
    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.paths.settings_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        tmp = self.paths.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._store, ensure_ascii=False, indent=2))
        tmp.replace(self.paths.settings_file)

    def update(self, **values: Any) -> None:
        with self._lock:
            self._store.update(values)
            self._save()

    def recent_folder(self, ssh_id: int) -> str:
        return self._store.get("recent_folders", {}).get(str(ssh_id), "")

    def remember_folder(self, ssh_id: int, path: str) -> None:
        with self._lock:
            self._store.setdefault("recent_folders", {})[str(ssh_id)] = path
            self._save()

    # ---- 配置项
    @property
    def projects_root(self) -> Optional[Path]:
        """项目根目录：其下每个子目录是一个项目。未设置时返回 None（界面会提示选择）。"""
        raw = os.environ.get("WB_PROJECTS_ROOT") or self._store.get("projects_root")
        if not raw:
            return None
        p = Path(raw).expanduser().resolve()
        return p if p.is_dir() else None

    @property
    def projects_root_locked(self) -> bool:
        """由环境变量指定时，应用内不能修改。"""
        return bool(os.environ.get("WB_PROJECTS_ROOT"))

    @property
    def max_concurrent(self) -> int:
        return int(os.environ.get("WB_MAX_CONCURRENT") or self._store.get("max_concurrent") or 4)

    @property
    def keychain_service(self) -> str:
        return os.environ.get("WB_KEYCHAIN_SERVICE", "workbench-ssh")


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def configure(data_dir: Optional[Path] = None) -> Settings:
    """显式初始化（测试或自定义数据目录时用）。"""
    global _settings
    _settings = Settings(data_dir)
    return _settings
