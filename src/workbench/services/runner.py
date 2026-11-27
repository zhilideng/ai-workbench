"""运行器：为任务启动 CLI 进程（本机或经 SSH 在服务器上），把事件流写入运行日志，结束后更新状态。

并发受 Settings.max_concurrent 限制，超出的任务排队。引擎相关的细节全部委托给 engines。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from workbench.core.errors import BadRequest, Conflict, NotFound
from workbench.core.settings import get_settings
from workbench.core.shell import child_env
from workbench.db.repositories import RunRepo, TaskRepo
from workbench.engines import REGISTRY, CommandSpec, RunState, get_engine
from workbench.engines.base import ServerUnavailable, TurnHandle
from workbench.engines.claude import ROOT_BYPASS_HINT
from workbench.engines.progress import event_progress
from workbench.services import connections, runlog, uploads
from workbench.services.agent_cards import AgentCards, reconcile_codex_agents
from workbench.services.projects import project_path
from workbench.ssh import commands as cmds
from workbench.ssh import transport

DEFAULT_IMAGE_PROMPT = "请查看附带的图片。"
HANDOFF_TURN_CHARS = 4000     # 交接给新会话时，每条历史消息 / 回复最多保留的字数
HANDOFF_TOTAL_CHARS = 40000   # 交接内容的总上限，超出时丢弃最早的轮次
log = logging.getLogger(__name__)


@asynccontextmanager
async def _remote_step(run_id: int, step: str):
    runlog.append_wb(run_id, "remote_step", step=step, state="running")
    state = "failed"
    try:
        yield
    except asyncio.CancelledError:
        state = "interrupted"
        raise
    else:
        state = "done"
    finally:
        runlog.append_wb(run_id, "remote_step", step=step, state=state)


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…（已截断）"


def handoff_prompt(runs: list[dict], message: str) -> str:
    """没有可续接的会话（如切换了引擎）但已有对话记录时：把此前的对话作为上下文交给新会话。"""
    turns, total = [], 0
    for r in reversed(runs):
        turn = f"【用户】\n{_clip(r['prompt'], HANDOFF_TURN_CHARS)}\n\n【助手】\n{_clip(r.get('result') or '（无回复）', HANDOFF_TURN_CHARS)}"
        if turns and total + len(turn) > HANDOFF_TOTAL_CHARS:
            break
        turns.insert(0, turn)
        total += len(turn)
    if len(turns) < len(runs):
        turns.insert(0, f"（更早的 {len(runs) - len(turns)} 轮已省略）")
    return ("以下是这个任务此前的对话记录（由另一个会话完成，文件改动已在工作目录中），请在此基础上继续：\n\n"
            + "\n\n---\n\n".join(turns) + f"\n\n===\n\n用户的新消息：\n{message}")


@dataclass
class Job:
    """一个正在排队或执行的任务。"""
    task_id: int
    run_id: int
    proc: Optional[asyncio.subprocess.Process] = None
    remote: Optional[dict] = None        # 远程任务的 SSH 连接
    handle: Optional[TurnHandle] = None  # 在常驻进程里执行时：中断当前轮次、回复审批
    interrupted: bool = False
    cancelled: bool = False
    aio_task: Optional[asyncio.Task] = field(default=None, repr=False)


def ensure_workdir(task: dict) -> Path:
    """本机任务的工作目录：项目目录，或（勾选 worktree 时）data/worktrees/<项目>/task-N。"""
    # 已有目录可能包含旧会话尚未提交的改动，续接时沿用。
    if task["workdir"] and Path(task["workdir"]).is_dir():
        return Path(task["workdir"])
    proj = Path(task["source_path"]) if task.get("source_path") else project_path(task["project"])
    if not proj.is_dir():
        raise RuntimeError(f"源文件夹不存在：{proj}")
    if task.get("parallel_group_id"):
        TaskRepo.update(task["id"], touch=False, workdir=str(proj), use_worktree=0, branch="")
        return proj
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
        self.agent_results: asyncio.Task | None = None
        self.stopping = False

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
        if task["agent_id"]:
            raise Conflict("子会话由 CLI 管理")
        check_remote_bypass(task)
        resume = bool(message and task["session_id"])
        history = [] if resume or not message else RunRepo.of_task(task_id)
        if resume or history:  # 有会话就续接；没有会话但有对话记录时，把记录交接给新会话
            prompt = message
        else:
            prompt = (task["description"] or task["title"]) + (f"\n\n补充说明：\n{message}" if message else "")
        cli_prompt = handoff_prompt(history, prompt) if history else prompt
        if not resume:  # 新会话：带上任务首条消息的图片
            images = TaskRepo.images(task) + (images or [])
        images = [i for i in (images or []) if Path(i).is_file()]
        run_id = RunRepo.enqueue(task_id, prompt, images)
        job = self.jobs[task_id] = Job(task_id, run_id)
        job.aio_task = asyncio.create_task(self._run(job, task, cli_prompt, task["session_id"] if resume else None, images,
                                                     shown=prompt))
        return run_id

    async def _spawn(self, job: Job, task: dict, prompt: str, resume_sid: Optional[str], images: list[str],
                     shown: str):
        """准备命令并启动进程，返回 (进程, 引擎, 是否远程)。"""
        engine = get_engine(task.get("engine"))
        conn = connections.of_task(task)
        TaskRepo.update(job.task_id, status="running")
        ssh_ctx = ""
        if conn:  # 远程：ssh 到服务器，在远程目录里跑服务器上的 CLI
            job.remote = conn
            cwd_label = f"{conn['name']}:{conn['remote_dir'] or '~'}"
            TaskRepo.update(job.task_id, touch=False, workdir=conn["remote_dir"] or "~")
            async with _remote_step(job.run_id, "connect"):
                await transport.connect(conn)  # 未连接时自动连接；之后上传和执行都复用这条连接
            rimgs, rdir = [], None
            if images:
                async with _remote_step(job.run_id, "upload"):
                    rimgs, rdir = await transport.upload_images(conn, images)
            # 远程任务不附带本机的 SSH 连接说明（命令里的 askpass 路径在服务器上不存在）
            cli = engine.build_cmd(CommandSpec(task, prompt, resume_sid, rimgs, rdir, "", engine.remote_bin(conn)))
            argv, env = transport.exec_argv(conn, cmds.remote_run_cmd(conn, cli, job.run_id, engine.remote_env(task, conn)))
            workdir = get_settings().paths.data
        else:
            workdir = ensure_workdir(TaskRepo.get(job.task_id))
            cwd_label = str(workdir)
            ssh_ctx = connections.ai_context(task["project"])
            lines = await self._live_turn(job, engine, task, prompt, resume_sid, images, str(workdir), ssh_ctx)
            if lines is not None:   # 常驻进程（codex app-server）：不启动新进程
                self._log_start(job, engine, task, cwd_label, None, resume_sid, images, shown, prompt,
                                "codex app-server（常驻进程）" if engine.name == "codex"
                                else f"{engine.name} 常驻会话（stream-json）")
                return lines, None, engine, False
            cli = argv = engine.build_cmd(CommandSpec(task, prompt, resume_sid, images, str(uploads.uploads_dir()), ssh_ctx))
            env = child_env()
        if job.cancelled:  # 上传图片期间被停止
            raise asyncio.CancelledError
        self._log_start(job, engine, task, cwd_label, conn, resume_sid, images, shown, prompt,
                        (f"[ssh {conn['name']}] " if conn else "") + " ".join(engine.mask_prompt(cli)))
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=workdir, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024, start_new_session=True)
        job.proc = proc
        RunRepo.set_pid(job.run_id, proc.pid)
        return self._proc_lines(proc), proc, engine, bool(conn)

    @staticmethod
    def _log_start(job: Job, engine, task: dict, cwd_label: str, conn: Optional[dict], resume_sid: Optional[str],
                   images: list[str], shown: str, prompt: str, cmd: str) -> None:
        runlog.append_wb(job.run_id, "start", cwd=cwd_label, remote=conn["name"] if conn else "",
                         engine=engine.name, model=task["model"] or "", effort=task.get("effort") or "",
                         resume=bool(resume_sid), images=[uploads.url_of(i) for i in images],
                         cmd=cmd, prompt=shown, handoff=prompt != shown)

    @staticmethod
    async def _proc_lines(proc):
        async for raw in proc.stdout:
            yield raw.decode(errors="replace")

    async def _live_turn(self, job: Job, engine, task: dict, prompt: str, resume_sid: Optional[str],
                         images: list[str], cwd: str, instructions: str):
        """在引擎的常驻进程里开始一轮，返回事件行的异步迭代器；不支持或启动失败时返回 None（改用启动 CLI）。"""
        server = engine.live_server(task)
        if server is None:
            return None
        job.handle = TurnHandle()
        gen = server.turn(task, prompt, resume_sid, images, cwd, instructions, job.handle,
                          uploads_dir=str(uploads.uploads_dir()))
        try:
            first = await gen.__anext__()   # 启动进程、建立 / 恢复会话；失败在这里暴露
        except ServerUnavailable as e:
            job.handle = None
            runlog.append_wb(job.run_id, "stderr", text=f"{e}，改为启动 {engine.name} exec\n")
            return None

        async def lines():
            try:
                yield first
                async for line in gen:
                    yield line
            finally:
                await gen.aclose()   # 提前结束时也释放会话，避免它一直处于执行中
        return lines()

    def _interrupt(self, job: Job) -> None:
        """停止常驻进程里的当前轮次（轮次还没开始时，等开始后由 _run 再调用）。"""
        if job.handle and job.handle.interrupt and not job.interrupted:
            job.interrupted = True
            asyncio.create_task(job.handle.interrupt())

    def decide(self, task_id: int, approval_id: str, decision: str, run_id: Optional[int] = None) -> None:
        """回复常驻进程里的审批请求（批准 / 本会话都批准 / 拒绝）。"""
        job = self.jobs.get(task_id)
        if job and (job.cancelled or (run_id is not None and job.run_id != run_id)):
            raise Conflict("这个审批已失效")
        if not job or not job.handle or not job.handle.decide or not job.handle.decide(approval_id, decision):
            raise Conflict("这个审批已处理或已失效")

    async def _pump_stderr(self, proc, run_id: int, remote: bool) -> None:
        async for line in proc.stderr:
            if line.startswith(b"Reading additional input from stdin"):
                continue
            if remote and any(n.encode() in line for n in transport.SHELL_NOISE):
                continue
            if remote and b"cannot be used with root/sudo privileges" in line:
                line += ("\n" + ROOT_BYPASS_HINT).encode()
            runlog.append_wb(run_id, "stderr", text=line.decode(errors="replace"))

    async def _run(self, job: Job, task: dict, prompt: str, resume_sid: Optional[str], images: list[str],
                   shown: str = "") -> None:
        status, result, cost, code = "error", "", 0.0, None
        lines = None
        last_progress = None
        agents = None
        try:
            async with self.sem:
                if job.cancelled:
                    raise asyncio.CancelledError
                lines, proc, engine, remote = await self._spawn(job, task, prompt, resume_sid, images, shown or prompt)
                err_task = asyncio.create_task(self._pump_stderr(proc, job.run_id, remote)) if proc else None
                st = RunState()
                agents = AgentCards(TaskRepo.get(job.task_id), engine.name)
                with open(runlog.path(job.run_id), "a", encoding="utf-8") as f:
                    async for raw in lines:
                        if job.cancelled:
                            self._interrupt(job)
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            ev = None
                        if isinstance(ev, dict):
                            try:
                                if agents.record(ev):
                                    continue
                            except Exception as e:  # noqa: BLE001
                                log.warning("任务 %s 的子会话事件记录失败 (%s)", job.task_id, type(e).__name__)
                        if isinstance(ev, dict) and ev.get("type") == "stream_event":
                            # 逐字输出：只保留正文和思考的增量，写成精简事件，避免日志被大量分片撑大；
                            # 完整内容随后由 assistant 消息原样写入
                            delta = (ev.get("event") or {}).get("delta") or {}
                            kind = {"text_delta": ("delta", "text"), "thinking_delta": ("thinking_delta", "thinking")}.get(
                                delta.get("type"))
                            if kind and delta.get(kind[1]) and not ev.get("parent_tool_use_id"):   # 子代理不逐字显示
                                runlog.write_event(f, {"type": "wb", "subtype": kind[0], "text": delta[kind[1]]})
                            continue
                        if not isinstance(ev, dict):
                            f.write(line + "\n")
                            f.flush()
                            continue
                        runlog.write_event(f, ev)
                        old_sid = st.sid
                        engine.parse_event(ev, st)
                        progress = event_progress(engine.name, ev)
                        if progress is not None and progress != last_progress:
                            last_progress = progress
                            TaskRepo.update(job.task_id, touch=False, progress=progress[0], progress_label=progress[1])
                        if st.sid and st.sid != old_sid:
                            TaskRepo.update(job.task_id, touch=False, session_id=st.sid, fork_at="")
                result, cost, status = st.result, st.cost, st.status or "error"
                if proc:
                    code = await proc.wait()
                    await err_task
                    if code != 0 and status == "success":
                        status = "error"
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as e:  # noqa: BLE001
            runlog.append_wb(job.run_id, "error", text=str(e))
        finally:
            if lines is not None:
                await lines.aclose()
            if job.cancelled:
                status = "cancelled"
            if agents is not None:
                try:
                    agents.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("任务 %s 的子会话日志收尾失败 (%s)", job.task_id, type(e).__name__)
            self.jobs.pop(job.task_id, None)
            RunRepo.finish(job.run_id, status, code, cost, result)
            TaskRepo.add_cost(job.task_id, {"success": "review", "cancelled": "cancelled"}.get(status, "failed"), cost)
            runlog.append_wb(job.run_id, "end", status=status, exit_code=code)
            if agents is not None and agents.engine == "codex" and not task.get("ssh_id"):
                self._check_agent_results()

    def _check_agent_results(self) -> None:
        if self.stopping or (self.agent_results and not self.agent_results.done()):
            return
        if get_engine("codex").available() and RunRepo.unreported_codex_agents():
            self.agent_results = asyncio.create_task(self._recover_agent_results())

    @staticmethod
    async def _recover_agent_results() -> None:
        try:
            await reconcile_codex_agents()
        except Exception as e:  # noqa: BLE001
            log.warning("读取 Codex 子会话结果失败 (%s)", type(e).__name__)

    # ---- 停止
    def cancel(self, task_id: int) -> None:
        job = self.jobs.get(task_id)
        if not job:
            raise Conflict("任务未在执行")
        job.cancelled = True
        if job.handle:  # 常驻进程里执行：中断当前轮次，不结束进程
            self._interrupt(job)
            return
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

    async def warm(self) -> None:
        """桌面应用启动时：提前拉起已安装引擎的常驻进程（失败不影响使用，发消息时会重试）。"""
        for engine in REGISTRY.values():
            if engine.available():
                try:
                    await engine.warm()
                except Exception:  # noqa: BLE001
                    pass
        self._check_agent_results()

    async def shutdown(self) -> None:
        self.stopping = True
        if self.agent_results is not None:
            self.agent_results.cancel()
            await asyncio.gather(self.agent_results, return_exceptions=True)
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
        for engine in REGISTRY.values():   # 关闭常驻进程（如 codex app-server）
            await engine.close()
