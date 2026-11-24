"""任务：创建、查看、修改、删除、发送消息（含编辑历史消息）、停止、读取任务目录里的文件。"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from workbench.core.errors import BadRequest, Conflict, NotFound, WorkbenchError
from workbench.core.shell import git
from workbench.db.repositories import RunRepo, SshRepo, TaskRepo
from workbench.engines import get_engine
from workbench.services import connections, runlog, uploads
from workbench.services.projects import project_path, project_sources
from workbench.services.runner import Runner, check_remote_bypass
from workbench.ssh import commands as cmds
from workbench.ssh import transport

EFFORT_RE = re.compile(r"^[a-z]{0,16}$")
PATCHABLE = ("title", "description", "priority", "status", "permission_mode", "model", "effort", "kind")


def derive_title(text: str) -> str:
    line = next((l.strip() for l in text.splitlines() if l.strip()), "")
    return line[:60] + ("…" if len(line) > 60 else "")


def _check_effort(effort: Optional[str]) -> None:
    if effort is not None and not EFFORT_RE.match(effort):
        raise BadRequest("思考等级无效")


# ---------------------------------------------------------------- 查询

def list_tasks(project: Optional[str] = None) -> list[dict]:
    return TaskRepo.list(project)


def get_task(task_id: int) -> dict:
    t = TaskRepo.get_view(task_id)
    if not t:
        raise NotFound("任务不存在")
    engine = get_engine(t.get("engine"))
    t["image_urls"] = [uploads.url_of(i) for i in TaskRepo.images(t)]
    t["runs"] = RunRepo.summaries(task_id)
    t["resume_cmd"] = (f"cd {shlex.quote(t['workdir'])} && {engine.resume_command(t['session_id'])}"
                       if t["session_id"] and t["workdir"] else "")
    if t["ssh_id"]:  # 远程任务：工作目录在服务器上
        try:
            c = connections.of_task(t)
            t["ssh_target"] = cmds.target(c, with_port=False)
            t["ssh_project"] = c["project"]
            t["ssh_connected"] = transport.is_connected(c)
            t["resume_cmd"] = (cmds.resume_in_terminal(c, engine.resume_command(t["session_id"], engine.remote_bin(c)))
                               if t["session_id"] else "")
        except RuntimeError:
            t["resume_cmd"] = ""
        return t
    if t["workdir"] and Path(t["workdir"]).is_dir() and (Path(t["workdir"]) / ".git").exists():
        t["git_status"] = git(Path(t["workdir"]), "status", "--short")
    return t


# ---------------------------------------------------------------- 增删改

async def create_task(runner: Runner, *, project: str, prompt: str, title: str = "", engine: str = "claude",
                      permission_mode: str = "acceptEdits", model: str = "", effort: str = "",
                      use_worktree: bool = False, ssh_id: int = 0, run_now: bool = False,
                      images: Optional[list[str]] = None, source_path: str = "") -> dict:
    sources = project_sources(project)
    if source_path and {"path": source_path, "ssh_id": ssh_id} not in sources:
        raise BadRequest("请选择该项目关联的源文件夹")
    if not ssh_id:
        if source_path and not Path(source_path).is_dir():
            raise BadRequest("源文件夹不存在")
        source_path = source_path or str(project_path(project))
    get_engine(engine)
    if not prompt.strip():
        raise BadRequest("内容不能为空")
    _check_effort(effort)
    imgs = uploads.validate(images or [])
    if ssh_id:
        if not SshRepo.get(ssh_id):
            raise BadRequest("SSH 连接不存在")
        use_worktree = False  # 远程任务直接在远程目录里执行
    tid = TaskRepo.create(project=project, kind="task", title=title.strip() or derive_title(prompt), description=prompt,
                          priority="", permission_mode=permission_mode, model=model, effort=effort,
                          use_worktree=int(use_worktree), ssh_id=ssh_id, source_path=source_path,
                          engine=engine, images=json.dumps(imgs))
    if run_now:
        await runner.start(tid)
    return TaskRepo.get(tid)


def _engine_fields(task_id: int, engine: Optional[str]) -> dict:
    """切换引擎：原会话属于旧引擎，无法续接，清空后下一条消息会带着此前的对话记录开新会话。"""
    t = TaskRepo.get(task_id)
    if not t:
        raise NotFound("任务不存在")
    if not engine or engine == (t.get("engine") or "claude"):
        return {}
    get_engine(engine)
    if t["status"] in ("running", "queued"):
        raise Conflict("任务正在执行，先停止再切换引擎")
    return {"engine": engine, "session_id": "", "fork_at": ""}


def patch_task(task_id: int, fields: dict) -> dict:
    engine = fields.get("engine")
    fields = {k: v for k, v in fields.items() if k in PATCHABLE and v is not None}
    _check_effort(fields.get("effort"))
    fields.update(_engine_fields(task_id, engine))
    TaskRepo.update(task_id, **fields)
    return get_task(task_id)


def delete_task(runner: Runner, task_id: int) -> dict:
    if runner.is_running(task_id):
        raise Conflict("任务正在执行，先停止")
    t = TaskRepo.get(task_id)
    if t and t["branch"] and t["workdir"]:
        # 移除 worktree，但保留分支，避免丢失代码
        subprocess.run(["git", "worktree", "remove", "--force", t["workdir"]],
                       cwd=t["source_path"] or project_path(t["project"]), capture_output=True)
    imgs = TaskRepo.images(t or {})
    for r in RunRepo.of_task(task_id):
        runlog.delete(r["id"])
        imgs += TaskRepo.images(r)
    uploads.remove(set(imgs))
    TaskRepo.delete(task_id)
    return {"ok": True}


# ---------------------------------------------------------------- 预热

def prewarm(*, project: str, engine: str = "claude", source_path: str = "", permission_mode: str = "acceptEdits",
            model: str = "", effort: str = "", ssh_id: int = 0, use_worktree: bool = False) -> dict:
    """新建任务的输入框就绪：让引擎的常驻进程按这些设置提前建好会话（与 CLI 打开时就准备好一样）。"""
    task = {"ssh_id": ssh_id, "permission_mode": permission_mode, "model": model, "effort": effort}
    server = get_engine(engine).live_server(task)
    if server is None or ssh_id or use_worktree:   # 远程任务没有常驻进程；worktree 目录要到执行时才创建
        return {"ok": False}
    if source_path:
        if {"path": source_path, "ssh_id": 0} not in project_sources(project):
            raise BadRequest("请选择该项目关联的源文件夹")
    else:
        source_path = str(project_path(project))
    if not Path(source_path).is_dir():
        return {"ok": False}
    # 目录写法与 runner.ensure_workdir 一致，执行时才能用上预建的会话
    server.prewarm(task, str(Path(source_path)), connections.ai_context(project))
    return {"ok": True}


def prewarm_task(task_id: int) -> dict:
    """打开已有任务：提前恢复它的会话；还没有会话时提前建好新会话。"""
    t = TaskRepo.get(task_id)
    if not t:
        raise NotFound("任务不存在")
    server = get_engine(t.get("engine")).live_server(t)
    if server is None or t["status"] in ("running", "queued"):
        return {"ok": False}
    cwd = t["workdir"] or ("" if t["use_worktree"] else t["source_path"] or str(project_path(t["project"])))
    if not cwd or not Path(cwd).is_dir():
        return {"ok": False}
    server.prewarm(t, str(Path(cwd)), connections.ai_context(t["project"]), t["session_id"] or None)
    return {"ok": True}


# ---------------------------------------------------------------- 发送消息

async def send(runner: Runner, task_id: int, *, message: Optional[str] = None, model: Optional[str] = None,
               effort: Optional[str] = None, permission_mode: Optional[str] = None, engine: Optional[str] = None,
               edit_run_id: Optional[int] = None, images: Optional[list[str]] = None) -> dict:
    """继续对话 / 重新执行。engine / model / effort / permission_mode 随发送一起提交，从这一轮起生效。
    edit_run_id：编辑某条已发送的消息——回退到它之前，用新内容重新发送（该条及之后的记录被替换）。"""
    if runner.is_running(task_id):
        raise Conflict("任务正在执行中")
    _check_effort(effort)
    fields = {k: v for k, v in (("model", model), ("effort", effort), ("permission_mode", permission_mode)) if v is not None}
    fields.update(_engine_fields(task_id, engine))
    TaskRepo.update(task_id, **fields)
    imgs = uploads.validate(images or [])
    t = TaskRepo.get(task_id)
    if t:
        check_remote_bypass(t)  # 在回退历史之前检查，避免被拦下时已经丢了记录
    if edit_run_id is not None:
        if not (message or "").strip():
            raise BadRequest("内容不能为空")
        if images is None:  # 未指定则沿用被编辑消息原来的图片
            old = RunRepo.get(edit_run_id)
            imgs = TaskRepo.images(old) if old and old["task_id"] == task_id else []
        if await rewind(task_id, edit_run_id, message):
            # 从头开始：新内容已写入任务描述，图片也改为任务首条消息的图片
            TaskRepo.update(task_id, touch=False, images=json.dumps(imgs))
            return {"run_id": await runner.start(task_id, None)}
    return {"run_id": await runner.start(task_id, message, imgs)}


async def rewind(task_id: int, edit_run_id: int, message: str) -> bool:
    """把会话回退到 edit_run_id 这条消息之前，之后由 runner.start 以新消息继续。
    被编辑的消息若是第一条（或找不到回退点），则开新会话并把它作为任务描述。返回 True 表示从头开始。"""
    t = TaskRepo.get(task_id)
    if not t:
        raise NotFound("任务不存在")
    if t["status"] in ("running", "queued"):
        raise Conflict("任务正在执行中")
    runs = [r["id"] for r in RunRepo.of_task(task_id)]
    if edit_run_id not in runs:
        raise NotFound("要编辑的消息不存在")
    earlier = [r for r in runs if r < edit_run_id]
    engine = t.get("engine") or "claude"
    # 切换过引擎：回退点之前的记录不全属于当前会话，改为把剩下的记录交接给新会话
    handoff = bool(earlier) and any(_run_engine(r) != engine for r in earlier)
    new_sid, fork_at = "", ""
    if t["session_id"] and earlier and not handoff:
        new_sid, fork_at = await get_engine(engine).fork(t, earlier, runlog.events)
    for rid in runs:  # 删掉被编辑的这条及之后的记录
        if rid >= edit_run_id:
            runlog.delete(rid)
            RunRepo.delete(rid)
    if new_sid or handoff:
        TaskRepo.update(task_id, session_id=new_sid, fork_at=fork_at)
        return False
    TaskRepo.update(task_id, session_id="", fork_at="", description=message, title=derive_title(message))
    return True


def _run_engine(run_id: int) -> str:
    """某次运行使用的引擎（记录在运行日志的 start 事件里）。"""
    for e in runlog.events(run_id):
        if e.get("type") == "wb" and e.get("subtype") == "start":
            return e.get("engine") or "claude"
    return ""


def cancel(runner: Runner, task_id: int) -> dict:
    runner.cancel(task_id)
    return {"ok": True}


# ---------------------------------------------------------------- 文件

def resolve_file(task_id: int, path: str) -> Path:
    """AI 回复里引用的本地文件：只允许任务工作目录、项目目录和 uploads 内的文件。"""
    t = TaskRepo.get(task_id)
    if not t:
        raise NotFound("任务不存在")
    if t["ssh_id"]:
        raise NotFound("远程任务的文件在服务器上，无法在这里预览")
    source = t["source_path"]
    if not source:
        try:
            source = str(project_path(t["project"]))
        except WorkbenchError:
            if not t["workdir"]:
                raise
    roots = [Path(p).resolve() for p in (t["workdir"], source, str(uploads.uploads_dir())) if p]
    raw = path[7:] if path.startswith("file://") else path
    cand = Path(raw).expanduser()
    for f in ([cand] if cand.is_absolute() else [r / cand for r in roots]):
        f = f.resolve()
        if f.is_file() and any(f.is_relative_to(r) for r in roots):
            return f
    raise NotFound("文件不存在或不在任务目录内")


def run_events(run_id: int, offset: int = 0) -> dict:
    r = RunRepo.get(run_id)
    if not r:
        raise NotFound("run 不存在")
    events, offset = runlog.read_from(run_id, offset)
    return {"events": events, "offset": offset, "status": r["status"]}
