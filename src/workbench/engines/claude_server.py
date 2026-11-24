"""常驻的 Claude 会话：每个会话保持一个 `claude` 进程，用 stream-json 收发消息（与官方 Agent SDK 的做法一致）。

每条消息启动一次 `claude -p` 都要冷启动；常驻进程里后续消息直接写进它的输入，回复逐字到达。
需要审批的工具调用由 CLI 以 control_request（can_use_tool）发出，转给界面由用户批准或拒绝。
模型 / 思考等级 / 权限模式 / 目录变化时，带 `--resume` 重开进程（会话不变）。

turn() 产出的就是 CLI 自己的 stream-json 事件（system / assistant / stream_event / result），
外加工作台的审批事件 {"type": "wb", "subtype": "approval" | "approval_done"}。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from workbench.core.shell import child_env, find_bin
from workbench.engines.base import ServerUnavailable, TurnHandle

log = logging.getLogger("workbench.prewarm")   # 预热过程写进应用日志，排查「为什么慢」用
IDLE_SECONDS = 15 * 60          # 会话空闲这么久就关掉进程，下次发消息时带 --resume 重开
INIT_TIMEOUT = 120              # initialize 要等 MCP 服务启动
MAX_SPARES = 2                  # 预启动进程的上限（不同目录 / 设置各算一个）
FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
DECISIONS = ("accept", "acceptForSession", "decline", "cancel")


@dataclass
class Session:
    proc: asyncio.subprocess.Process
    key: tuple                      # 启动参数（变化时重开进程）
    sid: str
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending: dict = field(default_factory=dict)      # 控制请求 id -> Future
    busy: bool = False
    cost: float = 0.0               # 进程报告的累计费用，用来换算每轮的增量
    idle: Optional[asyncio.TimerHandle] = None
    seq: int = 0

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None


def describe(key: tuple) -> str:
    """日志里的设置摘要：目录名 / 权限 / 模型 / 思考。"""
    cwd, _, mode, model, effort, _, add_dir = key
    return (f"{os.path.basename(cwd) or '?'} 权限={mode} 模型={model or '默认'} 思考={effort or '默认'}"
            + (" 带图片目录" if add_dir else ""))


def approval_view(req: dict) -> dict:
    """can_use_tool 请求 -> 界面上的审批卡片内容。"""
    tool, inp = req.get("tool_name") or "", req.get("input") or {}
    reason = req.get("decision_reason") or req.get("description") or req.get("title") or ""
    if tool == "Bash":
        return {"kind": "command", "command": inp.get("command") or "", "reason": inp.get("description") or reason}
    if tool in FILE_TOOLS:
        return {"kind": "file", "command": inp.get("file_path") or inp.get("notebook_path") or "", "reason": reason}
    return {"kind": "tool", "tool": tool, "command": json.dumps(inp, ensure_ascii=False)[:2000], "reason": reason}


class ClaudeServer:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}   # 会话 ID -> 常驻进程
        # 预热（与 claude 打开时就准备好一样）：输入时提前启动进程并完成握手；设置与发送时完全一致才会被使用
        self.spares: dict[tuple, asyncio.Task] = {}      # 设置 -> 新会话进程
        self.spare_since: dict[tuple, float] = {}        # 设置 -> 开始预启动的时间（日志用）
        self.resuming: dict[str, asyncio.Task] = {}      # 会话 ID -> 正在用 --resume 启动的进程

    # ---- 进程
    async def _spawn(self, key: tuple, sid: Optional[str], fork_at: str) -> Session:
        cwd, binary, mode, model, effort, instructions, add_dir = key
        new_sid = str(uuid.uuid4())
        cmd = [binary, "--output-format", "stream-json", "--verbose", "--input-format", "stream-json",
               "--include-partial-messages", "--permission-mode", mode, "--permission-prompt-tool", "stdio"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if instructions:
            cmd += ["--append-system-prompt", instructions]
        if add_dir:
            cmd += ["--add-dir", add_dir]
        if sid:
            cmd += ["--resume", sid]
            if fork_at:   # 编辑过历史消息：从该点分叉出新会话，原会话不动
                cmd += ["--resume-session-at", fork_at, "--fork-session"]
        else:
            cmd += ["--session-id", new_sid]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd, env=child_env(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=64 * 1024 * 1024)
        except OSError as e:
            raise ServerUnavailable(f"无法启动 claude：{e}") from e
        s = Session(proc, key, sid if sid and not fork_at else new_sid)
        asyncio.create_task(self._read(s))
        try:
            await self._control(s, {"subtype": "initialize", "hooks": None}, INIT_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            self._kill(s)
            raise ServerUnavailable(f"claude 初始化失败：{e}") from e
        return s

    @staticmethod
    def _kill(s: Session) -> None:
        if s.idle:
            s.idle.cancel()
        if s.alive:
            try:
                os.killpg(s.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _drop(self, s: Session) -> None:
        self._kill(s)
        for k, v in list(self.sessions.items()):
            if v is s:
                self.sessions.pop(k, None)

    async def close(self) -> None:
        for t in [*self.spares.values(), *self.resuming.values()]:
            if t.done() and not t.cancelled() and not t.exception():
                self._kill(t.result())
            t.cancel()
        self.spares.clear()
        self.resuming.clear()
        for s in list(self.sessions.values()):
            self._drop(s)

    def _touch(self, s: Session) -> None:
        if s.idle:
            s.idle.cancel()
            s.idle = None
        if not s.busy and s.alive:
            s.idle = asyncio.get_running_loop().call_later(IDLE_SECONDS, self._drop, s)

    # ---- 协议
    def _write(self, s: Session, obj: dict) -> None:
        if s.alive:
            s.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())

    async def _control(self, s: Session, request: dict, timeout: float = 60) -> dict:
        s.seq += 1
        rid = f"wb_{s.seq}"
        fut = s.pending[rid] = asyncio.get_running_loop().create_future()
        self._write(s, {"type": "control_request", "request_id": rid, "request": request})
        await s.proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            s.pending.pop(rid, None)

    def _reply(self, s: Session, request_id: str, response: Optional[dict] = None, error: Optional[str] = None) -> None:
        body = ({"subtype": "error", "request_id": request_id, "error": error} if error is not None
                else {"subtype": "success", "request_id": request_id, "response": response or {}})
        self._write(s, {"type": "control_response", "response": body})

    async def _read(self, s: Session) -> None:
        async for raw in s.proc.stdout:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = msg.get("type")
            if t == "control_response":
                resp = msg.get("response") or {}
                fut = s.pending.get(resp.get("request_id"))
                if fut and not fut.done():
                    if resp.get("subtype") == "error":
                        fut.set_exception(RuntimeError(resp.get("error") or "控制请求失败"))
                    else:
                        fut.set_result(resp.get("response") or {})
            elif t == "control_request" and (msg.get("request") or {}).get("subtype") != "can_use_tool":
                self._reply(s, msg.get("request_id"), error="工作台不支持这类请求")
            elif t == "control_cancel_request":
                s.queue.put_nowait(msg)
            elif t in ("keep_alive", "streamlined_text", "streamlined_tool_use_summary"):
                continue
            else:
                s.queue.put_nowait(msg)
        await s.proc.wait()
        for fut in s.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("claude 已退出"))
        s.queue.put_nowait({"type": "_exited"})
        self._drop(s)

    # ---- 预热
    @staticmethod
    def key(task: dict, cwd: str, instructions: str, add_dir: str = "") -> tuple:
        return (cwd, find_bin("claude"), task.get("permission_mode") or "acceptEdits", task.get("model") or "",
                task.get("effort") or "", instructions, add_dir)

    @staticmethod
    def _background(coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())   # 预热失败不影响发送，发送时会重试
        return task

    def _spare(self, key: tuple) -> None:
        old = self.spares.get(key)
        if old and not (old.done() and (old.cancelled() or old.exception() or not old.result().alive)):
            return
        while len(self.spares) >= MAX_SPARES:
            old_key = next(iter(self.spares))
            t = self.spares.pop(old_key)
            if t.done() and not t.cancelled() and not t.exception():
                self._kill(t.result())
            t.cancel()
            log.info("Claude 预启动进程被挤掉（最多 %d 个）：%s", MAX_SPARES, describe(old_key))
        t0 = self.spare_since[key] = time.monotonic()
        log.info("Claude 预启动进程：开始 %s", describe(key))

        async def run() -> Session:
            try:
                s = await self._spawn(key, None, "")
            except Exception as e:  # noqa: BLE001
                log.warning("Claude 预启动进程失败：%s（%s）", e, describe(key))
                raise
            self._touch(s)   # 一直没用上：空闲到时关掉
            log.info("Claude 预启动进程：已就绪，用时 %.1fs", time.monotonic() - t0)
            return s
        self.spares[key] = self._background(run())

    def prewarm(self, task: dict, cwd: str, instructions: str, resume_sid: Optional[str] = None) -> None:
        """提前准备：新任务预启动进程；已有会话提前用 --resume 启动。重复调用不会重复准备。"""
        key = self.key(task, cwd, instructions)
        if not resume_sid:
            self._spare(key)
            return
        s = self.sessions.get(resume_sid)
        if (s and s.alive and s.key == key) or resume_sid in self.resuming or task.get("fork_at"):
            return

        async def run() -> Session:
            try:
                s = await self._spawn(key, resume_sid, "")
                old = self.sessions.get(resume_sid)
                if old and old is not s:
                    self._drop(old)
                self.sessions[resume_sid] = s
                self._touch(s)
                return s
            finally:
                self.resuming.pop(resume_sid, None)
        self.resuming[resume_sid] = self._background(run())

    # ---- 执行一轮
    async def turn(self, task: dict, prompt: str, resume_sid: Optional[str], images: list[str], cwd: str,
                   instructions: str, handle: TurnHandle, uploads_dir: str = "") -> AsyncIterator[str]:
        """发送一条消息，逐行产出 stream-json 事件，直到这一轮的 result。"""
        key = self.key(task, cwd, instructions, uploads_dir if images else "")   # 图片在项目外，需 --add-dir 放行
        fork_at = task.get("fork_at") or ""
        if resume_sid and resume_sid in self.resuming:   # 打开任务时已在后台启动：等它完成
            await asyncio.gather(self.resuming[resume_sid], return_exceptions=True)
        s = self.sessions.get(resume_sid or "")
        if s and (s.key != key or fork_at or not s.alive):
            self._drop(s)
            s = None
        if s is None and not resume_sid and not fork_at:
            spare = self.spares.pop(key, None)
            if spare:     # 输入时已预启动、设置完全一致：直接用（还没好就等它），并为下一个新任务补一个
                ready = spare.done()
                got = (await asyncio.gather(spare, return_exceptions=True))[0]
                if isinstance(got, Session) and got.alive:
                    s = got
                    log.info("Claude 新会话：用上预启动进程（%.1fs 前开始预启动，发送时%s）",
                             time.monotonic() - self.spare_since.get(key, time.monotonic()),
                             "已就绪" if ready else "仍在准备，等它完成")
                else:
                    log.warning("Claude 新会话：预启动进程不可用（%s），改为当场启动", got)
                self._spare(key)
            else:
                log.info("Claude 新会话：没有可用的预启动进程，当场启动。需要：%s；现有：%s", describe(key),
                         "；".join(describe(k) for k in self.spares) or "无")
        if s is None:
            s = await self._spawn(key, resume_sid, fork_at)
        if s.busy:
            raise RuntimeError("该会话正在执行")
        s.busy = True
        self._touch(s)
        self.sessions[s.sid] = s
        while not s.queue.empty():   # 丢掉上一轮之后的零散消息
            s.queue.get_nowait()
        approvals: dict[str, dict] = {}   # 审批编号 -> can_use_tool 请求
        try:
            from workbench.engines.claude import with_images   # 避免循环引用
            self._write(s, {"type": "user", "message": {"role": "user", "content": with_images(prompt, images)},
                            "parent_tool_use_id": None, "session_id": s.sid})
            await s.proc.stdin.drain()
            handle.interrupt = lambda: self._control(s, {"subtype": "interrupt"})

            def decide(approval_id: str, decision: str) -> bool:
                msg = approvals.pop(approval_id, None)
                if msg is None or decision not in DECISIONS:
                    return False
                req = msg.get("request") or {}
                if decision in ("accept", "acceptForSession"):
                    resp = {"behavior": "allow", "updatedInput": req.get("input") or {}}
                    if decision == "acceptForSession" and req.get("permission_suggestions"):
                        resp["updatedPermissions"] = req["permission_suggestions"]   # CLI 给出的「以后不再询问」
                else:
                    resp = {"behavior": "deny", "message": "用户拒绝了这一步"}
                    if decision == "cancel":
                        resp["interrupt"] = True
                self._reply(s, msg.get("request_id"), resp)
                s.queue.put_nowait({"type": "_decided", "id": approval_id, "decision": decision})
                return True
            handle.decide = decide

            while True:
                msg = await s.queue.get()
                t = msg.get("type")
                if t == "_exited":
                    yield json.dumps({"type": "result", "is_error": True, "result": "claude 进程意外退出"},
                                     ensure_ascii=False)
                    return
                if t == "control_request":   # 审批请求
                    aid = msg.get("request_id")
                    approvals[aid] = msg
                    yield json.dumps({"type": "wb", "subtype": "approval", "id": aid,
                                      **approval_view(msg.get("request") or {})}, ensure_ascii=False)
                    continue
                if t == "control_cancel_request":   # CLI 放弃了这个审批（如被中断）
                    approvals.pop(msg.get("request_id"), None)
                    continue
                if t == "_decided":
                    yield json.dumps({"type": "wb", "subtype": "approval_done", "id": msg["id"],
                                      "decision": msg["decision"]}, ensure_ascii=False)
                    continue
                if t == "system" and msg.get("session_id"):
                    s.sid = msg["session_id"]
                    self.sessions[s.sid] = s
                if t == "result":   # 常驻进程报告的是累计费用：换算成这一轮的
                    total = float(msg.get("total_cost_usd") or 0)
                    msg["total_cost_usd"], s.cost = max(total - s.cost, 0.0), max(total, s.cost)
                    yield json.dumps(msg, ensure_ascii=False)
                    return
                yield json.dumps(msg, ensure_ascii=False)
        finally:
            for msg in approvals.values():   # 轮次结束时还没处理的审批：拒绝
                self._reply(s, msg.get("request_id"), {"behavior": "deny", "message": "已失效"})
            handle.decide = None
            s.busy = False
            self._touch(s)


_server: Optional[ClaudeServer] = None


def get_server() -> ClaudeServer:
    global _server
    if _server is None:
        _server = ClaudeServer()
    return _server


async def close_server() -> None:
    global _server
    server, _server = _server, None
    if server is not None:
        await server.close()
