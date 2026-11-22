"""子进程相关的工具：查找可执行文件、补全 GUI 应用缺失的 PATH、清理子进程环境、git。"""
from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

IS_MAC = os.uname().sysname == "Darwin"

_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"))


def adopt_login_path(timeout: float = 8) -> None:
    """从 Finder / Dock 启动的应用只有极简 PATH（/usr/bin:/bin…），找不到 node、claude、codex。
    启动时向用户的登录 shell 要一次 PATH 并合并进来，效果与在终端里启动一致。"""
    shell = os.environ.get("SHELL") or "/bin/zsh"
    marker = "__WB_PATH__"
    try:
        r = subprocess.run([shell, "-l", "-i", "-c", f'printf "{marker}%s{marker}" "$PATH"'],
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        login = r.stdout.split(marker)[1] if r.stdout.count(marker) >= 2 else ""
    except (OSError, subprocess.SubprocessError):
        login = ""
    parts: list[str] = []
    for p in login.split(":") + os.environ.get("PATH", "").split(":") + list(_EXTRA_BIN_DIRS):
        if p and p not in parts:
            parts.append(p)
    os.environ["PATH"] = ":".join(parts)
    find_bin.cache_clear()


@lru_cache(maxsize=None)
def find_bin(name: str) -> str:
    """优先环境变量 <NAME>_BIN / PATH；找不到时再搜 nvm 各 Node 版本下的全局包和常见目录。"""
    found = os.environ.get(f"{name.upper()}_BIN") or shutil.which(name)
    if found:
        return found
    cands = sorted(Path.home().glob(f".nvm/versions/node/*/bin/{name}"), reverse=True)
    cands += [Path(p) / name for p in _EXTRA_BIN_DIRS]
    return next((str(c) for c in cands if c.is_file()), name)


def bin_available(path: str) -> bool:
    return bool(shutil.which(path) or Path(path).is_file())


def child_env() -> dict:
    """子进程环境：清掉从 Claude Code 会话继承来的内部变量（如 CLAUDE_CODE_SIMPLE 会让子进程跳过 OAuth 登录），
    让 claude / codex 按普通 CLI 运行。"""
    env = dict(os.environ)
    keep = {"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_MAX_OUTPUT_TOKENS"}
    for k in list(env):
        if k in keep:
            continue
        if k in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT") or k.startswith(
                ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_", "CLAUDE_PREVIEW_")):
            env.pop(k)
    return env


def git(cwd: Path, *args: str, timeout: float = 5) -> str:
    """执行 git，成功返回 stdout（去首尾空白），失败返回空串。"""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""
