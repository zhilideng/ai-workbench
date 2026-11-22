"""项目的 SSH 连接：校验、增删改查、测试、连接 / 断开、在终端打开、给 AI 的说明。"""
from __future__ import annotations

import asyncio
import re
import shlex
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from workbench.core.errors import BadRequest, Conflict, NotFound, UpstreamError, WorkbenchError
from workbench.core.shell import IS_MAC
from workbench.db.repositories import SshRepo, TaskRepo
from workbench.services.projects import project_path
from workbench.ssh import commands as cmds
from workbench.ssh import keychain, transport

if TYPE_CHECKING:
    from workbench.services.runner import Runner

HOST_RE = re.compile(r"^[A-Za-z0-9._:%\-\[\]]{1,253}$")
USER_RE = re.compile(r"^[A-Za-z0-9._\-]{0,64}$")
JUMP_RE = re.compile(r"^[A-Za-z0-9@._:,%\-\[\]]{0,512}$")
SOCKS_RE = re.compile(r"^(?:[A-Za-z0-9.\-]{1,253}|\[[0-9A-Fa-f:.]+\]):\d{1,5}$")
# 改了这些字段，旧的主连接作废
_CONN_KEYS = ("host", "port", "user", "identity_file", "proxy_jump", "socks_proxy", "auth")


# ---------------------------------------------------------------- 校验

def validate(raw: dict[str, Any]) -> tuple[dict, Optional[str]]:
    """校验并规范化表单；返回 (入库字段, 新密码或 None)。密码不入库。"""
    raw = dict(raw)
    password = raw.pop("password", None) or None
    if raw.get("auth") not in ("key", "password"):
        raise BadRequest("认证方式只能是 key 或 password")
    if password is not None and (len(password) > 1024 or "\n" in password or "\r" in password):
        raise BadRequest("密码格式无效")
    if raw["auth"] == "password" and not IS_MAC:
        raise BadRequest("密码登录只支持 macOS（密码保存在钥匙串里）")
    d = {k: v.strip() if isinstance(v, str) else v for k, v in raw.items()}
    if d["auth"] == "password":
        d["identity_file"] = ""
    if not d["name"] or len(d["name"]) > 60:
        raise BadRequest("名称不能为空，且不超过 60 个字符")
    if not HOST_RE.match(d["host"]) or d["host"].startswith("-"):
        raise BadRequest("主机格式无效（填主机名、IP 或 ~/.ssh/config 里的 Host 别名）")
    if not USER_RE.match(d["user"]) or d["user"].startswith("-"):
        raise BadRequest("用户名格式无效")
    if not (0 <= d["port"] <= 65535):
        raise BadRequest("端口应在 1–65535 之间（留空用默认）")
    if not JUMP_RE.match(d["proxy_jump"]) or d["proxy_jump"].startswith("-"):
        raise BadRequest("跳板机格式无效（如 user@bastion:22，多个用逗号分隔）")
    sp = d["socks_proxy"]
    for pre in ("socks5h://", "socks5://"):
        sp = sp[len(pre):] if sp.lower().startswith(pre) else sp
    if sp and (not SOCKS_RE.match(sp) or not 0 < int(sp.rsplit(":", 1)[1]) <= 65535):
        raise BadRequest("SOCKS5 代理格式无效，应为 主机:端口（如 127.0.0.1:1080）")
    if sp and d["proxy_jump"]:
        raise BadRequest("跳板机和 SOCKS5 代理只能二选一")
    d["socks_proxy"] = sp
    if d["identity_file"]:
        f = Path(d["identity_file"]).expanduser()
        if not f.is_file():
            raise BadRequest(f"密钥文件不存在：{d['identity_file']}")
        d["identity_file"] = str(f)
    if "\n" in d["remote_dir"] or len(d["remote_dir"]) > 500:
        raise BadRequest("远程目录格式无效")
    for k in ("remote_claude", "remote_codex"):
        if "\n" in d[k] or len(d[k]) > 500:
            raise BadRequest("CLI 路径格式无效")
    d["note"] = d["note"][:500]
    d["share_with_ai"] = int(d["share_with_ai"])
    d["allow_root_bypass"] = int(d["allow_root_bypass"])
    return d, (password if d["auth"] == "password" else None)


# ---------------------------------------------------------------- 查询

def get(project: str, cid: int) -> dict:
    c = SshRepo.get(cid, project)
    if not c:
        raise NotFound("连接不存在")
    return c


def of_task(task: dict) -> Optional[dict]:
    """任务的执行位置：None = 本机。"""
    if not task.get("ssh_id"):
        return None
    c = SshRepo.get(task["ssh_id"], task["project"])
    if not c:
        raise RuntimeError("任务使用的 SSH 连接已被删除")
    return c


def list_for(project: str) -> list[dict]:
    rows = SshRepo.of_project(project)
    for r in rows:
        r["share_with_ai"] = bool(r["share_with_ai"])
        r["allow_root_bypass"] = bool(r["allow_root_bypass"])
        r["command"] = cmds.interactive_command(r)
        r["has_password"] = r["auth"] == "password" and keychain.has_password(r["id"])
        r["connected"] = transport.is_connected(r)
        r["target"] = cmds.target(r)
        r["running"] = len(TaskRepo.active_on_conn(r["id"]))
    return rows


def ai_context(project: str) -> str:
    """给 AI 的说明：本项目可用的 SSH 连接。没有可共享的连接时返回空串。"""
    rows = [r for r in SshRepo.of_project(project) if r["share_with_ai"]]
    if not rows:
        return ""
    lines = ["本项目在工作台里配置了以下 SSH 远程连接（需要操作远程服务器时使用；请原样使用给出的命令前缀，"
             "在后面追加远程命令，以非交互方式执行，例如 <命令前缀> '<远程命令>'。"
             "密码登录的连接会自动从钥匙串取密码，不要尝试读取或输出密码）："]
    for r in rows:
        s = f"- {r['name']}：{' '.join(shlex.quote(a) for a in cmds.prefix(r) + cmds.args(r))}"
        if r["remote_dir"]:
            s += f"；远程目录 {r['remote_dir']}"
        if r["note"]:
            s += f"；说明：{r['note']}"
        lines.append(s)
    return "\n".join(lines)


def config_hosts() -> list[str]:
    """~/.ssh/config 里的 Host 别名（不含通配符），供新建连接时选择。"""
    try:
        text = (Path.home() / ".ssh" / "config").read_text(errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "host":
            out += [h for h in parts[1].split() if not any(ch in h for ch in "*?!")]
    return sorted(set(out), key=str.lower)


# ---------------------------------------------------------------- 增删改

def create(project: str, raw: dict) -> list[dict]:
    project_path(project)
    d, password = validate(raw)
    if d["auth"] == "password" and not password:
        raise BadRequest("请填写密码")
    try:
        cid = SshRepo.create(project, d)
    except sqlite3.IntegrityError:
        raise Conflict(f"已有同名连接：{d['name']}")
    if password:
        try:
            keychain.set_password(cid, password)
        except Exception:
            SshRepo.delete(cid)
            raise
    return list_for(project)


def update(project: str, cid: int, raw: dict) -> list[dict]:
    old = get(project, cid)
    d, password = validate(raw)
    if d["auth"] == "password" and not password and not keychain.has_password(cid):
        raise BadRequest("请填写密码")
    try:
        SshRepo.update(cid, d)
    except sqlite3.IntegrityError:
        raise Conflict(f"已有同名连接：{d['name']}")
    if password:
        keychain.set_password(cid, password)
    elif d["auth"] == "key":  # 改回密钥登录：不再保留密码
        keychain.delete_password(cid)
    new = get(project, cid)
    if any(old[k] != new[k] for k in _CONN_KEYS) or password:
        transport.disconnect(old)
    return list_for(project)


def delete(project: str, cid: int) -> list[dict]:
    c = get(project, cid)
    if TaskRepo.active_on_conn(cid):
        raise Conflict("有任务正在这台服务器上执行，先停止或断开连接")
    transport.disconnect(c)
    SshRepo.delete(cid)
    keychain.delete_password(cid)
    return list_for(project)


# ---------------------------------------------------------------- 操作

async def test(project: str, cid: int) -> dict:
    """非交互地连一次（不会弹密码、不会自动信任新主机指纹），顺带检查服务器上有没有 claude / codex。"""
    c = get(project, cid)
    t0 = time.time()
    try:
        code, out, err = await transport.run(c, cmds.probe_cmd(c), timeout=30)
    except RuntimeError:
        return {"ok": False, "ms": int((time.time() - t0) * 1000), "message": "连接超时"}
    ms = int((time.time() - t0) * 1000)
    lines = out.splitlines()
    if "wb-ok" not in lines:
        msg = transport.clean_stderr(err)
        if any("Host key verification failed" in m for m in msg):
            msg.append("提示：首次连接这台主机，请先在终端里手动连一次并确认主机指纹。")
        return {"ok": False, "ms": ms, "message": "\n".join(msg[-4:]) or f"ssh 退出码 {code}"}
    has = {l.split(":", 2)[1]: l.split(":", 2)[2] for l in lines if l.startswith("wb-has:") and l.count(":") >= 2}
    parts = ["连接成功"]
    if "wb-dir" not in lines:
        parts.append(f"远程目录 {c['remote_dir']} 不存在")
    parts.append("服务器上 " + ("、".join(f"{k} ✓" for k in has) if has else "没找到 claude / codex")
                 + ("" if len(has) == 2 else "（缺少 " + "、".join(b for b in ("claude", "codex") if b not in has) + "）"))
    return {"ok": True, "ms": ms, "message": "；".join(parts), "clis": has, "dir_ok": "wb-dir" in lines}


async def connect(project: str, cid: int) -> list[dict]:
    c = get(project, cid)
    try:
        await transport.connect(c)
    except RuntimeError as e:
        raise UpstreamError(f"连接失败：{e}")
    return list_for(project)


async def disconnect(project: str, cid: int, runner: "Runner") -> list[dict]:
    """断开：先停止正在这台服务器上执行的任务（连同服务器上的 CLI 进程），再关闭主连接。"""
    c = get(project, cid)
    for t in TaskRepo.active_on_conn(cid):
        await runner.kill_remote_now(t["id"])  # 趁主连接还在，先结束服务器上的进程
        try:
            runner.cancel(t["id"])
        except Conflict:
            pass
    await asyncio.to_thread(transport.disconnect, c)
    return list_for(project)


def open_in_terminal(project: str, cid: int) -> dict:
    """在 macOS「终端」里打开这个连接。"""
    c = get(project, cid)
    if not IS_MAC:
        raise BadRequest("只支持 macOS，请复制命令到终端执行")
    cmd = cmds.interactive_command(c).replace("\\", "\\\\").replace('"', '\\"')
    r = subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script "{cmd}"',
                        "-e", 'tell application "Terminal" to activate'], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise WorkbenchError(f"打开终端失败：{r.stderr.strip()}")
    return {"ok": True}
