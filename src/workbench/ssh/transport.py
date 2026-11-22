"""持久连接（OpenSSH ControlMaster）：「连接」建立一条主连接，远程任务 / 上传 / 额度查询都复用它；「断开」关闭它。"""
from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import subprocess
from pathlib import Path
from typing import Optional

from workbench.core.settings import get_settings
from workbench.core.shell import child_env
from workbench.ssh import commands as cmds

# 远程用交互式 shell 启动（为了加载 .bashrc 里的 nvm 等 PATH），没有 tty 时 bash 会打这些无害警告
SHELL_NOISE = ("no job control in this shell", "cannot set terminal process group",
               "Inappropriate ioctl for device", "Pseudo-terminal will not be allocated")

_CM_DIR = Path("/tmp") / f"wb-ssh-{os.getuid()}"   # unix socket 路径有长度限制，放在短路径下
_connect_locks: dict[int, asyncio.Lock] = {}


def _cm_tag() -> str:
    # 同一台机器上多个工作台实例（不同数据目录）互不干扰；用稳定哈希，重启后仍能找到已有的主连接
    return hashlib.sha1(str(get_settings().paths.data).encode()).hexdigest()[:6]


def cm_path(c: dict) -> str:
    return str(_CM_DIR / f"{_cm_tag()}-{c['id']}")


def exec_argv(c: dict, remote_cmd: str, tty: bool = False) -> tuple[list[str], dict]:
    """非交互地在服务器上执行 remote_cmd 的本地 ssh 命令及环境变量（已连接时复用主连接）。"""
    argv = ["ssh", "-tt" if tty else "-T", *cmds.auth_opts(c), "-o", "ConnectTimeout=10",
            "-o", f"ControlPath={cm_path(c)}", "-o", "ControlMaster=no",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4", *cmds.args(c), remote_cmd]
    return argv, {**child_env(), **cmds.env(c)}


def is_connected(c: dict) -> bool:
    if not Path(cm_path(c)).exists():
        return False
    r = subprocess.run(["ssh", "-o", f"ControlPath={cm_path(c)}", "-O", "check", *cmds.args(c)],
                       capture_output=True, timeout=5)
    return r.returncode == 0


def clean_stderr(err: str) -> list[str]:
    return [m for m in err.strip().splitlines()
            if not m.startswith(("Warning: Permanently added", "** ")) and not any(n in m for n in SHELL_NOISE)]


async def connect(c: dict) -> None:
    """建立主连接（已连接则直接返回）。失败抛 RuntimeError，消息为 ssh 的报错。"""
    lock = _connect_locks.setdefault(c["id"], asyncio.Lock())
    async with lock:
        if await asyncio.to_thread(is_connected, c):
            return
        _CM_DIR.mkdir(mode=0o700, exist_ok=True)
        Path(cm_path(c)).unlink(missing_ok=True)  # 清掉失效的 socket
        argv = ["ssh", "-M", "-N", "-f", *cmds.auth_opts(c), "-o", "ConnectTimeout=10",
                "-o", f"ControlPath={cm_path(c)}", "-o", "ControlPersist=no",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4", *cmds.args(c)]
        # -f：认证成功后转入后台。后台进程会继承输出，所以 stderr 写到文件而不是管道，否则这里会一直等不到 EOF
        errf = _CM_DIR / f"{_cm_tag()}-{c['id']}.err"
        with open(errf, "wb") as ef:
            proc = await asyncio.create_subprocess_exec(*argv, env={**child_env(), **cmds.env(c)},
                                                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=ef, start_new_session=True)
            try:
                code = await asyncio.wait_for(proc.wait(), 30)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError("连接超时")
        err = errf.read_bytes()
        errf.unlink(missing_ok=True)
        if code != 0 or not await asyncio.to_thread(is_connected, c):
            msg = clean_stderr(err.decode(errors="replace"))
            if any("Host key verification failed" in m for m in msg):
                msg.append("首次连接这台主机，请先在终端里手动连一次并确认主机指纹。")
            raise RuntimeError("\n".join(msg[-3:]) or f"ssh 退出码 {code}")


def disconnect(c: dict) -> None:
    subprocess.run(["ssh", "-o", f"ControlPath={cm_path(c)}", "-O", "exit", *cmds.args(c)],
                   capture_output=True, timeout=10)
    Path(cm_path(c)).unlink(missing_ok=True)


async def run(c: dict, remote_cmd: str, stdin: Optional[bytes] = None, timeout: float = 60) -> tuple[int, str, str]:
    argv, env = exec_argv(c, remote_cmd)
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        raise RuntimeError("SSH 执行超时")
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def upload_images(c: dict, images: list[str]) -> tuple[list[str], str]:
    """把本机图片传到服务器 ~/.workbench/uploads，返回 (服务器上的路径, 服务器上的目录)。"""
    out, home = [], ""
    for i in images:
        name = Path(i).name  # uuid.ext，无需转义
        code, so, se = await run(c, f'mkdir -p {cmds.REMOTE_UPLOADS} && cat > {cmds.REMOTE_UPLOADS}/{name} && printf %s "$HOME"',
                                 Path(i).read_bytes(), 180)
        if code != 0:
            raise RuntimeError(f"上传图片到服务器失败：{se.strip()}")
        home = so.strip()
        out.append(f"{home}/{cmds.REMOTE_UPLOADS}/{name}")
    return out, f"{home}/{cmds.REMOTE_UPLOADS}"


async def kill_remote(c: dict, run_id: int) -> None:
    """停止服务器上的 CLI 进程（连同它启动的子进程）。"""
    try:
        await run(c, cmds.remote_kill_cmd(run_id), timeout=20)
    except Exception:  # noqa: BLE001
        pass
