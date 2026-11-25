"""常驻的 `codex app-server`：本机 Codex 任务的每条消息都发给同一个进程（与 t3code 的做法一致）。

每次启动 `codex exec` 都要冷启动；常驻进程里同一会话的后续消息只要一两秒，并且能逐字收到回复。
会话在第一条消息发出时才 `thread/start`，并一次带上工作目录、模型、沙箱、审批策略，
之后每轮 `turn/start` 再带上同样的设置（用户中途改了模型 / 权限也能生效）。
需要审批的命令 / 文件修改会转给界面，由用户批准或拒绝。

对外只暴露 turn()：按顺序产出与 `codex exec --json` 相同格式的事件（thread.started、turn.started、
item.*、turn.completed / turn.failed），外加工作台事件：逐字输出 {"type": "wb", "subtype": "delta"}、
审批请求 {"type": "wb", "subtype": "approval"} 与其结果 {"type": "wb", "subtype": "approval_done"}。
"""
from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import signal
import time
from typing import AsyncIterator, Optional

from workbench.core.shell import child_env, find_bin
from workbench.engines.base import ServerUnavailable, TurnHandle

log = logging.getLogger("workbench.prewarm")   # 预热过程写进应用日志，排查「为什么慢」用
IDLE_SECONDS = 15 * 60          # 没有任务在跑超过这么久就退出，下次发消息时再启动
SESSION_LOAD_LIMIT = 12         # 常驻进程反复加载会话会累积文件句柄，达到限额后在空闲时回收
RECYCLE_SECONDS = 1
MAX_SPARES = 2                  # 预建会话的上限（不同目录 / 设置各算一个）
REQUEST_TIMEOUT = 60            # 启动、初始化与单个请求的超时
# 权限模式 -> (会话沙箱, 单轮沙箱策略, 审批策略)，参照 t3code 的运行模式
MODES = {
    "acceptEdits": ("workspace-write", {"type": "workspaceWrite"}, "on-request"),
    "plan": ("read-only", {"type": "readOnly"}, "on-request"),
    "default": ("read-only", {"type": "readOnly"}, "untrusted"),
    "bypassPermissions": ("danger-full-access", {"type": "dangerFullAccess"}, "never"),
}
# 转给界面的审批请求；其余服务端请求（提问、MCP 表单、外部工具等）工作台不支持，直接拒绝
APPROVALS = {"item/commandExecution/requestApproval": "command", "item/fileChange/requestApproval": "file"}
DECISIONS = ("accept", "acceptForSession", "decline", "cancel")


def describe(key: str) -> str:
    """日志里的设置摘要：目录名 / 模型 / 思考 / 沙箱 / 审批。"""
    try:
        t = json.loads(key)
    except ValueError:
        return key
    return (f"{os.path.basename(t.get('cwd') or '') or '?'} 模型={t.get('model') or '默认'} "
            f"思考={(t.get('config') or {}).get('model_reasoning_effort') or '默认'} "
            f"沙箱={t.get('sandbox')} 审批={t.get('approvalPolicy')}")


def mode_of(permission_mode: Optional[str]) -> tuple[str, dict, str]:
    return MODES.get(permission_mode or "acceptEdits", MODES["acceptEdits"])


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def convert_item(item: dict) -> Optional[dict]:
    """app-server 的条目 -> `codex exec --json` 的条目格式，内容完整保留（思考、diff、计划、工具参数与结果）。
    只有用户自己发的消息不再转发；其余没有专门格式的条目原样放在 raw 里。"""
    t, iid = item.get("type"), item.get("id")
    if t == "userMessage":
        return None
    if t == "agentMessage":
        return {"id": iid, "type": "agent_message", "text": item.get("text") or "", "phase": item.get("phase")}
    if t == "reasoning":   # Codex 的思考：摘要 + 正文（模型提供多少就是多少）
        return {"id": iid, "type": "reasoning", "summary": item.get("summary") or [], "content": item.get("content") or []}
    if t == "plan":
        return {"id": iid, "type": "plan", "text": item.get("text") or ""}
    if t == "commandExecution":
        return {"id": iid, "type": "command_execution", "command": item.get("command") or "", "cwd": item.get("cwd"),
                "aggregated_output": item.get("aggregatedOutput") or "", "exit_code": item.get("exitCode"),
                "status": item.get("status"), "duration_ms": item.get("durationMs")}
    if t == "fileChange":
        return {"id": iid, "type": "file_change", "status": item.get("status"),
                "changes": [{"path": c.get("path"), "kind": (c.get("kind") or {}).get("type", ""),
                             "move_path": (c.get("kind") or {}).get("move_path"), "diff": c.get("diff") or ""}
                            for c in item.get("changes") or []]}
    if t in ("mcpToolCall", "dynamicToolCall"):
        return {"id": iid, "type": "mcp_tool_call", "server": item.get("server") or item.get("namespace") or "",
                "tool": item.get("tool") or "", "arguments": item.get("arguments") or {},
                "result": item.get("result") or item.get("contentItems"), "error": item.get("error"),
                "status": item.get("status")}
    if t == "webSearch":
        return {"id": iid, "type": "web_search", "query": item.get("query") or "", "action": item.get("action"),
                "results": item.get("results")}
    return {"id": iid, "type": "other", "item_type": t, "raw": item}


def _usage(tu: dict) -> dict:
    last = (tu or {}).get("last") or {}
    return {"input_tokens": last.get("inputTokens", 0), "cached_input_tokens": last.get("cachedInputTokens", 0),
            "output_tokens": last.get("outputTokens", 0),
            "reasoning_output_tokens": last.get("reasoningOutputTokens", 0)}


class CodexServer:
    """一个常驻的 app-server 进程，按 threadId 把通知和审批请求分发给正在执行的轮次。"""

    def __init__(self, args: Optional[list[str]] = None):
        self.args = args or ["app-server"]
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.loaded: set[str] = set()                      # 已在本进程加载的会话
        # 预热（与 Codex CLI 打开时就建好会话一样）：新会话要先启动 MCP 服务、构建工具、连接模型，
        # 在用户输入时提前做；设置与发送时完全一致才会被使用
        self.spares: dict[str, asyncio.Task] = {}          # 设置的键 -> 预建会话的 threadId
        self.spare_since: dict[str, float] = {}            # 设置的键 -> 开始预建的时间（日志用）
        self.resuming: dict[str, asyncio.Task] = {}        # threadId -> 正在后台恢复
        self.queues: dict[str, asyncio.Queue] = {}         # threadId -> 该会话当前轮次的消息
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.lock = asyncio.Lock()
        self.idle: Optional[asyncio.TimerHandle] = None
        self.session_loads = 0
        self.opening_turns = 0

    # ---- 进程
    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def ensure(self) -> None:
        async with self.lock:
            if self.alive:
                return
            t0 = time.monotonic()
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    find_bin("codex"), *self.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=child_env(), start_new_session=True,
                    limit=64 * 1024 * 1024)
            except OSError as e:
                raise ServerUnavailable(f"无法启动 codex app-server：{e}") from e
            self.loaded.clear()
            self.session_loads = 0
            asyncio.create_task(self._read(self.proc))
            try:
                await self.request("initialize", {"clientInfo": {"name": "workbench", "version": "1.0"}})
                self._send({"jsonrpc": "2.0", "method": "initialized"})
            except Exception as e:  # noqa: BLE001
                await self.close()
                log.warning("Codex 常驻进程启动失败：%s", e)
                raise ServerUnavailable(f"codex app-server 初始化失败：{e}") from e
            log.info("Codex 常驻进程已启动，用时 %.1fs", time.monotonic() - t0)

    async def close(self) -> None:
        if self.alive:
            log.info("Codex 常驻进程关闭（空闲超时或应用退出）")
        self._forget_warm()
        if self.idle:
            self.idle.cancel()
            self.idle = None
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            await proc.wait()

    def _touch(self) -> None:
        """有轮次在跑时不退出；全部结束后开始计时。"""
        if self.idle:
            self.idle.cancel()
            self.idle = None
        if not self.queues and not self.opening_turns and self.alive:
            proc = self.proc
            delay = RECYCLE_SECONDS if self.session_loads >= SESSION_LOAD_LIMIT else IDLE_SECONDS
            self.idle = asyncio.get_running_loop().call_later(
                delay, lambda: asyncio.create_task(self._close_when_idle(proc)))

    async def _close_when_idle(self, proc: asyncio.subprocess.Process) -> None:
        async with self.lock:
            if self.proc is not proc or self.queues or self.opening_turns:
                return
            if self.pending:
                self._touch()
                return
            await self.close()

    # ---- JSON-RPC
    def _send(self, obj: dict) -> None:
        if self.alive:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())

    async def request(self, method: str, params: Optional[dict] = None, timeout: float = REQUEST_TIMEOUT):
        if not self.alive:
            raise RuntimeError("codex app-server 已退出")
        self.next_id += 1
        rid = self.next_id
        fut = self.pending[rid] = asyncio.get_running_loop().create_future()
        opens_session = method in ("thread/start", "thread/resume")
        if opens_session:
            self.session_loads += 1
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params else {})})
            await self.proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout)
        except (RuntimeError, OSError) as e:
            if (opens_session or method == "turn/start") and (
                "too many open files" in str(e).lower() or getattr(e, "errno", None) == errno.EMFILE
            ):
                self.session_loads = max(self.session_loads, SESSION_LOAD_LIMIT)
                raise ServerUnavailable("Codex 常驻进程已达到文件句柄上限，空闲时将自动回收") from e
            raise
        finally:
            self.pending.pop(rid, None)
            if opens_session:
                self._touch()

    def respond(self, request_id, result: Optional[dict] = None, error: Optional[str] = None) -> None:
        msg = {"jsonrpc": "2.0", "id": request_id}
        msg.update({"error": {"code": -32601, "message": error}} if error is not None else {"result": result or {}})
        self._send(msg)

    async def _read(self, proc) -> None:
        async for raw in proc.stdout:
            if self.proc is not None and self.proc is not proc:
                return
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if "method" in msg and "id" in msg:   # 服务端请求：审批转给对应会话的轮次，其余直接拒绝
                q = self.queues.get((msg.get("params") or {}).get("threadId"))
                if msg["method"] in APPROVALS and q:
                    q.put_nowait(msg)
                else:
                    self.respond(msg["id"], error="工作台不支持这类请求")
            elif "id" in msg:
                fut = self.pending.get(msg["id"])
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError((msg["error"] or {}).get("message") or str(msg["error"])))
                    else:
                        fut.set_result(msg.get("result"))
            else:
                q = self.queues.get((msg.get("params") or {}).get("threadId"))
                if q:
                    q.put_nowait(msg)
        if self.proc is not None and self.proc is not proc:
            return
        # 进程退出：唤醒该进程的等待者
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("codex app-server 已退出"))
        for q in self.queues.values():
            q.put_nowait({"method": "_exited"})
        if self.proc is proc:   # 意外退出：预建 / 恢复的会话随进程失效
            log.warning("Codex 常驻进程意外退出，预建的会话失效")
            self.proc = None
            self.loaded.clear()
            self._forget_warm()

    # ---- 会话设置与预热
    @staticmethod
    def settings(task: dict, cwd: str, instructions: str) -> tuple[str, dict, dict]:
        """(设置的键, thread/start 参数, turn/start 参数)。预建会话与发送时用同一份，保证完全一致。"""
        from workbench.engines.codex import codex_config   # 避免循环引用
        cfg = codex_config()
        sandbox, policy, approval = mode_of(task.get("permission_mode"))
        # 选「默认」时显式传 config.toml 里的默认值：单轮的模型 / 思考等级会沿用到之后的轮次
        model = task.get("model") or cfg.get("model")
        effort = task.get("effort") or cfg.get("model_reasoning_effort")
        thread = {"cwd": cwd, "approvalPolicy": approval, "sandbox": sandbox}
        turn = {"cwd": cwd, "approvalPolicy": approval, "sandboxPolicy": policy}
        if model:
            thread["model"] = turn["model"] = model
        if effort:
            thread["config"] = {"model_reasoning_effort": effort}
            turn["effort"] = effort
        if instructions:
            thread["developerInstructions"] = instructions
        return json.dumps(thread, sort_keys=True, ensure_ascii=False), thread, turn

    def _forget_warm(self) -> None:
        for t in [*self.spares.values(), *self.resuming.values()]:
            t.cancel()
        self.spares.clear()
        self.spare_since.clear()
        self.resuming.clear()

    def _background(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())   # 预热失败不影响发送，发送时会重试
        return task

    def _spare(self, key: str, thread: dict) -> None:
        old = self.spares.get(key)
        if old and not (old.done() and (old.cancelled() or old.exception())):
            return
        while len(self.spares) >= MAX_SPARES:
            old_key = next(iter(self.spares))
            self.spares.pop(old_key).cancel()
            log.info("Codex 预建会话被挤掉（最多 %d 个）：%s", MAX_SPARES, describe(old_key))
        t0 = self.spare_since[key] = time.monotonic()
        log.info("Codex 预建会话：开始 %s", describe(key))

        async def run() -> str:
            try:
                await self.ensure()
                self._touch()
                res = await self.request("thread/start", thread)
            except Exception as e:  # noqa: BLE001
                log.warning("Codex 预建会话失败：%s（%s）", e, describe(key))
                raise
            self.loaded.add(res["thread"]["id"])
            log.info("Codex 预建会话：会话已建立，用时 %.1fs（Codex 随后在后台启动 MCP、连接模型）", time.monotonic() - t0)
            return res["thread"]["id"]
        self.spares[key] = self._background(run())

    def prewarm(self, task: dict, cwd: str, instructions: str, resume_sid: Optional[str] = None) -> None:
        """提前准备：新任务预建会话；已有会话提前 thread/resume。重复调用不会重复准备。"""
        key, thread, _ = self.settings(task, cwd, instructions)
        if not resume_sid:
            self._spare(key, thread)
            return
        if resume_sid in self.loaded or resume_sid in self.resuming:
            return

        async def run() -> None:
            t0 = time.monotonic()
            await self.ensure()
            self._touch()
            await self.request("thread/resume", {"threadId": resume_sid, **{k: v for k, v in thread.items()
                                                                              if k != "developerInstructions"}})
            self.loaded.add(resume_sid)
            log.info("Codex 提前恢复会话 %s：已就绪，用时 %.1fs", resume_sid[:8], time.monotonic() - t0)
        self.resuming[resume_sid] = self._background(run())

    # ---- 执行一轮
    async def _open_thread(self, key: str, thread: dict, resume_sid: Optional[str]) -> tuple[str, bool]:
        tid, warmed = resume_sid, False
        if tid:
            pending = self.resuming.pop(tid, None)
            if pending:   # 打开任务时已在后台恢复：等它完成
                await asyncio.gather(pending, return_exceptions=True)
            if tid not in self.loaded:
                await self.request("thread/resume", {"threadId": tid, **{k: v for k, v in thread.items()
                                                                          if k != "developerInstructions"}})
        else:
            spare = self.spares.pop(key, None)
            if spare:     # 输入时已预建、设置完全一致：直接用（还没建好就等它），并为下一个新任务补一个
                ready = spare.done()
                got = (await asyncio.gather(spare, return_exceptions=True))[0]
                if isinstance(got, str):
                    tid, warmed = got, True
                    log.info("Codex 新会话：用上预建会话（%.1fs 前开始预建，发送时%s）",
                             time.monotonic() - self.spare_since.get(key, time.monotonic()),
                             "已就绪" if ready else "仍在准备，等它完成")
                else:
                    log.warning("Codex 新会话：预建会话不可用（%s），改为当场新建", got)
                self._spare(key, thread)
            else:
                log.info("Codex 新会话：没有可用的预建会话，当场新建。需要：%s；现有：%s", describe(key),
                         "；".join(describe(k) for k in self.spares) or "无")
            if not tid:
                tid = (await self.request("thread/start", thread))["thread"]["id"]
        self.loaded.add(tid)
        return tid, warmed

    async def turn(self, task: dict, prompt: str, resume_sid: Optional[str], images: list[str], cwd: str,
                   instructions: str, handle: TurnHandle, uploads_dir: str = "") -> AsyncIterator[str]:
        """发送一条消息，按 `codex exec --json` 的格式逐行产出事件。"""
        self.opening_turns += 1
        self._touch()
        try:
            await self.ensure()   # 失败抛 ServerUnavailable
            key, thread, turn_params = self.settings(task, cwd, instructions)
            tid, warmed = await self._open_thread(key, thread, resume_sid)
            if tid in self.queues:
                raise RuntimeError("该会话正在执行")
            q = self.queues[tid] = asyncio.Queue()
        finally:
            self.opening_turns -= 1
            self._touch()
        approvals: dict[str, object] = {}   # 界面上的审批编号 -> app-server 请求 id
        try:
            inputs = [{"type": "text", "text": prompt or "请查看附带的图片。", "text_elements": []}]
            inputs += [{"type": "localImage", "path": p} for p in images]
            # 预建会话的设置与这一轮完全一致，第一轮不再重复传（实测首字更稳定）；其余情况带上设置，中途改了也能生效
            params = {"threadId": tid, "input": inputs, **({} if warmed else turn_params)}
            res = await self.request("turn/start", params)
            yield _line({"type": "thread.started", "thread_id": tid})
            turn_id = (res.get("turn") or {}).get("id")
            handle.interrupt = lambda: self.request("turn/interrupt", {"threadId": tid, "turnId": turn_id})

            def decide(approval_id: str, decision: str) -> bool:
                rid = approvals.pop(approval_id, None)
                if rid is None or decision not in DECISIONS:
                    return False
                self.respond(rid, {"decision": decision})
                q.put_nowait({"method": "_decided", "params": {"id": approval_id, "decision": decision}})
                return True
            handle.decide = decide

            usage: dict = {}
            while True:
                msg = await q.get()
                m, p = msg.get("method"), msg.get("params") or {}
                if p.get("turnId") and turn_id and p["turnId"] != turn_id:
                    if "id" in msg:   # 上一轮遗留的审批请求：拒绝
                        self.respond(msg["id"], {"decision": "decline"})
                    continue
                if m == "_exited":
                    yield _line({"type": "turn.failed", "error": {"message": "codex app-server 意外退出"}})
                    return
                if m in APPROVALS and "id" in msg:
                    aid = str(p.get("approvalId") or p.get("itemId") or msg["id"])
                    approvals[aid] = msg["id"]
                    yield _line({"type": "wb", "subtype": "approval", "id": aid, "kind": APPROVALS[m],
                                 "command": p.get("command") or "", "cwd": p.get("cwd") or "",
                                 "reason": p.get("reason") or "", "grant_root": p.get("grantRoot") or ""})
                elif m == "_decided":
                    yield _line({"type": "wb", "subtype": "approval_done", **p})
                elif m == "turn/started":
                    yield _line({"type": "turn.started"})
                elif m == "item/agentMessage/delta" and p.get("delta"):
                    yield _line({"type": "wb", "subtype": "delta", "text": p["delta"]})
                elif m in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta") and p.get("delta"):
                    yield _line({"type": "wb", "subtype": "thinking_delta", "item": p.get("itemId"), "text": p["delta"]})
                elif m == "item/reasoning/summaryPartAdded":   # 新的一段思考摘要
                    yield _line({"type": "wb", "subtype": "thinking_delta", "item": p.get("itemId"), "text": "\n\n"})
                elif m == "item/commandExecution/outputDelta" and p.get("delta"):
                    yield _line({"type": "wb", "subtype": "output_delta", "item": p.get("itemId"), "text": p["delta"]})
                elif m == "turn/plan/updated":
                    yield _line({"type": "wb", "subtype": "plan", "explanation": p.get("explanation") or "",
                                 "plan": p.get("plan") or []})
                elif m == "warning" and p.get("message"):
                    yield _line({"type": "wb", "subtype": "warning", "text": p["message"]})
                elif m == "error" and p.get("willRetry"):
                    yield _line({"type": "wb", "subtype": "warning",
                                 "text": "正在重试：" + ((p.get("error") or {}).get("message") or "")})
                elif m in ("item/started", "item/completed"):
                    item = convert_item(p.get("item") or {})
                    if item:
                        yield _line({"type": m.replace("/", "."), "item": item})
                elif m == "thread/tokenUsage/updated":
                    usage = _usage(p.get("tokenUsage"))
                elif m == "turn/completed":
                    turn = p.get("turn") or {}
                    status = turn.get("status")
                    if status == "completed":
                        yield _line({"type": "turn.completed", "usage": usage})
                    elif status == "interrupted":
                        yield _line({"type": "turn.failed", "interrupted": True, "error": {"message": "已停止"}})
                    else:
                        err = (turn.get("error") or {}).get("message") or "执行失败"
                        yield _line({"type": "turn.failed", "error": {"message": err}})
                    return
        finally:
            for rid in approvals.values():   # 轮次结束时还没处理的审批：拒绝
                self.respond(rid, {"decision": "decline"})
            handle.decide = None
            self.queues.pop(tid, None)
            self._touch()


_server: Optional[CodexServer] = None


def get_server() -> CodexServer:
    global _server
    if _server is None:
        _server = CodexServer()
    return _server


async def close_server() -> None:
    global _server
    server, _server = _server, None
    if server is not None:
        await server.close()
