"""拼 ssh 命令与远程 shell 命令。全部是纯函数（c 为 ssh_conns 的一行）。"""
from __future__ import annotations

import shlex
from typing import Optional

from workbench.ssh.keychain import askpass_path

REMOTE_UPLOADS = ".workbench/uploads"   # 相对远程 home
REMOTE_RUNS = ".workbench/runs"         # 远程进程的 pid 文件，用于停止任务


def target(c: dict, with_port: bool = True) -> str:
    s = f"{c['user'] + '@' if c.get('user') else ''}{c['host']}"
    return s + (f":{c['port']}" if with_port and c.get("port") else "")


def env(c: dict) -> dict:
    """密码登录时 ssh 需要的环境变量（不含密码本身）。"""
    if c.get("auth") != "password":
        return {}
    return {"SSH_ASKPASS": str(askpass_path()), "SSH_ASKPASS_REQUIRE": "force", "WB_SSH_ID": str(c["id"])}


def args(c: dict) -> list[str]:
    """连接参数（不含 ssh 本身和远程命令）。"""
    a: list[str] = []
    if c.get("auth") == "password":
        a += ["-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=keyboard-interactive,password"]
    if c.get("port"):
        a += ["-p", str(c["port"])]
    if c.get("identity_file"):
        a += ["-i", c["identity_file"]]
    if c.get("proxy_jump"):
        a += ["-J", c["proxy_jump"]]
    if c.get("socks_proxy"):
        a += ["-o", f"ProxyCommand=nc -X 5 -x {c['socks_proxy']} %h %p"]
    return a + [target(c, with_port=False)]


def auth_opts(c: dict) -> list[str]:
    # 密码登录不能开 BatchMode（会禁用 askpass）；改为严格校验主机指纹、只尝试一次密码
    return (["-o", "StrictHostKeyChecking=yes", "-o", "NumberOfPasswordPrompts=1"] if c.get("auth") == "password"
            else ["-o", "BatchMode=yes"])


def prefix(c: dict) -> list[str]:
    """命令行开头：密码登录时用 env 带上 askpass 变量，否则就是 ssh。"""
    e = env(c)
    return (["env"] + [f"{k}={v}" for k, v in e.items()] if e else []) + ["ssh"]


def _env_prefix_str(c: dict) -> str:
    pre = " ".join(shlex.quote(a) for a in prefix(c)[:-1])
    return pre + " " if pre else ""


def remote_dir_expr(d: str) -> str:
    """远程目录的 shell 表达式；~ 开头的路径留给远程 shell 展开。空 = home。"""
    d = (d or "").strip() or "~"
    if d == "~":
        return "~"
    if d.startswith("~/"):
        return "~/" + shlex.quote(d[2:])
    return shlex.quote(d)


def interactive_command(c: dict) -> str:
    """可直接粘到终端的命令；配置了远程目录时登录后进入该目录。"""
    base = ["ssh"] + args(c)
    if c.get("remote_dir"):
        cmd = " ".join(shlex.quote(a) for a in ["ssh", "-t"] + base[1:]) + " " + shlex.quote(
            f"cd {remote_dir_expr(c['remote_dir'])} && exec $SHELL -l")
    else:
        cmd = " ".join(shlex.quote(a) for a in base)
    return _env_prefix_str(c) + cmd


def remote_shell(inner: str) -> str:
    """让远程用户的登录 + 交互 shell 执行 inner，这样 .bashrc / .zshrc 里配置的 PATH（nvm 等）都能生效。"""
    return f'exec "${{SHELL:-/bin/sh}}" -lic {shlex.quote(inner)}'


def remote_run_cmd(c: dict, cmd: list[str], run_id: int, extra_env: Optional[dict] = None) -> str:
    """服务器上：进入远程目录，记下 pid（exec 后就是 CLI 本身的 pid），再执行 CLI。"""
    envp = (["env"] + [f"{k}={v}" for k, v in extra_env.items()]) if extra_env else []
    inner = (f"find ~/{REMOTE_RUNS} -name '*.pid' -mtime +7 -delete 2>/dev/null; "
             f"cd {remote_dir_expr(c['remote_dir'])} && mkdir -p ~/{REMOTE_RUNS} && echo $$ > ~/{REMOTE_RUNS}/{run_id}.pid"
             f" && exec {shlex.join(envp + cmd)}")
    return remote_shell(inner)


def remote_kill_cmd(run_id: int) -> str:
    f = f"~/{REMOTE_RUNS}/{run_id}.pid"
    return f'p=$(cat {f} 2>/dev/null) && [ -n "$p" ] && {{ pkill -TERM -P "$p"; kill -TERM "$p"; }}; rm -f {f}'


def resume_in_terminal(c: dict, resume: str) -> str:
    """终端接管：ssh 到服务器，在远程目录里执行 resume（如 `claude --resume <sid>`）。"""
    cmd = " ".join(shlex.quote(a) for a in ["ssh", "-t", *args(c)]) + " " + shlex.quote(
        remote_shell(f"cd {remote_dir_expr(c['remote_dir'])} && {resume}"))
    return _env_prefix_str(c) + cmd


def probe_cmd(c: dict) -> str:
    """连通性测试：检查远程目录是否存在、服务器上有没有 claude / codex。"""
    return ("echo wb-ok && " + remote_shell(
        f"cd {remote_dir_expr(c['remote_dir'])} && echo wb-dir; "
        f"for b in claude:{shlex.quote(c['remote_claude'] or 'claude')} codex:{shlex.quote(c['remote_codex'] or 'codex')}; do "
        "n=${b%%:*}; p=$(command -v \"${b#*:}\" 2>/dev/null) && echo \"wb-has:$n:$p\"; done; true"))
