"""SSH 密码只存在 macOS 钥匙串里；ssh 通过 SSH_ASKPASS 脚本按连接 id 读取，数据库和命令行里都没有明文。"""
from __future__ import annotations

import subprocess
from pathlib import Path

from workbench.core.errors import BadRequest, WorkbenchError
from workbench.core.settings import get_settings
from workbench.core.shell import IS_MAC


def askpass_path() -> Path:
    return get_settings().paths.bin / "wb-askpass"


def ensure_askpass() -> None:
    """生成 ssh 的 SSH_ASKPASS 程序：按 WB_SSH_ID 从钥匙串取出该连接的密码。"""
    f = askpass_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("#!/bin/sh\n# AI 工作台生成：ssh 询问密码时从 macOS 钥匙串读取（只读这一个连接的条目）\n"
                 f'exec /usr/bin/security find-generic-password -s {get_settings().keychain_service} -a "wb-$WB_SSH_ID" -w\n')
    f.chmod(0o700)


def set_password(cid: int, password: str) -> None:
    if not IS_MAC:
        raise BadRequest("密码登录只支持 macOS（密码保存在钥匙串里）")
    esc = password.replace("\\", "\\\\").replace('"', '\\"')
    service = get_settings().keychain_service
    # 经 stdin 传给 security -i，避免密码出现在进程参数里
    r = subprocess.run(["/usr/bin/security", "-i"], capture_output=True, text=True, timeout=15,
                       input=f'add-generic-password -U -s {service} -a wb-{cid} -l "工作台 SSH #{cid}" -w "{esc}"\n')
    if r.returncode != 0 or "error" in r.stderr.lower():
        raise WorkbenchError(f"保存密码到钥匙串失败：{r.stderr.strip()}")


def has_password(cid: int) -> bool:
    if not IS_MAC:
        return False
    return subprocess.run(["/usr/bin/security", "find-generic-password", "-s", get_settings().keychain_service,
                           "-a", f"wb-{cid}"], capture_output=True, timeout=15).returncode == 0


def delete_password(cid: int) -> None:
    if IS_MAC:
        subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", get_settings().keychain_service,
                        "-a", f"wb-{cid}"], capture_output=True, timeout=15)
