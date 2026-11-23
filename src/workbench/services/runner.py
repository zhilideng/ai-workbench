"""运行器：为任务启动 CLI 进程（本机或经 SSH 在服务器上），把事件流写入运行日志，结束后更新状态。

并发受 Settings.max_concurrent 限制，超出的任务排队。引擎相关的细节全部委托给 engines。
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from workbench.core.errors import BadRequest, Conflict, NotFound
from workbench.core.settings import get_settings
from workbench.core.shell import child_env
from workbench.db.repositories import RunRepo, TaskRepo
from workbench.engines import CommandSpec, RunState, get_engine
from workbench.engines.claude import ROOT_BYPASS_HINT
from workbench.services import connections, runlog, uploads
from workbench.services.projects import project_path
from workbench.ssh import commands as cmds
from workbench.ssh import transport

DEFAULT_IMAGE_PROMPT = "请查看附带的图片。"


@dataclass
class Job:
    """一个正在排队或执行的任务。"""
    task_id: int
    run_id: int
    proc: Optional[asyncio.subprocess.Process] = None
    remote: Optional[dict] = None        # 远程任务的 SSH 连接
    cancelled: bool = False
    aio_task: Optional[asyncio.Task] = field(default=None, repr=False)


def ensure_workdir(task: dict) -> Path:
    """本机任务的工作目录：项目目录，或（勾选 worktree 时）data/worktrees/<项目>/task-N。"""
    if task["workdir"] and Path(task["workdir"]).is_dir():
        return Path(task["workdir"])
    proj = Path(task["source_path"]) if task.get("source_path") else project_path(task["project"])
    if not proj.is_dir():
        raise RuntimeError(f"源文件夹不存在：{proj}")
    if task["use_worktree"] and (proj / ".git").exists():
        wt = get_settings().paths.worktrees / task["project"] / f"task-{task['id']}"
        branch = f"wb/task-{task['id']}"
        wt.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "worktree", "add", "-b", branch, str(wt)], cwd=proj, capture_output=True, text=True)
        if r.returncode != 0:  # 分支已存在
            r = subprocess.run(["git", "worktree", "add", str(wt), branch], cwd=proj, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"创建 worktree 失败: {r.stderr.strip()}")
        TaskRepo.update(task["id"], touch=False, workdir=str(wt), branch=branch)
        return wt
    TaskRepo.update(task["id"], touch=False, workdir=str(proj))
    return proj


def check_remote_bypass(task: dict) -> None:
    """发送前检查：已知以 root 登录、选了全部放行、又没在连接里允许时，直接说明原因，而不是让 CLI 报错。"""
    if not task.get("ssh_id") or (task.get("engine") or "claude") != "claude" or task.get("permission_mode") != "bypassPermissions":
        return
    try:
        c = connections.of_task(task)
    except RuntimeError as e:
        raise BadRequest(str(e))
    if c["user"] == "root" and not c["allow_root_bypass"]:
        raise BadRequest(ROOT_BYPASS_HINT)


class Runner:
    def __init__(self, max_concurrent: Optional[int] = None):
        self.jobs: dict[int, Job] = {}   # task_id -> Job
        self.sem = asyncio.Semaphore(max_concurrent or get_settings().max_concurrent)

    # ---- 查询
    def is_running(self, task_id: int) -> bool:
        return task_id in self.jobs

    @property
    def active_count(self) -> int:
        return len(self.jobs)

    # ---- 启动
    async def start(self, task_id: int, message: Optional[str] = None, images: Optional[list[str]] = None) -> int:
        task = TaskRepo.get(task_id)
        if not task:
            raise NotFound("任务不存在")
        if self.is_running(task_id) or task["status"] in ("running", "queued"):
            raise Conflict("任务正在执行中")
        check_remote_bypass(task)
        resume = bool(message and task["session_id"])
        prompt = message if resume else ((task["description"] or task["title"]) + (f"\n\n补充说明：\n{message}" if message else ""))
        if not resume:  # 新会话：带上任务首条消息的图片
            images = TaskRepo.images(task) + (images or [])
        images = [i for i in (images or []) if Path(i).is_file()]
        run_id = RunRepo.create(task_id, prompt, images)
        TaskRepo.update(task_id, status="queued")
        job = self.jobs[task_id] = Job(task_id, run_id)
        job.aio_task = asyncio.create_task(self._run(job, task, prompt, task["session_id"] if resume else None, images))
        return run_id

    async def _spawn(self, job: Job, task: dict, prompt: str, resume_sid: Optional[str], images: list[str]):
        """准备命令并启动进程，返回 (进程, 引擎, 是否远程)。"""
        engine = get_engine(task.get("engine"))
        conn = connections.of_task(task)
        TaskRepo.update(job.task_id, status="running")
        ssh_ctx = ""
        if conn:  # 远程：ssh 到服务器，在远程目录里跑服务器上的 CLI
            job.remote = conn
            cwd_label = f"{conn['name']}:{conn['remote_dir'] or '~'}"
            TaskRepo.update(job.task_id, touch=False, workdir=conn["remote_dir"] or "~")
            await transport.connect(conn)  # 未连接时自动连接；之后上传和执行都复用这条连接
            rimgs, rdir = await transport.upload_images(conn, images) if images else ([], None)
            # 远程任务不附带本机的 SSH 连接说明（命令里的 askpass 路径在服务器上不存在）
            cli = engine.build_cmd(CommandSpec(task, prompt, resume_sid, rimgs, rdir, "", engine.remote_bin(conn)))
            argv, env = transport.exec_argv(conn, cmds.remote_run_cmd(conn, cli, job.run_id, engine.remote_env(task, conn)))
            workdir = get_settings().paths.data
        else:
            workdir = ensure_workdir(TaskRepo.get(job.task_id))
            cwd_label = str(workdir)
            ssh_ctx = connections.ai_context(task["project"])
            cli = argv = engine.build_cmd(CommandSpec(task, prompt, resume_sid, images, str(uploads.uploads_dir()), ssh_ctx))
            env = child_env()
        if job.cancelled:  # 上传图片期间被停止
            raise asyncio.CancelledError
        runlog.append_wb(job.run_id, "start", cwd=cwd_label, remote=conn["name"] if conn else "",
                         engine=engine.name, model=task["model"] or "", effort=task.get("effort") or "",
                         resume=bool(resume_sid), images=[uploads.url_of(i) for i in images],
                         cmd=(f"[ssh {conn['name']}] " if conn else "") + " ".join(engine.mask_prompt(cli)),
                         prompt=prompt)
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=workdir, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024, start_new_session=True)
        job.proc = proc
        RunRepo.set_pid(job.run_id, proc.pid)
        return proc, engine, bool(conn)

    async def _pump_stderr(self, proc, run_id: int, remote: bool) -> None:
        async for line in proc.stderr:
            if line.startswith(b"Reading additional input from stdin"):
                continue
            if remote and any(n.encode() in line for n in transport.SHELL_NOISE):
                continue
            if remote and b"cannot be used with root/sudo privileges" in line:
                line += ("\n" + ROOT_BYPASS_HINT).encode()
            runlog.append_wb(run_id, "stderr", text=line.decode(errors="replace"))

    async def _run(self, job: Job, task: dict, prompt: str, resume_sid: Optional[str], images: list[str]) -> None:
        status, result, cost, code = "error", "", 0.0, None
        try:
            async with self.sem:
                if job.cancelled:
                    raise asyncio.CancelledError
                proc, engine, remote = await self._spawn(job, task, prompt, resume_sid, images)
                err_task = asyncio.create_task(self._pump_stderr(proc, job.run_id, remote))
                st = RunState()
                with open(runlog.path(job.run_id), "a", encoding="utf-8") as f:
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
                        old_sid = st.sid
                        engine.parse_event(ev, st)
                        if st.sid and st.sid != old_sid:
                            TaskRepo.update(job.task_id, touch=False, session_id=st.sid, fork_at="")
                result, cost, status = st.result, st.cost, st.status or "error"
                code = await proc.wait()
                await err_task
                if code != 0 and status == "success":
                    status = "error"
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as e:  # noqa: BLE001
            runlog.append_wb(job.run_id, "error", text=str(e))
        finally:
            if job.cancelled:
                status = "cancelled"
            self.jobs.pop(job.task_id, None)
            RunRepo.finish(job.run_id, status, code, cost, result)
            TaskRepo.add_cost(job.task_id, {"success": "review", "cancelled": "cancelled"}.get(status, "failed"), cost)
            runlog.append_wb(job.run_id, "end", status=status, exit_code=code)

    # ---- 停止
    def cancel(self, task_id: int) -> None:
        job = self.jobs.get(task_id)
        if not job:
            raise Conflict("任务未在执行")
        job.cancelled = True
        if job.proc is None:  # 还在排队或准备中
            return
        if job.remote:  # 远程任务：同时停掉服务器上的 CLI 进程
            asyncio.create_task(transport.kill_remote(job.remote, job.run_id))
        try:
            os.killpg(job.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    async def kill_remote_now(self, task_id: int) -> None:
        job = self.jobs.get(task_id)
        if job and job.proc and job.remote:
            await transport.kill_remote(job.remote, job.run_id)

    async def shutdown(self) -> None:
        for job in list(self.jobs.values()):
            job.cancelled = True
            if job.proc is None:
                continue
            if job.remote:
                await transport.kill_remote(job.remote, job.run_id)
            try:
                os.killpg(job.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
