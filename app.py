"""个人工作台：管理 dzl-py 下的项目，提需求/Bug，每个任务由一个 claude CLI 进程执行。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
PROJECTS_ROOT = Path(os.environ.get("WB_PROJECTS_ROOT", BASE_DIR.parent)).resolve()
DATA_DIR = BASE_DIR / "data"
RUNS_DIR = DATA_DIR / "runs"
WORKTREES_DIR = DATA_DIR / "worktrees"
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "workbench.db"
def find_bin(name: str) -> str:
    """优先环境变量 / PATH；找不到时再搜 nvm 各 Node 版本下的全局包（不同终端 PATH 可能指向不同 Node 版本）。"""
    found = os.environ.get(f"{name.upper()}_BIN") or shutil.which(name)
    if found:
        return found
    cands = sorted(Path.home().glob(f".nvm/versions/node/*/bin/{name}"), reverse=True)
    cands += [Path(p) / name for p in ("/opt/homebrew/bin", "/usr/local/bin", Path.home() / ".local/bin")]
    return next((str(c) for c in cands if c.is_file()), name)


CLAUDE_BIN = find_bin("claude")
CODEX_BIN = find_bin("codex")
ENGINES = {"claude": CLAUDE_BIN, "codex": CODEX_BIN}
MAX_CONCURRENT = int(os.environ.get("WB_MAX_CONCURRENT", "4"))

for d in (DATA_DIR, RUNS_DIR, WORKTREES_DIR, UPLOADS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'task',
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    priority TEXT DEFAULT 'P2',
    status TEXT DEFAULT 'todo',      -- todo | queued | running | review | done | failed | cancelled
    permission_mode TEXT DEFAULT 'acceptEdits',
    model TEXT DEFAULT '',
    effort TEXT DEFAULT '',
    engine TEXT DEFAULT 'claude',     -- claude | codex
    use_worktree INTEGER DEFAULT 0,
    ssh_id INTEGER DEFAULT 0,         -- 执行位置：0 = 本机；否则为 ssh_conns.id，在远程服务器上跑 CLI
    workdir TEXT DEFAULT '',
    branch TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    fork_at TEXT DEFAULT '',
    images TEXT DEFAULT '[]',         -- 首条消息附带的图片（data/uploads 下的绝对路径，JSON）          -- claude: 下次继续时先回退到这条消息（--resume-session-at）
    total_cost REAL DEFAULT 0,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    prompt TEXT,
    status TEXT DEFAULT 'running',   -- running | success | error | cancelled | interrupted
    pid INTEGER,
    exit_code INTEGER,
    cost REAL DEFAULT 0,
    result TEXT DEFAULT '',
    images TEXT DEFAULT '[]',
    started_at REAL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS ssh_conns (     -- 每个项目自己的 SSH 远程连接（只存连接信息，不存密码）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    name TEXT NOT NULL,
    host TEXT NOT NULL,               -- 主机名 / IP，或 ~/.ssh/config 里的 Host 别名
    port INTEGER DEFAULT 0,           -- 0 = 默认（22 或 ssh config）
    user TEXT DEFAULT '',
    identity_file TEXT DEFAULT '',
    proxy_jump TEXT DEFAULT '',
    socks_proxy TEXT DEFAULT '',      -- SOCKS5 代理 host:port（经 nc -X 5 转发）
    auth TEXT DEFAULT 'key',          -- key | password（密码存在 macOS 钥匙串，不进数据库）
    remote_claude TEXT DEFAULT '',    -- 服务器上 claude / codex 的路径，空 = 用登录 shell 的 PATH
    remote_codex TEXT DEFAULT '',
    allow_root_bypass INTEGER DEFAULT 0,  -- 以 root 登录时也允许 claude「全部放行」（远程设 IS_SANDBOX=1）
    remote_dir TEXT DEFAULT '',       -- 登录后进入的目录
    note TEXT DEFAULT '',
    share_with_ai INTEGER DEFAULT 1,  -- 执行任务时把连接信息告诉 claude / codex
    created_at REAL,
    updated_at REAL,
    UNIQUE(project, name)
);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql: str, args=(), one=False):
    with closing(db()) as conn:
        cur = conn.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
        return (rows[0] if rows else None) if one else rows


def ex(sql: str, args=()) -> int:
    with closing(db()) as conn:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


def init_db():
    with closing(db()) as conn:
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "engine" not in cols:  # 旧库迁移
            conn.execute("ALTER TABLE tasks ADD COLUMN engine TEXT DEFAULT 'claude'")
        if "effort" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN effort TEXT DEFAULT ''")
        if "fork_at" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN fork_at TEXT DEFAULT ''")
        if "images" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN images TEXT DEFAULT '[]'")
        if "ssh_id" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN ssh_id INTEGER DEFAULT 0")
        ssh_cols = {r[1] for r in conn.execute("PRAGMA table_info(ssh_conns)")}
        if "socks_proxy" not in ssh_cols:
            conn.execute("ALTER TABLE ssh_conns ADD COLUMN socks_proxy TEXT DEFAULT ''")
        if "auth" not in ssh_cols:
            conn.execute("ALTER TABLE ssh_conns ADD COLUMN auth TEXT DEFAULT 'key'")
        for col in ("remote_claude", "remote_codex"):
            if col not in ssh_cols:
                conn.execute(f"ALTER TABLE ssh_conns ADD COLUMN {col} TEXT DEFAULT ''")
        if "allow_root_bypass" not in ssh_cols:
            conn.execute("ALTER TABLE ssh_conns ADD COLUMN allow_root_bypass INTEGER DEFAULT 0")
        if "images" not in {r[1] for r in conn.execute("PRAGMA table_info(runs)")}:
            conn.execute("ALTER TABLE runs ADD COLUMN images TEXT DEFAULT '[]'")
        # 服务重启时，之前在跑的进程已经不受管理，标记为中断
        conn.execute("UPDATE runs SET status='interrupted', ended_at=? WHERE status='running'", (time.time(),))
        conn.execute("UPDATE tasks SET status='failed' WHERE status IN ('running','queued')")
        conn.commit()


# ---------------------------------------------------------------- projects

def git(cwd: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def project_path(name: str) -> Path:
    p = (PROJECTS_ROOT / name).resolve()
    if p.parent != PROJECTS_ROOT or not p.is_dir():
        raise HTTPException(404, f"项目不存在: {name}")
    return p


def scan_projects() -> list[dict]:
    counts = {}
    for r in q("SELECT project, status, COUNT(*) c FROM tasks GROUP BY project, status"):
        counts.setdefault(r["project"], {})[r["status"]] = r["c"]
    ssh_counts = {r["project"]: r["c"] for r in q("SELECT project, COUNT(*) c FROM ssh_conns GROUP BY project")}
    out = []
    for p in sorted(PROJECTS_ROOT.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir() or p.name.startswith(".") or p == BASE_DIR:
            continue
        is_git = (p / ".git").exists()
        out.append({
            "name": p.name,
            "path": str(p),
            "is_git": is_git,
            "branch": git(p, "rev-parse", "--abbrev-ref", "HEAD") if is_git else "",
            "dirty": len(git(p, "status", "--porcelain").splitlines()) if is_git else 0,
            "has_claude_md": (p / "CLAUDE.md").exists(),
            "mtime": p.stat().st_mtime,
            "task_counts": counts.get(p.name, {}),
            "ssh_count": ssh_counts.get(p.name, 0),
        })
    return out


# ---------------------------------------------------------------- SSH 连接（按项目）

SSH_HOST_RE = re.compile(r"^[A-Za-z0-9._:%\-\[\]]{1,253}$")
SSH_USER_RE = re.compile(r"^[A-Za-z0-9._\-]{0,64}$")
SSH_JUMP_RE = re.compile(r"^[A-Za-z0-9@._:,%\-\[\]]{0,512}$")
SOCKS_RE = re.compile(r"^(?:[A-Za-z0-9.\-]{1,253}|\[[0-9A-Fa-f:.]+\]):\d{1,5}$")


KEYCHAIN_SERVICE = os.environ.get("WB_KEYCHAIN_SERVICE", "workbench-ssh")
ASKPASS = DATA_DIR / "bin" / "wb-askpass"


def ensure_askpass():
    """ssh 的 SSH_ASKPASS 程序：按 WB_SSH_ID 从钥匙串取出该连接的密码。"""
    ASKPASS.parent.mkdir(parents=True, exist_ok=True)
    ASKPASS.write_text("#!/bin/sh\n# 工作台生成：ssh 询问密码时从 macOS 钥匙串读取（只读这一个连接的条目）\n"
                       f'exec /usr/bin/security find-generic-password -s {KEYCHAIN_SERVICE} -a "wb-$WB_SSH_ID" -w\n')
    ASKPASS.chmod(0o700)


def keychain_set(cid: int, password: str):
    if os.uname().sysname != "Darwin":
        raise HTTPException(400, "密码登录只支持 macOS（密码保存在钥匙串里）")
    esc = password.replace("\\", "\\\\").replace('"', '\\"')
    # 经 stdin 传给 security -i，避免密码出现在进程参数里
    r = subprocess.run(["/usr/bin/security", "-i"], capture_output=True, text=True, timeout=15,
                       input=f'add-generic-password -U -s {KEYCHAIN_SERVICE} -a wb-{cid} -l "工作台 SSH #{cid}" -w "{esc}"\n')
    if r.returncode != 0 or "error" in r.stderr.lower():
        raise HTTPException(500, f"保存密码到钥匙串失败：{r.stderr.strip()}")


def keychain_has(cid: int) -> bool:
    if os.uname().sysname != "Darwin":
        return False
    return subprocess.run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", f"wb-{cid}"],
                          capture_output=True, timeout=15).returncode == 0


def keychain_del(cid: int):
    if os.uname().sysname == "Darwin":
        subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", f"wb-{cid}"],
                       capture_output=True, timeout=15)


def ssh_env(c: dict) -> dict:
    """密码登录时 ssh 需要的环境变量（不含密码本身）。"""
    if c.get("auth") != "password":
        return {}
    return {"SSH_ASKPASS": str(ASKPASS), "SSH_ASKPASS_REQUIRE": "force", "WB_SSH_ID": str(c["id"])}


def ssh_args(c: dict) -> list[str]:
    """连接参数（不含 ssh 本身和远程命令）。"""
    args = []
    if c.get("auth") == "password":
        args += ["-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=keyboard-interactive,password"]
    if c.get("port"):
        args += ["-p", str(c["port"])]
    if c.get("identity_file"):
        args += ["-i", c["identity_file"]]
    if c.get("proxy_jump"):
        args += ["-J", c["proxy_jump"]]
    if c.get("socks_proxy"):
        args += ["-o", f"ProxyCommand=nc -X 5 -x {c['socks_proxy']} %h %p"]
    return args + [f"{c['user']}@{c['host']}" if c.get("user") else c["host"]]


def ssh_prefix(c: dict) -> list[str]:
    """命令行开头：密码登录时用 env 带上 askpass 变量，否则就是 ssh。"""
    env = ssh_env(c)
    return (["env"] + [f"{k}={v}" for k, v in env.items()] if env else []) + ["ssh"]


def ssh_command(c: dict) -> str:
    """可直接粘到终端的命令；配置了远程目录时登录后进入该目录。"""
    pre = " ".join(shlex.quote(a) for a in ssh_prefix(c)[:-1])
    return (pre + " " if pre else "") + _ssh_command(c)


def remote_dir_expr(d: str) -> str:
    """远程目录的 shell 表达式；~ 开头的路径留给远程 shell 展开。空 = home。"""
    d = (d or "").strip() or "~"
    if d == "~":
        return "~"
    if d.startswith("~/"):
        return "~/" + shlex.quote(d[2:])
    return shlex.quote(d)


def _ssh_command(c: dict) -> str:
    base = ["ssh"] + ssh_args(c)
    if c.get("remote_dir"):
        return " ".join(shlex.quote(a) for a in ["ssh", "-t"] + base[1:]) + " " + shlex.quote(
            f"cd {remote_dir_expr(c['remote_dir'])} && exec $SHELL -l")
    return " ".join(shlex.quote(a) for a in base)


# ---------------------------------------------------------------- 在 SSH 服务器上执行 claude / codex

REMOTE_UPLOADS = ".workbench/uploads"   # 相对远程 home
REMOTE_RUNS = ".workbench/runs"         # 远程进程的 pid 文件，用于停止任务
# 远程用交互式 shell 启动（为了加载 .bashrc 里的 nvm 等 PATH），没有 tty 时 bash 会打这些无害警告
SHELL_NOISE = ("no job control in this shell", "cannot set terminal process group",
               "Inappropriate ioctl for device", "Pseudo-terminal will not be allocated")


def task_conn(task: dict) -> Optional[dict]:
    """任务的执行位置：None = 本机。"""
    if not task.get("ssh_id"):
        return None
    c = q("SELECT * FROM ssh_conns WHERE id=? AND project=?", (task["ssh_id"], task["project"]), one=True)
    if not c:
        raise RuntimeError("任务使用的 SSH 连接已被删除")
    return c


def remote_shell(inner: str) -> str:
    """让远程用户的登录 + 交互 shell 执行 inner，这样 .bashrc / .zshrc 里配置的 PATH（nvm 等）都能生效。"""
    return f'exec "${{SHELL:-/bin/sh}}" -lic {shlex.quote(inner)}'


# ---- 持久连接（OpenSSH ControlMaster）：「连接」建立一条主连接，远程任务 / 上传 / 额度查询都复用它；「断开」关闭它
_CM_DIR = Path("/tmp") / f"wb-ssh-{os.getuid()}"   # unix socket 路径有长度限制，放在短路径下
_CM_TAG = format(abs(hash(str(DATA_DIR))) % 0xFFFFFF, "06x")  # 同一台机器上多个工作台实例互不干扰
_connect_locks: dict[int, asyncio.Lock] = {}


def cm_path(c: dict) -> str:
    return str(_CM_DIR / f"{_CM_TAG}-{c['id']}")


def ssh_auth_opts(c: dict) -> list[str]:
    # 密码登录不能开 BatchMode（会禁用 askpass）；改为严格校验主机指纹、只尝试一次密码
    return (["-o", "StrictHostKeyChecking=yes", "-o", "NumberOfPasswordPrompts=1"] if c.get("auth") == "password"
            else ["-o", "BatchMode=yes"])


def ssh_exec_argv(c: dict, remote_cmd: str, tty: bool = False) -> tuple[list[str], dict]:
    """非交互地在服务器上执行 remote_cmd 的本地 ssh 命令及环境变量（已连接时复用主连接）。"""
    argv = ["ssh", "-tt" if tty else "-T", *ssh_auth_opts(c), "-o", "ConnectTimeout=10",
            "-o", f"ControlPath={cm_path(c)}", "-o", "ControlMaster=no",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4", *ssh_args(c), remote_cmd]
    return argv, {**child_env(), **ssh_env(c)}


def ssh_connected(c: dict) -> bool:
    if not Path(cm_path(c)).exists():
        return False
    r = subprocess.run(["ssh", "-o", f"ControlPath={cm_path(c)}", "-O", "check", *ssh_args(c)],
                       capture_output=True, timeout=5)
    return r.returncode == 0


async def ssh_connect(c: dict) -> None:
    """建立主连接（已连接则直接返回）。失败抛 RuntimeError，消息为 ssh 的报错。"""
    lock = _connect_locks.setdefault(c["id"], asyncio.Lock())
    async with lock:
        if await asyncio.to_thread(ssh_connected, c):
            return
        _CM_DIR.mkdir(mode=0o700, exist_ok=True)
        Path(cm_path(c)).unlink(missing_ok=True)  # 清掉失效的 socket
        argv = ["ssh", "-M", "-N", "-f", *ssh_auth_opts(c), "-o", "ConnectTimeout=10",
                "-o", f"ControlPath={cm_path(c)}", "-o", "ControlPersist=no",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4", *ssh_args(c)]
        # -f：认证成功后转入后台。后台进程会继承输出，所以 stderr 写到文件而不是管道，否则这里会一直等不到 EOF
        errf = _CM_DIR / f"{_CM_TAG}-{c['id']}.err"
        with open(errf, "wb") as ef:
            proc = await asyncio.create_subprocess_exec(*argv, env={**child_env(), **ssh_env(c)},
                                                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=ef, start_new_session=True)
            try:
                code = await asyncio.wait_for(proc.wait(), 30)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError("连接超时")
        err = errf.read_bytes()
        errf.unlink(missing_ok=True)
        if code != 0 or not await asyncio.to_thread(ssh_connected, c):
            msg = [m for m in err.decode(errors="replace").strip().splitlines()
                   if not m.startswith(("Warning: Permanently added", "** "))]
            if any("Host key verification failed" in m for m in msg):
                msg.append("首次连接这台主机，请先在终端里手动连一次并确认主机指纹。")
            raise RuntimeError("\n".join(msg[-3:]) or f"ssh 退出码 {code}")


def ssh_disconnect_master(c: dict) -> None:
    subprocess.run(["ssh", "-o", f"ControlPath={cm_path(c)}", "-O", "exit", *ssh_args(c)],
                   capture_output=True, timeout=10)
    Path(cm_path(c)).unlink(missing_ok=True)


async def ssh_run(c: dict, remote_cmd: str, stdin: Optional[bytes] = None, timeout: float = 60) -> tuple[int, str, str]:
    argv, env = ssh_exec_argv(c, remote_cmd)
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
        code, so, se = await ssh_run(c, f'mkdir -p {REMOTE_UPLOADS} && cat > {REMOTE_UPLOADS}/{name} && printf %s "$HOME"',
                                     Path(i).read_bytes(), 180)
        if code != 0:
            raise RuntimeError(f"上传图片到服务器失败：{se.strip()}")
        home = so.strip()
        out.append(f"{home}/{REMOTE_UPLOADS}/{name}")
    return out, f"{home}/{REMOTE_UPLOADS}"


ROOT_BYPASS_MSG = "Claude CLI 不允许以 root 身份使用「全部放行」"
ROOT_BYPASS_HINT = (ROOT_BYPASS_MSG + "。可以：① 改用非 root 用户登录；② 权限模式改为「自动改文件」；"
                    "③ 在该 SSH 连接里勾选「允许以 root 全部放行」（会以 IS_SANDBOX=1 启动 claude，AI 将拥有服务器的完全 root 权限）。")


def remote_cli_env(c: dict, task: dict) -> dict:
    """远程 CLI 额外的环境变量：连接允许时，让 claude 在 root 下也能「全部放行」。"""
    if ((task.get("engine") or "claude") == "claude" and task.get("permission_mode") == "bypassPermissions"
            and c.get("allow_root_bypass")):
        return {"IS_SANDBOX": "1"}
    return {}


def check_remote_bypass(task: dict):
    """发送前检查：已知以 root 登录、选了全部放行、又没在连接里允许时，直接说明原因，而不是让 CLI 报错。"""
    if not task.get("ssh_id") or (task.get("engine") or "claude") != "claude" or task.get("permission_mode") != "bypassPermissions":
        return
    try:
        c = task_conn(task)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    if c["user"] == "root" and not c["allow_root_bypass"]:
        raise HTTPException(400, ROOT_BYPASS_HINT)


def remote_run_cmd(c: dict, cmd: list[str], run_id: int, env: Optional[dict] = None) -> str:
    """服务器上：进入远程目录，记下 pid（exec 后就是 CLI 本身的 pid），再执行 CLI。"""
    inner = (f"find ~/{REMOTE_RUNS} -name '*.pid' -mtime +7 -delete 2>/dev/null; "
             f"cd {remote_dir_expr(c['remote_dir'])} && mkdir -p ~/{REMOTE_RUNS} && echo $$ > ~/{REMOTE_RUNS}/{run_id}.pid"
             f" && exec {shlex.join((['env'] + [f'{k}={v}' for k, v in env.items()] if env else []) + cmd)}")
    return remote_shell(inner)


async def remote_kill(c: dict, run_id: int):
    """停止服务器上的 CLI 进程（连同它启动的子进程）。"""
    f = f"~/{REMOTE_RUNS}/{run_id}.pid"
    try:
        await ssh_run(c, f'p=$(cat {f} 2>/dev/null) && [ -n "$p" ] && {{ pkill -TERM -P "$p"; kill -TERM "$p"; }}; rm -f {f}', timeout=20)
    except Exception:  # noqa
        pass


def remote_resume_cmd(c: dict, engine: str, sid: str) -> str:
    """终端接管：ssh 到服务器，在远程目录里继续同一个会话。"""
    resume = (f"{shlex.quote(c.get('remote_codex') or 'codex')} resume {shlex.quote(sid)}" if engine == "codex"
              else f"{shlex.quote(c.get('remote_claude') or 'claude')} --resume {shlex.quote(sid)}")
    pre = " ".join(shlex.quote(a) for a in ssh_prefix(c)[:-1])
    cmd = " ".join(shlex.quote(a) for a in ["ssh", "-t", *ssh_args(c)]) + " " + shlex.quote(
        remote_shell(f"cd {remote_dir_expr(c['remote_dir'])} && {resume}"))
    return (pre + " " if pre else "") + cmd


def ssh_list(project: str) -> list[dict]:
    rows = q("SELECT * FROM ssh_conns WHERE project=? ORDER BY name COLLATE NOCASE", (project,))
    for r in rows:
        r["share_with_ai"] = bool(r["share_with_ai"])
        r["allow_root_bypass"] = bool(r["allow_root_bypass"])
        r["command"] = ssh_command(r)
        r["has_password"] = r["auth"] == "password" and keychain_has(r["id"])
        r["connected"] = ssh_connected(r)
        r["target"] = f"{r['user'] + '@' if r['user'] else ''}{r['host']}" + (f":{r['port']}" if r["port"] else "")
        r["running"] = sum(1 for t in q("SELECT id FROM tasks WHERE ssh_id=? AND status IN ('running','queued')", (r["id"],)))
    return rows


def ssh_context(project: str) -> str:
    """给 AI 的说明：本项目可用的 SSH 连接。没有可共享的连接时返回空串。"""
    rows = [r for r in ssh_list(project) if r["share_with_ai"]]
    if not rows:
        return ""
    lines = ["本项目在工作台里配置了以下 SSH 远程连接（需要操作远程服务器时使用；请原样使用给出的命令前缀，"
             "在后面追加远程命令，以非交互方式执行，例如 <命令前缀> '<远程命令>'。"
             "密码登录的连接会自动从钥匙串取密码，不要尝试读取或输出密码）："]
    for r in rows:
        s = f"- {r['name']}：{' '.join(shlex.quote(a) for a in ssh_prefix(r) + ssh_args(r))}"
        if r["remote_dir"]:
            s += f"；远程目录 {r['remote_dir']}"
        if r["note"]:
            s += f"；说明：{r['note']}"
        lines.append(s)
    return "\n".join(lines)


def ssh_config_hosts() -> list[str]:
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


# ---------------------------------------------------------------- runner

def build_prompt(task: dict) -> str:
    """用户在对话框里写的内容原样作为提示词。"""
    return task["description"] or task["title"]


def derive_title(text: str) -> str:
    line = next((l.strip() for l in text.splitlines() if l.strip()), "")
    return line[:60] + ("…" if len(line) > 60 else "")


def child_env() -> dict:
    env = dict(os.environ)
    # 若工作台本身是从某个 Claude Code 会话/桌面端里启动的，会继承一批内部环境变量
    # （如 CLAUDE_CODE_SIMPLE 会让子进程跳过 OAuth 登录），这里全部清掉，让子进程按普通 CLI 运行。
    keep = {"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_MAX_OUTPUT_TOKENS"}
    for k in list(env):
        if k in keep:
            continue
        if k in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT") or k.startswith(
                ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_", "CLAUDE_PREVIEW_")):
            env.pop(k)
    return env


def ensure_workdir(task: dict) -> Path:
    proj = project_path(task["project"])
    if task["workdir"] and Path(task["workdir"]).is_dir():
        return Path(task["workdir"])
    if task["use_worktree"] and (proj / ".git").exists():
        wt = WORKTREES_DIR / task["project"] / f"task-{task['id']}"
        branch = f"wb/task-{task['id']}"
        wt.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "worktree", "add", "-b", branch, str(wt)], cwd=proj,
                           capture_output=True, text=True)
        if r.returncode != 0:  # 分支已存在
            r = subprocess.run(["git", "worktree", "add", str(wt), branch], cwd=proj,
                               capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"创建 worktree 失败: {r.stderr.strip()}")
        ex("UPDATE tasks SET workdir=?, branch=? WHERE id=?", (str(wt), branch, task["id"]))
        return wt
    ex("UPDATE tasks SET workdir=? WHERE id=?", (str(proj), task["id"]))
    return proj


def run_log(run_id: int) -> Path:
    return RUNS_DIR / f"run-{run_id}.jsonl"


# 权限模式 -> codex 沙箱
CODEX_SANDBOX = {"acceptEdits": "workspace-write", "plan": "read-only", "default": "read-only"}


def with_images(prompt: str, images: list[str]) -> str:
    if not images:
        return prompt
    return (prompt or "请查看附带的图片。") + "\n\n附带图片（用 Read 工具查看）：\n" + "\n".join(f"- {i}" for i in images)


def build_cmd(task: dict, prompt: str, resume_sid: Optional[str], images: Optional[list[str]] = None,
              remote: Optional[dict] = None, uploads_dir: Optional[str] = None) -> list[str]:
    """remote=True 时生成在服务器上执行的命令：用 PATH 里的 claude / codex，图片路径为服务器上的路径。"""
    images = images or []
    mode = task["permission_mode"] or "acceptEdits"
    # 远程任务不附带本机的 SSH 连接说明（命令里的 askpass 路径在服务器上不存在）
    ssh_ctx = "" if remote else ssh_context(task["project"])
    if task.get("engine") == "codex":
        if ssh_ctx and not resume_sid:  # codex 没有追加系统提示的参数：新会话时放在提示词前面
            prompt = f"{ssh_ctx}\n\n---\n\n{prompt or '请查看附带的图片。'}"
        cmd = [(remote.get("remote_codex") or "codex") if remote else CODEX_BIN, "exec"] + (["resume", resume_sid] if resume_sid else [])
        cmd += ["--json", "--skip-git-repo-check"]
        if mode == "bypassPermissions":
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            cmd += ["-c", f'sandbox_mode="{CODEX_SANDBOX.get(mode, "workspace-write")}"']
        if task["model"]:
            cmd += ["-m", task["model"]]
        if task.get("effort"):
            cmd += ["-c", f'model_reasoning_effort="{task["effort"]}"']
        for img in images:
            cmd += ["-i", img]
        return cmd + ["--", prompt or "请查看附带的图片。"]  # -i 可接多个值，用 -- 隔开提示词
    cmd = [(remote.get("remote_claude") or "claude") if remote else CLAUDE_BIN, "-p", with_images(prompt, images), "--output-format", "stream-json", "--verbose"]
    if images:  # 图片在 data/uploads 里，不在项目目录内，需显式放行读取
        cmd += ["--add-dir", uploads_dir or str(UPLOADS_DIR)]
    cmd += ["--permission-mode", mode]
    if ssh_ctx:
        cmd += ["--append-system-prompt", ssh_ctx]
    if task["model"]:
        cmd += ["--model", task["model"]]
    if task.get("effort"):
        cmd += ["--effort", task["effort"]]
    if resume_sid:
        cmd += ["--resume", resume_sid]
        if task.get("fork_at"):  # 编辑过历史消息：从该点分叉出新会话，原会话不动
            cmd += ["--resume-session-at", task["fork_at"], "--fork-session"]
    return cmd


def parse_event(engine: str, ev: dict, st: dict):
    """从事件流中提取 session_id / 结果 / 花费 / 状态，写入 st。"""
    t = ev.get("type")
    if engine == "codex":
        if t == "thread.started":
            st["sid"] = ev.get("thread_id")
        elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            st["result"] = ev["item"].get("text") or ""
        elif t == "turn.completed":
            st["status"] = "success"
        elif t == "turn.failed":
            st["status"] = "error"
            st["result"] = ((ev.get("error") or {}).get("message")) or st["result"]
        elif t == "error":
            st["status"] = "error"
            st["result"] = ev.get("message") or st["result"]
        return
    if t == "system" and ev.get("session_id"):
        st["sid"] = ev["session_id"]
    elif t == "result":
        st["result"] = ev.get("result") or ""
        st["cost"] = float(ev.get("total_cost_usd") or 0)
        st["status"] = "error" if ev.get("is_error") else "success"


class Runner:
    def __init__(self):
        self.procs: dict[int, asyncio.subprocess.Process] = {}   # task_id -> proc
        self.sem = asyncio.Semaphore(MAX_CONCURRENT)

    def is_running(self, task_id: int) -> bool:
        return task_id in self.procs

    async def start(self, task_id: int, message: Optional[str] = None, images: Optional[list[str]] = None):
        task = q("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
        if not task:
            raise HTTPException(404, "任务不存在")
        if self.is_running(task_id) or task["status"] in ("running", "queued"):
            raise HTTPException(409, "任务正在执行中")
        check_remote_bypass(task)
        resume = bool(message and task["session_id"])
        prompt = message if resume else (build_prompt(task) + (f"\n\n补充说明：\n{message}" if message else ""))
        if not resume:  # 新会话：带上任务首条消息的图片
            images = json.loads(task.get("images") or "[]") + (images or [])
        images = [i for i in (images or []) if Path(i).is_file()]
        run_id = ex("INSERT INTO runs(task_id, prompt, status, images, started_at) VALUES(?,?,?,?,?)",
                    (task_id, prompt, "running", json.dumps(images), time.time()))
        ex("UPDATE tasks SET status='queued', updated_at=? WHERE id=?", (time.time(), task_id))
        self.procs[task_id] = None  # 占位，防止重复启动
        asyncio.create_task(self._run(task, run_id, prompt, task["session_id"] if resume else None, images))
        return run_id

    async def _run(self, task: dict, run_id: int, prompt: str, resume_sid: Optional[str], images: list[str]):
        task_id = task["id"]
        log = run_log(run_id)
        status, result, cost, code = "error", "", 0.0, None
        try:
            async with self.sem:
                if self.procs.get(task_id, None) == "cancel":
                    raise asyncio.CancelledError
                engine = task.get("engine") or "claude"
                conn = task_conn(task)
                ex("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (time.time(), task_id))
                if conn:  # 远程：ssh 到服务器，在远程目录里跑服务器上的 claude / codex
                    cwd_label = f"{conn['name']}:{conn['remote_dir'] or '~'}"
                    ex("UPDATE tasks SET workdir=? WHERE id=?", (conn["remote_dir"] or "~", task_id))
                    await ssh_connect(conn)  # 未连接时自动连接；之后上传和执行都复用这条连接
                    rimgs, rdir = await upload_images(conn, images) if images else ([], None)
                    cli = build_cmd(task, prompt, resume_sid, rimgs, remote=conn, uploads_dir=rdir)
                    cmd, env = ssh_exec_argv(conn, remote_run_cmd(conn, cli, run_id, remote_cli_env(conn, task)))
                    workdir = BASE_DIR
                else:
                    workdir = ensure_workdir(q("SELECT * FROM tasks WHERE id=?", (task_id,), one=True))
                    cwd_label = str(workdir)
                    cli = cmd = build_cmd(task, prompt, resume_sid, images)
                    env = child_env()
                shown = list(cli)
                shown[-1 if engine == "codex" else 2] = "<prompt>"
                if self.procs.get(task_id) == "cancel":  # 上传图片期间被停止
                    raise asyncio.CancelledError
                with open(log, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "wb", "subtype": "start", "cwd": cwd_label,
                                        "remote": conn["name"] if conn else "",
                                        "engine": engine, "model": task["model"] or "",
                                        "effort": task.get("effort") or "", "resume": bool(resume_sid),
                                        "images": [upload_url(i) for i in images],
                                        "cmd": (f"[ssh {conn['name']}] " if conn else "") + " ".join(shown),
                                        "prompt": prompt}, ensure_ascii=False) + "\n")
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=workdir, env=env, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    limit=64 * 1024 * 1024, start_new_session=True)
                if conn:
                    proc._wb_remote = (conn, run_id)
                self.procs[task_id] = proc
                ex("UPDATE runs SET pid=? WHERE id=?", (proc.pid, run_id))

                async def pump_err():
                    async for line in proc.stderr:
                        if line.startswith(b"Reading additional input from stdin"):
                            continue
                        if conn and any(n.encode() in line for n in SHELL_NOISE):
                            continue
                        if conn and b"cannot be used with root/sudo privileges" in line:
                            line += ("\n" + ROOT_BYPASS_HINT).encode()
                        with open(log, "a", encoding="utf-8") as f:
                            f.write(json.dumps({"type": "wb", "subtype": "stderr",
                                                "text": line.decode(errors="replace")}, ensure_ascii=False) + "\n")
                err_task = asyncio.create_task(pump_err())

                st = {"sid": None, "result": "", "cost": 0.0, "status": None}
                with open(log, "a", encoding="utf-8") as f:
                    async for raw in proc.stdout:
                        line = raw.decode(errors="replace").strip()
                        if not line:
                            continue
                        f.write(line + "\n")
                        f.flush()
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        old_sid = st["sid"]
                        parse_event(engine, ev, st)
                        if st["sid"] and st["sid"] != old_sid:
                            ex("UPDATE tasks SET session_id=?, fork_at='' WHERE id=?", (st["sid"], task_id))
                result, cost, status = st["result"], st["cost"], st["status"] or "error"
                code = await proc.wait()
                await err_task
                if code != 0 and status == "success":
                    status = "error"
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as e:  # noqa
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "wb", "subtype": "error", "text": str(e)}, ensure_ascii=False) + "\n")
        finally:
            if self.procs.get(task_id) == "cancel" or getattr(self.procs.get(task_id), "_wb_cancel", False):
                status = "cancelled"
            self.procs.pop(task_id, None)
            ex("UPDATE runs SET status=?, exit_code=?, cost=?, result=?, ended_at=? WHERE id=?",
               (status, code, cost, result, time.time(), run_id))
            new_status = {"success": "review", "cancelled": "cancelled"}.get(status, "failed")
            ex("UPDATE tasks SET status=?, total_cost=total_cost+?, updated_at=? WHERE id=?",
               (new_status, cost, time.time(), task_id))
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "wb", "subtype": "end", "status": status, "exit_code": code},
                                   ensure_ascii=False) + "\n")

    def cancel(self, task_id: int):
        proc = self.procs.get(task_id)
        if task_id not in self.procs:
            raise HTTPException(409, "任务未在执行")
        if proc is None:  # 还在排队
            self.procs[task_id] = "cancel"
            return
        proc._wb_cancel = True
        if getattr(proc, "_wb_remote", None):  # 远程任务：同时停掉服务器上的 CLI 进程
            asyncio.create_task(remote_kill(*proc._wb_remote))
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    async def shutdown(self):
        for proc in list(self.procs.values()):
            if isinstance(proc, asyncio.subprocess.Process):
                if getattr(proc, "_wb_remote", None):
                    await remote_kill(*proc._wb_remote)
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


runner: Runner


@asynccontextmanager
async def lifespan(app: FastAPI):
    global runner
    init_db()
    ensure_askpass()
    runner = Runner()
    yield
    await runner.shutdown()


app = FastAPI(title="Workbench", lifespan=lifespan)

# ---------------------------------------------------------------- API


class TaskIn(BaseModel):
    project: str
    prompt: str
    title: str = ""
    engine: str = "claude"
    permission_mode: str = "acceptEdits"
    model: str = ""
    effort: str = ""
    use_worktree: bool = False
    ssh_id: int = 0                   # 执行位置：0 = 本机；否则为本项目的 SSH 连接 id
    run_now: bool = False
    images: list[str] = []


class TaskPatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    permission_mode: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    kind: Optional[str] = None


class RunIn(BaseModel):
    message: Optional[str] = None
    # 本次（及之后）使用的模型 / 思考等级 / 权限，随发送一起提交，避免与单独的 PATCH 竞争
    model: Optional[str] = None
    effort: Optional[str] = None
    permission_mode: Optional[str] = None
    # 编辑某条已发送的消息：回退到该消息之前，用新内容重新发送（该条及之后的记录会被替换）
    edit_run_id: Optional[int] = None
    images: Optional[list[str]] = None  # 本条消息附带的图片（/api/uploads 返回的 path）


@app.get("/api/config")
def config():
    engines = {k: bool(shutil.which(v) or Path(v).is_file()) for k, v in ENGINES.items()}
    return {"projects_root": str(PROJECTS_ROOT), "engines": engines, "max_concurrent": MAX_CONCURRENT,
            "codex_models": codex_models(), "codex_efforts": codex_efforts(),
            "codex_default_effort": codex_config().get("model_reasoning_effort", ""),
            "claude_efforts": CLAUDE_EFFORTS}


CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
EFFORT_RE = re.compile(r"^[a-z]{0,16}$")


# 与 Codex 客户端一致，只提供这五档（不含 max）
CODEX_EFFORTS = ["low", "medium", "high", "xhigh", "ultra"]


def codex_config() -> dict:
    """读取 ~/.codex/config.toml（失败返回空）。"""
    import tomllib
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    try:
        return tomllib.loads((home / "config.toml").read_text())
    except Exception:
        return {}


def codex_efforts() -> dict:
    """每个 codex 模型支持的思考等级，及未选模型时使用的默认值。"""
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    try:
        data = json.loads((home / "models_cache.json").read_text())
    except Exception:
        return {}
    return {m["slug"]: {"levels": [e for e in CODEX_EFFORTS
                                   if e in {x.get("effort") for x in m.get("supported_reasoning_levels", [])}],
                        "default": m.get("default_reasoning_level") or ""}
            for m in data.get("models", []) if m.get("slug")}


def codex_models() -> list[str]:
    """读取 codex 自己缓存的可用模型列表（~/.codex/models_cache.json）。"""
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    try:
        data = json.loads((home / "models_cache.json").read_text())
        return [m["slug"] for m in data.get("models", []) if m.get("slug") and m.get("visibility", "list") != "hide"]
    except Exception:
        return []


# ---------------------------------------------------------------- 额度

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
_quota_cache: dict = {"at": 0.0, "data": None}
_quota_lock = asyncio.Lock()


async def codex_rpc(calls: list[tuple[str, Optional[dict]]], timeout: float = 25) -> list:
    """启动一次 `codex app-server`，按顺序发 JSON-RPC 请求，返回各自的 result（任一出错即抛异常）。"""
    proc = await asyncio.create_subprocess_exec(
        CODEX_BIN, "app-server", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=child_env(), start_new_session=True, limit=64 * 1024 * 1024)

    def send(obj):
        proc.stdin.write((json.dumps(obj) + "\n").encode())

    async def reply(rid: int):
        async for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message") or str(msg["error"]))
                return msg["result"]
        raise RuntimeError("codex app-server 未返回结果")

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "workbench", "version": "0.1"}}})
        send({"jsonrpc": "2.0", "method": "initialized"})
        await proc.stdin.drain()
        await asyncio.wait_for(reply(1), timeout)
        out = []
        for i, (method, params) in enumerate(calls, start=2):
            send({"jsonrpc": "2.0", "id": i, "method": method, **({"params": params} if params else {})})
            await proc.stdin.drain()
            out.append(await asyncio.wait_for(reply(i), timeout))
        return out
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        await proc.wait()


async def codex_rate_limits_live(timeout: float = 25) -> dict:
    """通过 `codex app-server` 的 JSON-RPC 接口 account/rateLimits/read 实时读取额度。"""
    return (await codex_rpc([("account/rateLimits/read", None)], timeout))[0]


def codex_rate_limits_from_logs() -> Optional[dict]:
    """兜底：从最近的 codex 会话日志里取最后一次 rate_limits 快照（字段为 snake_case）。"""
    files = sorted((CODEX_HOME / "sessions").glob("*/*/*/rollout-*.jsonl"),
                   key=lambda f: f.stat().st_mtime, reverse=True)[:5]
    for f in files:
        snap = None
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"rate_limits"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rl = (ev.get("payload") or {}).get("rate_limits")
                if rl:
                    snap = (rl, ev.get("timestamp"))
        if snap:
            rl, ts = snap
            win = lambda w: w and {"usedPercent": w.get("used_percent"), "windowDurationMins": w.get("window_minutes"),
                                   "resetsAt": w.get("resets_at")}
            cr = rl.get("credits") or {}
            return {"rateLimits": {"limitId": rl.get("limit_id"), "limitName": rl.get("limit_name"),
                                   "primary": win(rl.get("primary")), "secondary": win(rl.get("secondary")),
                                   "credits": cr and {"hasCredits": cr.get("has_credits"), "unlimited": cr.get("unlimited"),
                                                      "balance": cr.get("balance")},
                                   "planType": rl.get("plan_type"), "rateLimitReachedType": rl.get("rate_limit_reached_type")},
                    "_snapshot_at": ts}
    return None


def window_label(mins: Optional[int]) -> str:
    if not mins:
        return "额度"
    if mins == 10080:
        return "每周"
    if mins % 1440 == 0:
        return f"{mins // 1440} 天"
    if mins % 60 == 0:
        return f"{mins // 60} 小时"
    return f"{mins} 分钟"


def normalize_codex(raw: dict, source: str) -> dict:
    limits = raw.get("rateLimitsByLimitId") or {"codex": raw.get("rateLimits") or {}}
    groups = []
    for lid, rl in limits.items():
        if not rl:
            continue
        windows = [{"label": window_label(w.get("windowDurationMins")), "used_percent": w.get("usedPercent"),
                    "window_mins": w.get("windowDurationMins"), "resets_at": w.get("resetsAt")}
                   for w in (rl.get("primary"), rl.get("secondary")) if w]
        groups.append({"id": lid, "name": rl.get("limitName") or ("Codex" if lid == "codex" else lid),
                       "windows": windows, "reached": rl.get("rateLimitReachedType")})
    main = raw.get("rateLimits") or next(iter(limits.values()), {}) or {}
    credits = main.get("credits") or {}
    resets = [c for c in (raw.get("rateLimitResetCredits") or {}).get("credits", []) if c.get("status") == "available"]
    return {
        "available": True, "source": source, "snapshot_at": raw.get("_snapshot_at"),
        "plan": main.get("planType"), "usage_allowed": raw.get("ordinaryUsageAllowed", True),
        "spend_control_reached": bool(main.get("spendControlReached")),
        "groups": groups,
        "credits": {"balance": credits.get("balance"), "unlimited": bool(credits.get("unlimited")),
                    "has": bool(credits.get("hasCredits"))} if credits else None,
        "reset_credits": {"count": len(resets), "expires_at": min((c.get("expiresAt") or 0 for c in resets), default=None)},
    }


@app.get("/api/quota")
async def quota(refresh: bool = False):
    async with _quota_lock:
        if not refresh and _quota_cache["data"] and time.time() - _quota_cache["at"] < 120:
            return _quota_cache["data"]
        try:
            codex = normalize_codex(await codex_rate_limits_live(), "live")
        except Exception as e:  # noqa
            snap = codex_rate_limits_from_logs()
            codex = normalize_codex(snap, "log") if snap else {"available": False, "error": str(e) or type(e).__name__}
        data = {"fetched_at": time.time(), "codex": codex,
                "claude": {"available": False, "reason": "暂未接入"}}
        _quota_cache.update(at=time.time(), data=data)
        return data


# ---------------------------------------------------------------- 远程 Claude 额度（经 SSH）

# 在服务器上执行：从 ~/.claude/.credentials.json 取 OAuth 令牌，调用 Claude Code /usage 用的同一个接口。
# 令牌经 stdin 交给 curl（不出现在进程参数里），也不会传回本机；只回传额度 JSON。
CLAUDE_USAGE_SCRIPT = r'''
f="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"
if [ ! -r "$f" ]; then echo "WB_ERR:nocred"; exit 0; fi
one() { tr -d '\n' < "$f" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\).*/\1/p" | head -n 1; }
t=$(one accessToken)
echo "WB_SUB:$(one subscriptionType)"
echo "WB_TIER:$(one rateLimitTier)"
echo "WB_EXP:$(one expiresAt)"
if [ -z "$t" ]; then echo "WB_ERR:nocred"; exit 0; fi
if ! command -v curl >/dev/null 2>&1; then echo "WB_ERR:nocurl"; exit 0; fi
printf 'Authorization: Bearer %s\n' "$t" | curl -sS -m 15 -H @- -H 'anthropic-beta: oauth-2025-04-20' \
  -H 'Content-Type: application/json' -w '\nWB_HTTP:%{http_code}\n' https://api.anthropic.com/api/oauth/usage
'''
CLAUDE_WINDOW_LABELS = {"five_hour": "5 小时", "seven_day": "每周", "seven_day_opus": "每周 · Opus",
                        "seven_day_sonnet": "每周 · Sonnet"}
_claude_remote_cache: dict[tuple, dict] = {}   # (user, host, port) -> {"at", "data"}


def iso_ts(v) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v) / (1000 if v > 1e12 else 1)
    if not v:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_claude_usage(out: str) -> dict:
    kv = {}
    body = []
    for line in out.splitlines():
        if line.startswith("WB_") and ":" in line:
            k, v = line.split(":", 1)
            kv[k] = v.strip()
        else:
            body.append(line)
    res = {"plan": kv.get("WB_SUB") or "", "tier": kv.get("WB_TIER") or "", "windows": []}
    err = kv.get("WB_ERR")
    if err == "nocred":
        return {**res, "error": "服务器上没有 Claude 登录凭据（~/.claude/.credentials.json）。在服务器上运行 claude 登录一次；用 API Key 计费的不显示额度。"}
    if err == "nocurl":
        return {**res, "error": "服务器上没有 curl，无法查询额度"}
    code = kv.get("WB_HTTP", "")
    exp = iso_ts(float(kv["WB_EXP"])) if kv.get("WB_EXP", "").isdigit() else None
    if code == "401":
        stale = exp and exp < time.time()
        return {**res, "error": "登录令牌已过期，在服务器上执行一次 Claude 任务后会自动刷新" if stale
                else "服务器上的 Claude 登录已失效，请在服务器上重新运行 claude 登录"}
    if code != "200":
        return {**res, "error": f"查询失败（HTTP {code or '无响应'}）：{' '.join(body).strip()[:160]}"}
    try:
        data = json.loads("\n".join(body))
    except json.JSONDecodeError:
        return {**res, "error": "额度接口返回了无法解析的内容"}
    for key, w in data.items():
        if not isinstance(w, dict) or w.get("utilization") is None:
            continue
        res["windows"].append({"key": key, "label": CLAUDE_WINDOW_LABELS.get(key, key),
                               "used_percent": float(w["utilization"]), "resets_at": iso_ts(w.get("resets_at"))})
    order = list(CLAUDE_WINDOW_LABELS)
    res["windows"].sort(key=lambda w: order.index(w["key"]) if w["key"] in order else 99)
    if isinstance(data.get("extra_usage"), dict) and data["extra_usage"].get("is_enabled"):
        x = data["extra_usage"]
        res["extra"] = {"used": x.get("used_credits"), "limit": x.get("monthly_limit"),
                        "used_percent": x.get("utilization")}
    if not res["windows"]:
        res["error"] = "额度接口没有返回用量数据"
    return res


async def claude_remote_quota(c: dict, refresh: bool) -> dict:
    key = (c["user"], c["host"], c["port"])
    hit = _claude_remote_cache.get(key)
    if hit and not refresh and time.time() - hit["at"] < 120:
        return hit["data"]
    base = {"id": c["id"], "name": c["name"], "project": c["project"],
            "target": f"{c['user'] + '@' if c['user'] else ''}{c['host']}", "fetched_at": time.time()}
    try:
        code, out, err = await ssh_run(c, "sh -c " + shlex.quote(CLAUDE_USAGE_SCRIPT), timeout=30)
        if code != 0 and "WB_" not in out:
            msg = [m for m in err.strip().splitlines() if not m.startswith("Warning: Permanently added")]
            data = {**base, "error": "SSH 连接失败：" + (msg[-1] if msg else f"退出码 {code}")}
        else:
            data = {**base, **parse_claude_usage(out)}
    except RuntimeError as e:
        data = {**base, "error": str(e)}
    _claude_remote_cache[key] = {"at": time.time(), "data": data}
    return data


@app.get("/api/quota/claude-remote")
async def quota_claude_remote(refresh: bool = False):
    """用过远程 Claude 任务的 SSH 连接各自的额度（同一 user@host:port 只查一次）。"""
    conns = q("""SELECT s.* FROM ssh_conns s WHERE EXISTS
                 (SELECT 1 FROM tasks t WHERE t.ssh_id=s.id AND COALESCE(t.engine,'claude')='claude')
                 ORDER BY s.project, s.name""")
    uniq: dict[tuple, dict] = {}
    for c in conns:
        uniq.setdefault((c["user"], c["host"], c["port"]), c)
    return list(await asyncio.gather(*(claude_remote_quota(c, refresh) for c in uniq.values())))


@app.get("/api/projects")
def list_projects():
    return scan_projects()


class SshIn(BaseModel):
    name: str
    host: str
    port: int = 0
    user: str = ""
    identity_file: str = ""
    proxy_jump: str = ""
    socks_proxy: str = ""
    remote_dir: str = ""
    note: str = ""
    share_with_ai: bool = True
    remote_claude: str = ""
    remote_codex: str = ""
    allow_root_bypass: bool = False
    auth: str = "key"                 # key | password
    password: Optional[str] = None    # 只在设置 / 修改时提交；留空表示不改


def check_ssh(body: SshIn) -> tuple[dict, Optional[str]]:
    """校验并规范化；返回 (入库字段, 新密码或 None)。密码不入库。"""
    raw = body.model_dump()
    password = raw.pop("password") or None
    if body.auth not in ("key", "password"):
        raise HTTPException(400, "认证方式只能是 key 或 password")
    if password is not None and (len(password) > 1024 or "\n" in password or "\r" in password):
        raise HTTPException(400, "密码格式无效")
    if body.auth == "password" and os.uname().sysname != "Darwin":
        raise HTTPException(400, "密码登录只支持 macOS（密码保存在钥匙串里）")
    d = {k: v.strip() if isinstance(v, str) else v for k, v in raw.items()}
    if d["auth"] == "password":
        d["identity_file"] = ""
    if not d["name"] or len(d["name"]) > 60:
        raise HTTPException(400, "名称不能为空，且不超过 60 个字符")
    if not SSH_HOST_RE.match(d["host"]) or d["host"].startswith("-"):
        raise HTTPException(400, "主机格式无效（填主机名、IP 或 ~/.ssh/config 里的 Host 别名）")
    if not SSH_USER_RE.match(d["user"]) or d["user"].startswith("-"):
        raise HTTPException(400, "用户名格式无效")
    if not (0 <= d["port"] <= 65535):
        raise HTTPException(400, "端口应在 1–65535 之间（留空用默认）")
    if not SSH_JUMP_RE.match(d["proxy_jump"]) or d["proxy_jump"].startswith("-"):
        raise HTTPException(400, "跳板机格式无效（如 user@bastion:22，多个用逗号分隔）")
    sp = d["socks_proxy"]
    for pre in ("socks5h://", "socks5://"):
        sp = sp[len(pre):] if sp.lower().startswith(pre) else sp
    if sp and (not SOCKS_RE.match(sp) or not 0 < int(sp.rsplit(":", 1)[1]) <= 65535):
        raise HTTPException(400, "SOCKS5 代理格式无效，应为 主机:端口（如 127.0.0.1:1080）")
    if sp and d["proxy_jump"]:
        raise HTTPException(400, "跳板机和 SOCKS5 代理只能二选一")
    d["socks_proxy"] = sp
    if d["identity_file"]:
        f = Path(d["identity_file"]).expanduser()
        if not f.is_file():
            raise HTTPException(400, f"密钥文件不存在：{d['identity_file']}")
        d["identity_file"] = str(f)
    if "\n" in d["remote_dir"] or len(d["remote_dir"]) > 500:
        raise HTTPException(400, "远程目录格式无效")
    for k in ("remote_claude", "remote_codex"):
        if "\n" in d[k] or len(d[k]) > 500:
            raise HTTPException(400, "CLI 路径格式无效")
    d["note"] = d["note"][:500]
    d["share_with_ai"] = int(d["share_with_ai"])
    d["allow_root_bypass"] = int(d["allow_root_bypass"])
    return d, (password if d["auth"] == "password" else None)


def get_ssh(project: str, cid: int) -> dict:
    c = q("SELECT * FROM ssh_conns WHERE id=? AND project=?", (cid, project), one=True)
    if not c:
        raise HTTPException(404, "连接不存在")
    return c


@app.get("/api/ssh/config-hosts")
def list_ssh_config_hosts():
    return ssh_config_hosts()


@app.get("/api/projects/{name}/ssh")
def list_ssh(name: str):
    project_path(name)
    return ssh_list(name)


@app.post("/api/projects/{name}/ssh")
def create_ssh(name: str, body: SshIn):
    project_path(name)
    d, password = check_ssh(body)
    if d["auth"] == "password" and not password:
        raise HTTPException(400, "请填写密码")
    now = time.time()
    try:
        cid = ex(f"INSERT INTO ssh_conns(project, {', '.join(d)}, created_at, updated_at) "
                 f"VALUES(?, {', '.join('?' * len(d))}, ?, ?)", (name, *d.values(), now, now))
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"已有同名连接：{d['name']}")
    if password:
        try:
            keychain_set(cid, password)
        except Exception:
            ex("DELETE FROM ssh_conns WHERE id=?", (cid,))
            raise
    return ssh_list(name)


@app.put("/api/projects/{name}/ssh/{cid}")
def update_ssh(name: str, cid: int, body: SshIn):
    old = get_ssh(name, cid)
    d, password = check_ssh(body)
    if d["auth"] == "password" and not password and not keychain_has(cid):
        raise HTTPException(400, "请填写密码")
    try:
        ex(f"UPDATE ssh_conns SET {', '.join(f'{k}=?' for k in d)}, updated_at=? WHERE id=?",
           (*d.values(), time.time(), cid))
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"已有同名连接：{d['name']}")
    if password:
        keychain_set(cid, password)
    elif d["auth"] == "key":  # 改回密钥登录：不再保留密码
        keychain_del(cid)
    new = get_ssh(name, cid)
    if any(old[k] != new[k] for k in ("host", "port", "user", "identity_file", "proxy_jump", "socks_proxy", "auth")) or password:
        ssh_disconnect_master(old)  # 连接参数变了：旧的主连接作废
    return ssh_list(name)


@app.delete("/api/projects/{name}/ssh/{cid}")
def delete_ssh(name: str, cid: int):
    c = get_ssh(name, cid)
    if q("SELECT 1 FROM tasks WHERE ssh_id=? AND status IN ('running','queued')", (cid,), one=True):
        raise HTTPException(409, "有任务正在这台服务器上执行，先停止或断开连接")
    ssh_disconnect_master(c)
    ex("DELETE FROM ssh_conns WHERE id=?", (cid,))
    keychain_del(cid)
    return ssh_list(name)


@app.post("/api/projects/{name}/ssh/{cid}/test")
async def test_ssh(name: str, cid: int):
    """非交互地连一次（不会弹密码、不会自动信任新主机指纹），顺带检查服务器上有没有 claude / codex。"""
    c = get_ssh(name, cid)
    probe = ("echo wb-ok && " + remote_shell(
        f"cd {remote_dir_expr(c['remote_dir'])} && echo wb-dir; "
        f"for b in claude:{shlex.quote(c['remote_claude'] or 'claude')} codex:{shlex.quote(c['remote_codex'] or 'codex')}; do "
        "n=${b%%:*}; p=$(command -v \"${b#*:}\" 2>/dev/null) && echo \"wb-has:$n:$p\"; done; true"))
    t0 = time.time()
    try:
        code, out, err = await ssh_run(c, probe, timeout=30)
    except RuntimeError:
        return {"ok": False, "ms": int((time.time() - t0) * 1000), "message": "连接超时"}
    ms = int((time.time() - t0) * 1000)
    lines = out.splitlines()
    if "wb-ok" not in lines:
        msg = [m for m in err.strip().splitlines()
               if not m.startswith(("Warning: Permanently added", "** ")) and not any(n in m for n in SHELL_NOISE)]
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


@app.post("/api/projects/{name}/ssh/{cid}/connect")
async def connect_ssh(name: str, cid: int):
    c = get_ssh(name, cid)
    try:
        await ssh_connect(c)
    except RuntimeError as e:
        raise HTTPException(502, f"连接失败：{e}")
    return ssh_list(name)


@app.post("/api/projects/{name}/ssh/{cid}/disconnect")
async def disconnect_ssh(name: str, cid: int):
    """断开：先停止正在这台服务器上执行的任务（连同服务器上的 CLI 进程），再关闭主连接。"""
    c = get_ssh(name, cid)
    for t in q("SELECT id FROM tasks WHERE ssh_id=? AND status IN ('running','queued')", (cid,)):
        proc = runner.procs.get(t["id"])
        if isinstance(proc, asyncio.subprocess.Process) and getattr(proc, "_wb_remote", None):
            await remote_kill(*proc._wb_remote)  # 趁主连接还在，先结束服务器上的进程
        try:
            runner.cancel(t["id"])
        except HTTPException:
            pass
    await asyncio.to_thread(ssh_disconnect_master, c)
    return ssh_list(name)


@app.post("/api/projects/{name}/ssh/{cid}/open")
def open_ssh(name: str, cid: int):
    """在 macOS 终端里打开这个连接。"""
    c = get_ssh(name, cid)
    if os.uname().sysname != "Darwin":
        raise HTTPException(400, "只支持 macOS，请复制命令到终端执行")
    cmd = ssh_command(c).replace("\\", "\\\\").replace('"', '\\"')
    r = subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script "{cmd}"',
                        "-e", 'tell application "Terminal" to activate'], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise HTTPException(500, f"打开终端失败：{r.stderr.strip()}")
    return {"ok": True}


TASK_SELECT = ("SELECT t.*, COALESCE(s.name, CASE WHEN t.ssh_id THEN '已删除的连接' ELSE '' END) ssh_name "
               "FROM tasks t LEFT JOIN ssh_conns s ON s.id=t.ssh_id")


@app.get("/api/tasks")
def list_tasks(project: Optional[str] = None):
    if project:
        return q(TASK_SELECT + " WHERE t.project=? ORDER BY t.id DESC", (project,))
    return q(TASK_SELECT + " ORDER BY t.id DESC")


@app.post("/api/tasks")
async def create_task(body: TaskIn):
    project_path(body.project)
    if body.engine not in ENGINES:
        raise HTTPException(400, "engine 必须是 claude 或 codex")
    if not body.prompt.strip():
        raise HTTPException(400, "内容不能为空")
    if not EFFORT_RE.match(body.effort):
        raise HTTPException(400, "思考等级无效")
    images = check_uploads(body.images)
    if body.ssh_id:
        if not q("SELECT 1 FROM ssh_conns WHERE id=? AND project=?", (body.ssh_id, body.project), one=True):
            raise HTTPException(400, "SSH 连接不存在或不属于该项目")
        body.use_worktree = False  # 远程任务直接在远程目录里执行
    now = time.time()
    tid = ex("""INSERT INTO tasks(project, kind, title, description, priority, permission_mode, model,
                effort, use_worktree, ssh_id, engine, images, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (body.project, "task", body.title.strip() or derive_title(body.prompt), body.prompt, "",
              body.permission_mode, body.model, body.effort, int(body.use_worktree), body.ssh_id, body.engine,
              json.dumps(images), now, now))
    if body.run_now:
        await runner.start(tid)
    return q("SELECT * FROM tasks WHERE id=?", (tid,), one=True)


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int):
    t = q(TASK_SELECT + " WHERE t.id=?", (task_id,), one=True)
    if not t:
        raise HTTPException(404, "任务不存在")
    t["image_urls"] = [upload_url(i) for i in json.loads(t.get("images") or "[]")]
    t["runs"] = q("SELECT id, status, exit_code, cost, started_at, ended_at, substr(prompt,1,200) prompt "
                  "FROM runs WHERE task_id=? ORDER BY id", (task_id,))
    resume = "codex resume" if t.get("engine") == "codex" else "claude --resume"
    t["resume_cmd"] = (f"cd {t['workdir']} && {resume} {t['session_id']}"
                       if t["session_id"] and t["workdir"] else "")
    if t["ssh_id"]:  # 远程任务：工作目录在服务器上
        try:
            c = task_conn(t)
            t["ssh_target"] = f"{c['user'] + '@' if c['user'] else ''}{c['host']}"
            t["ssh_connected"] = ssh_connected(c)
            t["resume_cmd"] = remote_resume_cmd(c, t.get("engine") or "claude", t["session_id"]) if t["session_id"] else ""
        except RuntimeError:
            t["resume_cmd"] = ""
        return t
    if t["workdir"] and Path(t["workdir"]).is_dir() and (Path(t["workdir"]) / ".git").exists():
        t["git_status"] = git(Path(t["workdir"]), "status", "--short")
    return t


@app.patch("/api/tasks/{task_id}")
def patch_task(task_id: int, body: TaskPatch):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "effort" in fields and not EFFORT_RE.match(fields["effort"]):
        raise HTTPException(400, "思考等级无效")
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        ex(f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?", (*fields.values(), time.time(), task_id))
    return get_task(task_id)


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    if runner.is_running(task_id):
        raise HTTPException(409, "任务正在执行，先停止")
    t = q("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
    if t and t["branch"] and t["workdir"]:
        # 移除 worktree，但保留分支，避免丢失代码
        subprocess.run(["git", "worktree", "remove", "--force", t["workdir"]],
                       cwd=project_path(t["project"]), capture_output=True)
    imgs = json.loads((t or {}).get("images") or "[]")
    for r in q("SELECT id, images FROM runs WHERE task_id=?", (task_id,)):
        run_log(r["id"]).unlink(missing_ok=True)
        imgs += json.loads(r["images"] or "[]")
    for i in set(imgs):
        if Path(i).resolve().parent == UPLOADS_DIR.resolve():
            Path(i).unlink(missing_ok=True)
    ex("DELETE FROM runs WHERE task_id=?", (task_id,))
    ex("DELETE FROM tasks WHERE id=?", (task_id,))
    return {"ok": True}


@app.post("/api/tasks/{task_id}/run")
async def run_task(task_id: int, body: RunIn):
    if runner.is_running(task_id):
        raise HTTPException(409, "任务正在执行中")
    fields = {k: v for k in ("model", "effort", "permission_mode") if (v := getattr(body, k)) is not None}
    if "effort" in fields and not EFFORT_RE.match(fields["effort"]):
        raise HTTPException(400, "思考等级无效")
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        ex(f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?", (*fields.values(), time.time(), task_id))
    images = check_uploads(body.images or [])
    t = q("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
    if t:
        check_remote_bypass(t)  # 在回退历史之前检查，避免被拦下时已经丢了记录
    if body.edit_run_id is not None:
        if not (body.message or "").strip():
            raise HTTPException(400, "内容不能为空")
        if body.images is None:  # 未指定则沿用被编辑消息原来的图片
            old = q("SELECT images FROM runs WHERE id=? AND task_id=?", (body.edit_run_id, task_id), one=True)
            images = json.loads((old or {}).get("images") or "[]")
        if await rewind(task_id, body.edit_run_id, body.message):
            # 从头开始，新内容已写入任务描述，图片也改为任务首条消息的图片
            ex("UPDATE tasks SET images=? WHERE id=?", (json.dumps(images), task_id))
            return {"run_id": await runner.start(task_id, None)}
    run_id = await runner.start(task_id, body.message, images)
    return {"run_id": run_id}


def run_events_of(run_id: int):
    log = run_log(run_id)
    if not log.exists():
        return
    with open(log, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass


async def rewind(task_id: int, edit_run_id: int, message: str) -> bool:
    """把会话回退到 edit_run_id 这条消息之前：之后由 runner.start 以新消息继续。

    Claude：--resume <sid> --resume-session-at <上一条消息 uuid> --fork-session（原会话不动）。
    Codex：app-server thread/fork 到上一轮（lastTurnId），得到新会话后 exec resume。
    被编辑的消息若是第一条，则直接开新会话并把它作为任务描述。返回 True 表示从头开始。
    """
    t = q("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t["status"] in ("running", "queued"):
        raise HTTPException(409, "任务正在执行中")
    runs = q("SELECT id FROM runs WHERE task_id=? ORDER BY id", (task_id,))
    if edit_run_id not in [r["id"] for r in runs]:
        raise HTTPException(404, "要编辑的消息不存在")
    earlier = [r["id"] for r in runs if r["id"] < edit_run_id]
    new_sid, fork_at = "", ""
    if t["session_id"] and earlier:
        if t.get("engine") == "codex" and t.get("ssh_id"):
            raise HTTPException(400, "远程 Codex 任务暂不支持编辑历史消息（会话在服务器上）")
        if t.get("engine") == "codex":
            n_turns = sum(any(e.get("type") == "turn.started" for e in run_events_of(rid)) for rid in earlier)
            if n_turns:
                try:
                    page = (await codex_rpc([("thread/turns/list", {"threadId": t["session_id"], "sortDirection": "asc",
                                                                   "limit": 1000})]))[0]
                    turns = page.get("data") or []
                    if len(turns) < n_turns:
                        raise RuntimeError(f"会话只有 {len(turns)} 轮，找不到第 {n_turns} 轮")
                    fork = (await codex_rpc([("thread/fork", {"threadId": t["session_id"],
                                                              "lastTurnId": turns[n_turns - 1]["id"],
                                                              "excludeTurns": True})]))[0]
                    new_sid = fork["thread"]["id"]
                except Exception as e:  # noqa
                    raise HTTPException(500, f"回退 Codex 会话失败：{e}")
        else:
            for rid in reversed(earlier):
                uuids = [e["uuid"] for e in run_events_of(rid)
                         if e.get("type") in ("assistant", "user") and e.get("uuid")]
                if uuids:
                    new_sid, fork_at = t["session_id"], uuids[-1]
                    break
    # 删掉被编辑的这条及之后的记录
    for r in runs:
        if r["id"] >= edit_run_id:
            run_log(r["id"]).unlink(missing_ok=True)
            ex("DELETE FROM runs WHERE id=?", (r["id"],))
    if new_sid:
        ex("UPDATE tasks SET session_id=?, fork_at=?, updated_at=? WHERE id=?", (new_sid, fork_at, time.time(), task_id))
        return False
    else:  # 从头开始：新消息就是任务描述
        ex("UPDATE tasks SET session_id='', fork_at='', description=?, title=?, updated_at=? WHERE id=?",
           (message, derive_title(message), time.time(), task_id))
    return True


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int):
    runner.cancel(task_id)
    return {"ok": True}


# 让 AI 回复里引用的本地文件（图片、SVG、下载链接）能在页面里显示
FILE_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox",
                "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}


@app.get("/api/tasks/{task_id}/file")
def task_file(task_id: int, path: str):
    t = q("SELECT project, workdir, ssh_id FROM tasks WHERE id=?", (task_id,), one=True)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t["ssh_id"]:
        raise HTTPException(404, "远程任务的文件在服务器上，无法在这里预览")
    roots = [Path(p).resolve() for p in (t["workdir"], str(project_path(t["project"])), str(UPLOADS_DIR)) if p]
    raw = path[7:] if path.startswith("file://") else path
    cand = Path(raw).expanduser()
    targets = [cand] if cand.is_absolute() else [r / cand for r in roots]
    for f in targets:
        f = f.resolve()
        if f.is_file() and any(f.is_relative_to(r) for r in roots):
            return FileResponse(f, headers=FILE_HEADERS)
    raise HTTPException(404, "文件不存在或不在任务目录内")


IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
MAX_UPLOAD = 20 * 1024 * 1024


def upload_url(path: str) -> str:
    return f"/api/uploads/{Path(path).name}"


def check_uploads(paths: list[str]) -> list[str]:
    out = []
    for p in paths:
        f = Path(p).resolve()
        if f.parent != UPLOADS_DIR.resolve() or not f.is_file():
            raise HTTPException(400, f"图片不存在：{p}")
        out.append(str(f))
    return out


@app.post("/api/uploads")
async def upload(request: Request, name: str = "image.png"):
    """上传图片：请求体为文件原始字节，返回服务器上的路径（随消息一起提交）。"""
    ext = Path(name).suffix.lower().lstrip(".")
    if ext not in IMAGE_TYPES:
        ext = {v: k for k, v in IMAGE_TYPES.items()}.get(request.headers.get("content-type", ""), "")
    if ext not in IMAGE_TYPES:
        raise HTTPException(400, "只支持 png / jpg / gif / webp 图片")
    data = await request.body()
    if not data:
        raise HTTPException(400, "文件为空")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "图片不能超过 20MB")
    f = UPLOADS_DIR / f"{uuid.uuid4().hex}.{ext}"
    f.write_bytes(data)
    return {"path": str(f), "url": upload_url(str(f)), "name": name, "size": len(data)}


@app.get("/api/uploads/{name}")
def get_upload(name: str):
    f = (UPLOADS_DIR / name).resolve()
    if f.parent != UPLOADS_DIR.resolve() or not f.is_file() or f.suffix.lstrip(".") not in IMAGE_TYPES:
        raise HTTPException(404, "图片不存在")
    return FileResponse(f, media_type=IMAGE_TYPES[f.suffix.lstrip(".")], headers=FILE_HEADERS)


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: int, offset: int = 0):
    """增量读取运行日志：offset 为字节偏移，返回新事件与下一个 offset。"""
    log = run_log(run_id)
    r = q("SELECT status FROM runs WHERE id=?", (run_id,), one=True)
    if not r:
        raise HTTPException(404, "run 不存在")
    events = []
    if log.exists():
        with open(log, "rb") as f:
            f.seek(offset)
            data = f.read()
        # 只消费完整的行
        end = data.rfind(b"\n") + 1
        for line in data[:end].splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        offset += end
    return {"events": events, "offset": offset, "status": r["status"]}


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
