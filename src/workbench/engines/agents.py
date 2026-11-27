"""把 CLI 报告的子会话事件转换为看板可记录的活动。"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class AgentUpdate:
    id: str
    status: str = ""
    title: str = ""
    prompt: str = ""
    result: str = ""
    model: str = ""
    effort: str = ""
    event: dict | None = None
    native_run: str = ""
    previous_id: str = ""
    observed_turn: bool = False
    detail: str = ""
    known_only: bool = False


_CODEX_STATES = {
    "pendingInit": "running", "pending_init": "running", "running": "running",
    "completed": "success", "interrupted": "cancelled", "errored": "error", "shutdown": "cancelled",
}
_CODEX_SPAWN = ("spawnAgent", "spawn_agent")


def codex_agents(event: dict) -> list[AgentUpdate]:
    if event.get("type") not in ("item.started", "item.updated", "item.completed"):
        return []
    item = event.get("item") or {}
    raw = item.get("raw") or item
    kind = raw.get("type") or item.get("item_type")
    if kind in ("subAgentActivity", "sub_agent_activity"):
        aid = raw.get("agentThreadId") or raw.get("agent_thread_id")
        state = {"started": "running", "interrupted": "cancelled", "completed": "success"}.get(raw.get("kind"), "")
        title = (raw.get("agentPath") or raw.get("agent_path") or "").rsplit("/", 1)[-1]
        return [AgentUpdate(aid, state, title=title, model=raw.get("model") or "",
                            effort=raw.get("reasoningEffort") or raw.get("reasoning_effort") or "",
                            known_only=raw.get("kind") not in ("started", "interacted"))] if aid and (state or raw.get("kind") == "interacted") else []
    if kind not in ("collabAgentToolCall", "collab_agent_tool_call", "collab_tool_call"):
        return []
    spawn = raw.get("tool") in _CODEX_SPAWN and raw.get("status") != "failed"
    states = raw.get("agentsStates") or raw.get("agents_states") or {}
    ids = raw.get("receiverThreadIds") or raw.get("receiver_thread_ids") or []
    updates = []
    for aid in dict.fromkeys([*ids, *states]):
        state = states.get(aid) or {}
        if not isinstance(state, dict):
            state = {"status": state}
        # exec JSON 的旧版本用 {completed: "结果"}，app-server 用 {status, message}。
        status = state.get("status") or next(iter(state), "")
        result = state.get("message") or state.get(status)
        phase = _CODEX_STATES.get(status, "running" if spawn else "")
        if phase:
            updates.append(AgentUpdate(aid, phase, prompt=(raw.get("prompt") or "") if spawn else "",
                                       result=result if isinstance(result, str) else "",
                                       model=(raw.get("model") or "") if spawn else "", known_only=not spawn))
    return updates


_AGENT_ID = re.compile(r"(?im)^\s*agentId:\s*([\w-]+)")
_CLAUDE_STATES = {"completed": "success", "failed": "error", "stopped": "cancelled"}


class ClaudeAgents:
    def __init__(self) -> None:
        self.calls: dict[str, dict] = {}
        self.aliases: dict[str, str] = {}
        self.background: set[str] = set()
        self.known: set[str] = set()

    def updates(self, event: dict) -> tuple[list[AgentUpdate], bool]:
        """返回子会话更新，以及原始事件是否属于子会话自己的输出。"""
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype in ("task_started", "task_progress", "task_notification"):
            aid = event.get("task_id")
            tool = event.get("tool_use_id") or ""
            if not aid or event.get("ambient") or (event.get("task_type") not in (None, "local_agent", "remote_agent")):
                return [], False
            known = aid in self.known or tool in self.calls
            if not known and event.get("task_type") not in ("local_agent", "remote_agent"):
                return [], False
            if subtype == "task_started":
                self.known.add(aid)
                previous = self.aliases.get(tool, tool)
                if tool:
                    self.aliases[tool] = aid
                if event.get("is_backgrounded"):
                    self.background.add(tool)
                data = self.calls.get(tool, {})
                return [AgentUpdate(aid, "running", data.get("description") or event.get("description") or "",
                                    data.get("prompt") or "", model=data.get("model") or "",
                                    native_run=event.get("run_id") or tool, previous_id=previous)], False
            if subtype == "task_progress":
                label = event.get("description") or event.get("last_tool_name") or ""
                return [AgentUpdate(aid, native_run=event.get("run_id") or "", detail=label)], False
            return [AgentUpdate(aid, _CLAUDE_STATES.get(event.get("status"), ""),
                                result=event.get("summary") or "", native_run=event.get("run_id") or "")], False

        changes = []
        parent = event.get("parent_tool_use_id")
        aid = event.get("agent_id") or self.aliases.get(parent, parent)
        for block in (event.get("message") or {}).get("content", []) if kind in ("assistant", "user") else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task") and block.get("id"):
                self.calls[block["id"]] = block.get("input") or {}
            tool = block.get("tool_use_id")
            if block.get("type") != "tool_result" or tool not in self.calls:
                continue
            content = block.get("content") or ""
            result = "\n".join(c.get("text") or "" for c in content if isinstance(c, dict)) if isinstance(content, list) else str(content)
            match = _AGENT_ID.search(result)
            child = self.aliases.get(tool) or (match.group(1) if match else tool)
            if not block.get("is_error") or child != tool:
                if match:
                    self.aliases[tool] = child
                data = self.calls[tool]
                background = tool in self.background or data.get("run_in_background")
                status = "error" if block.get("is_error") else "" if background else "success"
                if status or child != tool:
                    changes.append(AgentUpdate(child, status, data.get("description") or "",
                                               data.get("prompt") or "", "" if background else result,
                                               previous_id=tool, known_only=bool(block.get("is_error"))))
        if aid and (parent or event.get("agent_id")) and kind in ("assistant", "user", "stream_event"):
            data = self.calls.get(parent, {})
            changes.insert(0, AgentUpdate(aid, title=data.get("description") or "", prompt=data.get("prompt") or "",
                                          model=data.get("model") or "", event=event, previous_id=parent or ""))
            return changes, True
        return changes, False
