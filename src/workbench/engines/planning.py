"""用现有 Claude 或 Codex CLI 生成拆分计划。"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
from pathlib import Path
from tempfile import TemporaryDirectory

from workbench.core.errors import UpstreamError
from workbench.core.shell import child_env
from workbench.engines import get_engine
from workbench.engines.base import CommandSpec, RunState
from workbench.ssh import commands, transport

INSTRUCTIONS = """把请求拆成共享工作区的会话。禁止调用工具。只输出紧凑 JSON：
{"title":"组名","tasks":[{"title":"准备","prompt":"会话要做的事"},{"title":"使用产物","prompt":"会话要做的事","depends_on":["1"]}]}
并行指令下明确列出的每个顶层任务各开一个会话（包括数字加空格的编号），标题写的数量只作参考。任务内部的子步骤留在本项，未明确列项时按实际工作拆分。
依赖产物或写同一文件的任务各保留一张卡，按执行顺序在 depends_on 填前置任务的序号字符串（从 1 开始），其余并行。共享产物未命名时指定一个路径，并在有关会话中沿用。
每项简述自身工作、所需产物和约束，保留其他会话的改动。等待和确认前置状态由工作台负责。
仅在用户为某项指定 claude 或 codex 时填 engine。没有可执行任务时才返回空 tasks 并用 blocked_reason 说明。
用户请求：
"""


def parse_plan(reply: str) -> dict:
    reply = reply.strip()
    if reply.startswith(("```json\n", "```\n")) and reply.endswith("```"):
        reply = reply.split("\n", 1)[1].removesuffix("```").strip()
    try:
        plan = json.loads(reply)
    except (json.JSONDecodeError, TypeError) as e:
        raise UpstreamError("CLI 没有返回有效的任务计划") from e
    if not isinstance(plan, dict):
        raise UpstreamError("CLI 没有返回有效的任务计划")
    return plan


def event_plan(engine: str, event: dict) -> list[dict]:
    """统一单轮计划的步骤名称与状态。"""
    label_key = "step"
    item = event.get("item")
    if event.get("type") == "wb" and event.get("subtype") == "plan":
        steps = event.get("plan") or []
    elif engine == "codex" and isinstance(item, dict) and item.get("type") == "todo_list":
        steps = (item.get("items") or []) if event.get("type") != "item.started" else []
        label_key = "text"
    elif engine == "claude" and event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
        blocks = (event.get("message") or {}).get("content") or []
        steps = next(((block.get("input") or {}).get("todos") or [] for block in reversed(blocks)
                      if isinstance(block, dict) and block.get("name") == "TodoWrite"), [])
        label_key = "content"
    else:
        return []
    plan = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("status") == "completed" or step.get("completed") is True:
            state = "done"
        elif step.get("status") in ("inProgress", "in_progress"):
            state = "running"
        else:
            state = "pending"
        label = str(step.get(label_key) or step.get("activeForm") or "")
        plan.append({"label": label, "state": state, "active_form": str(step.get("activeForm") or label)})
    return plan


def event_progress(engine: str, event: dict) -> tuple[int, str] | None:
    """从单轮计划事件提取已完成比例和当前步骤。"""
    steps = event_plan(engine, event)
    if not steps:
        return None
    done = sum(step["state"] == "done" for step in steps)
    current = next((step for step in steps if step["state"] == "running"), None)
    current = current or next((step for step in steps if step["state"] == "pending"), {})
    return round(100 * done / len(steps)), current.get("active_form", "")[:160]


async def plan_with_cli(settings: dict, prompt: str, remote: dict | None) -> dict:
    engine = get_engine(settings["engine"])
    binary = engine.remote_bin(remote) if remote else engine.bin
    cli = engine.build_cmd(CommandSpec({**settings, "permission_mode": "plan", "effort": "low"}, INSTRUCTIONS + prompt,
                                       binary=binary))
    if engine.name == "claude":
        cli += ["--tools", "", "--strict-mcp-config", "--no-session-persistence"]
    else:
        cli[2:2] = ["--ephemeral", "-c", 'approval_policy="never"', "-c", "features.shell_tool=false",
                    "-c", "features.unified_exec=false", "-c", 'web_search="disabled"', "-c", "mcp_servers={}",
                    "-c", "features.plugins=false", "-c", "features.skip_host_skill_discovery=true"]
    env = child_env()
    with TemporaryDirectory(prefix="wb-plan-") as cwd:
        if remote:
            await transport.connect(remote)
            argv, env = transport.exec_argv(remote, commands.remote_shell(f"cd /tmp && exec {shlex.join(cli)}"))
        else:
            if engine.name == "codex":
                # 规划只需生成 JSON，无需带入通用编码助手的长段指令。
                instructions = Path(cwd) / "planner.md"
                instructions.write_text("Split tasks as requested. Output only JSON. Do not use tools.\n", encoding="utf-8")
                cli[2:2] = ["-c", f"model_instructions_file={json.dumps(str(instructions))}"]
            argv = cli
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env, start_new_session=True, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError as e:
            raise UpstreamError(f"找不到 {engine.label} CLI，请检查安装和登录") from e
        try:
            payload, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
        except (TimeoutError, asyncio.CancelledError):
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.communicate()
            raise
    state = RunState()
    for line in payload.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            engine.parse_event(event, state)
    if proc.returncode or state.status != "success":
        raise UpstreamError(f"{engine.label} CLI 未能拆分任务，请检查引擎连接后重试")
    return parse_plan(state.result)
