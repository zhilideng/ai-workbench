"""从 CLI 事件读取当前会话的执行进度。"""
from __future__ import annotations


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
