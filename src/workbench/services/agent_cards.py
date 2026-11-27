"""用 CLI 的子会话事件维护卡片和日志，执行由主会话所属的 CLI 负责。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from workbench.db.repositories import RunRepo, TaskRepo
from workbench.engines import get_engine
from workbench.engines.agents import AgentUpdate, ClaudeAgents, codex_agents
from workbench.engines.progress import event_progress
from workbench.services import runlog

_TASK_STATUS = {"success": "review", "error": "failed", "cancelled": "cancelled", "unreported": "unreported"}


@dataclass
class AgentCard:
    task: dict
    run_id: int | None = None
    native_run: str = ""
    result: str = ""
    observed_turn: bool = False
    status: str = ""


class AgentCards:
    def __init__(self, owner: dict, engine: str) -> None:
        self.owner = owner
        self.engine = engine
        self.root_sid = owner["session_id"]
        self.claude = ClaudeAgents() if engine == "claude" else None
        self.cards = {task["agent_id"]: AgentCard(task, status=task["status"]) for task in TaskRepo.agents(owner["id"])}

    def record(self, event: dict) -> bool:
        """将子会话自己的输出从主会话日志分流，保留主会话调用子代理的过程。"""
        if self.claude is not None:
            updates, child_output = self.claude.updates(event)
            for change in updates:
                self._apply(change)
            return child_output

        if event.get("type") == "thread.started":
            self.root_sid = event.get("thread_id") or self.root_sid
        if event.get("type") == "wb" and event.get("subtype") == "agent_event":
            child = event["event"]
            kind = child.get("type")
            state = {"turn.started": "running", "turn.completed": "success", "turn.failed": "error"}.get(kind, "")
            if child.get("interrupted"):
                state = "cancelled"
            self._apply(AgentUpdate(event["agent_id"], state, event=child, prompt=event.get("prompt") or "",
                                    native_run=event.get("turn_id") or "", observed_turn=bool(state),
                                    result=(child.get("error") or {}).get("message") or ""))
            event = child
            child_output = True
        else:
            child_output = False
        for change in codex_agents(event):
            self._apply(change)
        return child_output

    def _apply(self, change: AgentUpdate) -> None:
        if self.engine == "codex" and change.id == self.root_sid:
            return  # 子会话向主会话发送消息时，CLI 也会报告主会话的 ID。
        card = self.cards.get(change.id)
        if card is None and change.previous_id in self.cards:
            card = self.cards.pop(change.previous_id)
            card.task["agent_id"] = change.id
            TaskRepo.update(card.task["id"], touch=False, agent_id=change.id)
            self.cards[change.id] = card
        if card is None:
            if change.known_only:
                return
            data = {key: self.owner[key] for key in ("project", "permission_mode", "ssh_id", "source_path", "workdir")}
            title = self._title(change)
            task_id = TaskRepo.create(**data, parent_task_id=self.owner["id"], agent_id=change.id, engine=self.engine,
                                      title=title, description=change.prompt, model=change.model, effort=change.effort)
            card = self.cards[change.id] = AgentCard(TaskRepo.get(task_id))
        new_turn = change.status == "running" and change.native_run and change.native_run != card.native_run
        if change.native_run and card.native_run and change.native_run != card.native_run:
            if not new_turn:
                return
            if card.run_id is not None:
                self._finish(card, "unreported")
        if card.run_id is None and card.status and not new_turn:
            return
        turn_changed = change.native_run and change.native_run != card.native_run
        if turn_changed:
            card.native_run = change.native_run
        if change.observed_turn:
            card.observed_turn = True
        fields = {}
        if change.prompt and not card.task["description"]:
            fields["description"] = change.prompt
        if card.task["title"] == "子会话" and (change.title or change.prompt):
            fields["title"] = self._title(change)
        if fields:
            card.task.update(fields)
            TaskRepo.update(card.task["id"], touch=False, **fields)

        started = card.run_id is None
        if started:
            card.status, card.result = "", ""
            turn_id = card.native_run if self.engine == "codex" else ""
            card.run_id = RunRepo.start_agent(card.task["id"], card.task["description"] or card.task["title"], turn_id)
            runlog.append_wb(card.run_id, "start", prompt=card.task["description"],
                             cwd=card.task["workdir"], engine=self.engine, model=card.task["model"],
                             effort=card.task["effort"], images=[])
        elif turn_changed and self.engine == "codex":
            RunRepo.set_agent_turn(card.run_id, card.native_run)
        if change.event:
            if not started and "description" in fields:
                runlog.append_wb(card.run_id, "agent_assignment", text=fields["description"])
            self._write_event(card, change.event)
        if change.detail:
            TaskRepo.update(card.task["id"], touch=False, progress_label=change.detail)
            runlog.append_wb(card.run_id, "agent_progress", text=change.detail)
        if change.result and not card.result:
            card.result = change.result
        if change.status in _TASK_STATUS and (not card.observed_turn or change.observed_turn):
            self._finish(card, change.status)

    @staticmethod
    def _title(change: AgentUpdate) -> str:
        line = next((line.strip() for line in (change.title or change.prompt).splitlines() if line.strip()), "子会话")
        return line[:60] + ("…" if len(line) > 60 else "")

    def _write_event(self, card: AgentCard, event: dict) -> None:
        kind = event.get("type")
        if kind == "stream_event":
            delta = (event.get("event") or {}).get("delta") or {}
            subtype = {"text_delta": "delta", "thinking_delta": "thinking_delta"}.get(delta.get("type"))
            if subtype:
                runlog.append_wb(card.run_id, subtype, text=delta.get("text") or delta.get("thinking") or "")
            return
        if kind not in ("turn.failed", "agent_assignment"):
            runlog.append(card.run_id, event)
        progress = event_progress(self.engine, {**event, "parent_tool_use_id": None})
        if progress is not None:
            TaskRepo.update(card.task["id"], touch=False, progress=progress[0], progress_label=progress[1])
        item = event.get("item") or {}
        if kind == "item.completed" and item.get("type") == "agent_message" and item.get("text"):
            card.result = item["text"]
        if kind == "assistant":
            text = [c.get("text") or "" for c in (event.get("message") or {}).get("content", [])
                    if isinstance(c, dict) and c.get("type") == "text"]
            if text:
                card.result = "\n".join(text)

    @staticmethod
    def _finish(card: AgentCard, status: str) -> None:
        if card.result and status != "unreported":
            runlog.append(card.run_id, {"type": "result", "result": card.result, "is_error": status == "error"})
        runlog.append_wb(card.run_id, "end", status=status)
        RunRepo.finish(card.run_id, status, None, 0, card.result)
        TaskRepo.add_cost(card.task["id"], _TASK_STATUS[status], 0)
        card.run_id, card.status = None, status

    def close(self) -> None:
        for card in self.cards.values():
            if card.run_id is not None:
                self._finish(card, "unreported")


def _match_turn(run: dict, turns: list[dict]) -> dict | None:
    if run["agent_turn_id"]:
        return next((turn for turn in turns if turn.get("id") == run["agent_turn_id"]), None)
    # 旧记录只有开始时间。仅在本机时间吻合且唯一时回填，避免关联到另一轮。
    matches = [turn for turn in turns if turn.get("startedAt") is not None
               and abs(turn["startedAt"] - run["started_at"]) <= 3]
    return matches[0] if len(matches) == 1 else None


def _reconcile_turn(run: dict, turn: dict) -> None:
    status = {"completed": "success", "failed": "error", "interrupted": "cancelled"}.get(turn.get("status"))
    if not status:
        return
    saved = RunRepo.get(run["id"])
    if not saved or saved["status"] != "unreported":
        return
    reply = next((item.get("text") for item in reversed(turn.get("items") or [])
                  if item.get("type") == "agentMessage" and item.get("text")), "")
    result = (turn.get("error") or {}).get("message") or reply or saved["result"]
    if result:
        runlog.append(run["id"], {"type": "result", "result": result, "is_error": status == "error"})
    runlog.append_wb(run["id"], "end", status=status)
    RunRepo.reconcile_agent(run["id"], run["task_id"], status, _TASK_STATUS[status], result, turn.get("completedAt"))


async def reconcile_codex_agents() -> None:
    """主会话收尾后补读仍未回报的本机 Codex 子会话。"""
    engine = get_engine("codex")
    checked = set()
    while runs := [run for run in RunRepo.unreported_codex_agents() if run["id"] not in checked]:
        retry = False
        for run in runs:
            recent = run["ended_at"] and time.time() - run["ended_at"] < 30
            try:
                turns, active = await engine.agent_turns(run["agent_id"])
                turn = _match_turn(run, turns)
            except (RuntimeError, OSError, TimeoutError):
                if recent:
                    retry = True
                else:
                    checked.add(run["id"])
                continue
            if turn and turn.get("status") in ("completed", "failed", "interrupted"):
                checked.add(run["id"])
                _reconcile_turn(run, turn)
            elif active or recent:
                retry = True  # CLI 仍在执行，或历史稍晚才可读。
            else:
                checked.add(run["id"])
        if retry:
            await asyncio.sleep(3)
