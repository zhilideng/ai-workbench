"""Codex CLI：`codex exec --json`；会话分叉与额度查询走 `codex app-server` 的 JSON-RPC。"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
from pathlib import Path
from typing import Optional

from workbench.core.errors import BadRequest, WorkbenchError
from workbench.core.shell import child_env, find_bin
from workbench.engines.base import CommandSpec, Engine, EventReader, RunState

# 与 Codex 客户端一致，只提供这五档（不含 max）
CODEX_EFFORTS = ["low", "medium", "high", "xhigh", "ultra"]
# 权限模式 -> codex 沙箱
CODEX_SANDBOX = {"acceptEdits": "workspace-write", "plan": "read-only", "default": "read-only"}
DEFAULT_IMAGE_PROMPT = "请查看附带的图片。"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def codex_config() -> dict:
    """读取 ~/.codex/config.toml（失败返回空）。"""
    import tomllib
    try:
        return tomllib.loads((codex_home() / "config.toml").read_text())
    except Exception:  # noqa: BLE001
        return {}


def _models_cache() -> list[dict]:
    try:
        return json.loads((codex_home() / "models_cache.json").read_text()).get("models", [])
    except Exception:  # noqa: BLE001
        return []


def codex_models() -> list[str]:
    """codex 自己缓存的可用模型列表（~/.codex/models_cache.json）。"""
    return [m["slug"] for m in _models_cache() if m.get("slug") and m.get("visibility", "list") != "hide"]


def codex_efforts() -> dict:
    """每个 codex 模型支持的思考等级，及未选模型时使用的默认值。"""
    return {m["slug"]: {"levels": [e for e in CODEX_EFFORTS
                                   if e in {x.get("effort") for x in m.get("supported_reasoning_levels", [])}],
                        "default": m.get("default_reasoning_level") or ""}
            for m in _models_cache() if m.get("slug")}


async def codex_rpc(calls: list[tuple[str, Optional[dict]]], timeout: float = 25) -> list:
    """启动一次 `codex app-server`，按顺序发 JSON-RPC 请求，返回各自的 result（任一出错即抛异常）。"""
    proc = await asyncio.create_subprocess_exec(
        find_bin("codex"), "app-server", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
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
              "params": {"clientInfo": {"name": "workbench", "version": "1.0"}}})
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


class CodexEngine(Engine):
    name = "codex"
    label = "Codex"

    def client_config(self) -> dict:
        return {"codex_models": codex_models(), "codex_efforts": codex_efforts(),
                "codex_default_effort": codex_config().get("model_reasoning_effort", "")}

    def build_cmd(self, spec: CommandSpec) -> list[str]:
        t, prompt = spec.task, spec.prompt
        mode = t.get("permission_mode") or "acceptEdits"
        if spec.ssh_context and not spec.resume_sid:  # codex 没有追加系统提示的参数：新会话时放在提示词前面
            prompt = f"{spec.ssh_context}\n\n---\n\n{prompt or DEFAULT_IMAGE_PROMPT}"
        cmd = [spec.binary or self.bin, "exec"] + (["resume", spec.resume_sid] if spec.resume_sid else [])
        cmd += ["--json", "--skip-git-repo-check"]
        if mode == "bypassPermissions":
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            cmd += ["-c", f'sandbox_mode="{CODEX_SANDBOX.get(mode, "workspace-write")}"']
        if t.get("model"):
            cmd += ["-m", t["model"]]
        if t.get("effort"):
            cmd += ["-c", f'model_reasoning_effort="{t["effort"]}"']
        for img in spec.images:
            cmd += ["-i", img]
        return cmd + ["--", prompt or DEFAULT_IMAGE_PROMPT]  # -i 可接多个值，用 -- 隔开提示词

    def mask_prompt(self, cmd: list[str]) -> list[str]:
        return cmd[:-1] + ["<prompt>"]

    def resume_command(self, sid: str, binary: Optional[str] = None) -> str:
        return f"{shlex.quote(binary or 'codex')} resume {shlex.quote(sid)}"

    def parse_event(self, ev: dict, st: RunState) -> None:
        t = ev.get("type")
        if t == "thread.started":
            st.sid = ev.get("thread_id")
        elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            st.result = ev["item"].get("text") or ""
        elif t == "turn.completed":
            st.status = "success"
        elif t == "turn.failed":
            st.status = "error"
            st.result = ((ev.get("error") or {}).get("message")) or st.result
        elif t == "error":
            st.status = "error"
            st.result = ev.get("message") or st.result

    async def fork(self, task: dict, earlier_runs: list[int], read_events: EventReader) -> tuple[str, str]:
        # app-server thread/fork 到上一轮（lastTurnId），得到新会话后 exec resume
        if task.get("ssh_id"):
            raise BadRequest("远程 Codex 任务暂不支持编辑历史消息（会话在服务器上）")
        n_turns = sum(any(e.get("type") == "turn.started" for e in read_events(rid)) for rid in earlier_runs)
        if not n_turns:
            return "", ""
        try:
            page = (await codex_rpc([("thread/turns/list", {"threadId": task["session_id"], "sortDirection": "asc",
                                                            "limit": 1000})]))[0]
            turns = page.get("data") or []
            if len(turns) < n_turns:
                raise RuntimeError(f"会话只有 {len(turns)} 轮，找不到第 {n_turns} 轮")
            fork = (await codex_rpc([("thread/fork", {"threadId": task["session_id"],
                                                      "lastTurnId": turns[n_turns - 1]["id"],
                                                      "excludeTurns": True})]))[0]
            return fork["thread"]["id"], ""
        except Exception as e:  # noqa: BLE001
            raise WorkbenchError(f"回退 Codex 会话失败：{e}")
