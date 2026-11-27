import asyncio
import json
import threading
import time

import pytest


async def _wait_until(predicate):
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("CLI 子会话未按预期更新")


def test_one_cli_turn_creates_cards_only_after_native_events(client, monkeypatch):
    from workbench.db.repositories import TaskRepo
    from workbench.engines import get_engine
    from workbench.services.runner import Runner

    prompt = "请在一个会话里协作完成。这个措辞必须原样交给 CLI。"
    calls = []
    release = threading.Event()

    async def spawn(self, job, task, sent, *args):
        calls.append(sent)

        async def stream():
            yield json.dumps({"type": "thread.started", "thread_id": "root"})
            await _wait_until(release.is_set)
            for aid in ("one", "two"):
                item = {"type": "subAgentActivity", "kind": "started", "agentThreadId": aid,
                        "agentPath": f"/root/{aid}", "model": "gpt-test"}
                yield json.dumps({"type": "item.started", "item": item})
                yield json.dumps({"type": "item.completed", "item": item})
                yield json.dumps({"type": "item.completed", "item": {"type": "subAgentActivity",
                                  "kind": "interacted", "agentThreadId": "root", "agentPath": "/root"}})
                for event in ({"type": "turn.started"},
                              {"type": "item.completed", "item": {"type": "agent_message", "text": aid}},
                              {"type": "turn.completed"}):
                    yield json.dumps({"type": "wb", "subtype": "agent_event", "agent_id": aid,
                                      "turn_id": f"{aid}-turn", "event": event})
            yield json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "主会话回复"}})
            yield json.dumps({"type": "turn.completed"})

        return stream(), None, get_engine("codex"), False

    monkeypatch.setattr(Runner, "_spawn", spawn)
    response = client.post("/api/tasks", json={"project": "alpha", "engine": "codex", "run_now": True, "prompt": prompt})
    task_id = response.json()["id"]
    assert [row["id"] for row in client.get("/api/tasks").json()] == [task_id]
    release.set()
    for _ in range(100):
        rows = client.get("/api/tasks").json()
        if next(row for row in rows if row["id"] == task_id)["status"] == "review":
            break
        time.sleep(0.02)
    assert calls == [prompt]
    children = [row for row in rows if row["parent_task_id"] == task_id]
    assert {row["agent_id"] for row in children} == {"one", "two"}
    assert {row["agent_id"] for row in TaskRepo.agents(task_id)} == {"one", "two"}
    old_duplicate = TaskRepo.create(project="alpha", title="root", engine="codex", parent_task_id=task_id,
                                    agent_id="root", status="failed")
    assert old_duplicate not in {row["id"] for row in client.get("/api/tasks").json()}
    assert TaskRepo.get(old_duplicate) is not None
    for child in children:
        assert child["status"] == "review"
        detail = client.get(f"/api/tasks/{child['id']}").json()
        assert len(detail["runs"]) == 1
        events = client.get(f"/api/runs/{detail['runs'][0]['id']}/events").json()["events"]
        assert next(e["result"] for e in events if e["type"] == "result") == child["agent_id"]
        assert client.post(f"/api/tasks/{child['id']}/run", json={"message": "再做一次"}).status_code == 409
    root = client.get(f"/api/tasks/{task_id}").json()
    events = client.get(f"/api/runs/{root['runs'][0]['id']}/events").json()["events"]
    assert [e["item"]["text"] for e in events if (e.get("item") or {}).get("type") == "agent_message"] == ["主会话回复"]


def test_codex_child_result_can_arrive_after_parent_finishes(client, monkeypatch):
    from workbench.engines import get_engine
    from workbench.services.runner import Runner

    engine = get_engine("codex")
    child_finished = threading.Event()
    looked_up = threading.Event()

    async def spawn(self, job, task, sent, *args):
        async def stream():
            yield json.dumps({"type": "thread.started", "thread_id": "root"})
            yield json.dumps({"type": "wb", "subtype": "agent_event", "agent_id": "child",
                              "turn_id": "original", "event": {"type": "turn.started"}})
            yield json.dumps({"type": "turn.completed"})

        return stream(), None, engine, False

    async def agent_turns(thread_id):
        assert thread_id == "child"
        looked_up.set()
        finished = child_finished.is_set()
        matching = {"id": "original", "status": "completed" if finished else "inProgress",
                    "items": [{"type": "agentMessage", "text": "迟到的正确结果"}] if finished else []}
        unrelated = {"id": "different", "status": "failed"}
        return [unrelated, matching], not finished

    monkeypatch.setattr(Runner, "_spawn", spawn)
    monkeypatch.setattr(engine, "agent_turns", agent_turns)
    monkeypatch.setattr(engine, "available", lambda: True)
    root = client.post("/api/tasks", json={"project": "alpha", "engine": "codex", "run_now": True, "prompt": "验收"}).json()["id"]
    assert looked_up.wait(timeout=3)
    rows = client.get("/api/tasks").json()
    child, = [row for row in rows if row["parent_task_id"] == root]
    assert child["status"] == "unreported"
    assert next(row for row in rows if row["id"] == root)["status"] == "review"
    run_id = child["last_run_id"]

    child_finished.set()
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if client.get(f"/api/tasks/{child['id']}").json()["status"] == "review":
            break
        time.sleep(0.05)
    log = client.get(f"/api/runs/{run_id}/events").json()
    assert log["status"] == "success"
    assert next(event["result"] for event in log["events"] if event["type"] == "result") == "迟到的正确结果"


def test_claude_links_native_task_and_tool_ids_without_promoting_shell_tasks(client, env):
    from workbench.db.repositories import RunRepo, TaskRepo
    from workbench.services.agent_cards import AgentCards

    root = TaskRepo.create(project="alpha", title="Claude 主任务", engine="claude")
    cards = AgentCards(TaskRepo.get(root), "claude")
    tool = {"type": "tool_use", "id": "tool-A", "name": "Agent",
            "input": {"description": "核对数字", "prompt": "检查 2+3", "run_in_background": True}}
    cards.record({"type": "assistant", "message": {"content": [tool]}})
    cards.record({"type": "system", "subtype": "task_started", "task_type": "local_bash", "task_id": "shell"})
    assert not TaskRepo.agents(root)
    assert cards.record({"type": "assistant", "parent_tool_use_id": "tool-A",
                         "message": {"content": [{"type": "text", "text": "结果是 5"}]}})
    first_id = TaskRepo.agents(root)[0]["id"]
    cards.record({"type": "system", "subtype": "task_started", "task_type": "local_agent",
                  "task_id": "claude-A", "tool_use_id": "tool-A", "run_id": "run-1"})
    cards.record({"type": "system", "subtype": "task_notification", "task_id": "claude-A",
                  "run_id": "run-1", "status": "completed", "summary": "计算完成"})
    cards.close()
    child, = TaskRepo.agents(root)
    run, = RunRepo.of_task(child["id"])
    assert (child["id"], child["agent_id"], child["status"]) == (first_id, "claude-A", "review")
    assert run["result"] == "结果是 5"


def test_child_card_can_be_accepted_and_returned_to_review(client):
    from workbench.db.repositories import RunRepo, TaskRepo

    root = TaskRepo.create(project="alpha", title="主会话", status="review")
    child = TaskRepo.create(project="alpha", title="子会话", parent_task_id=root, agent_id="child")
    run_id = RunRepo.start_agent(child, "测试")
    path = f"/api/tasks/{child}"
    assert client.patch(path, json={"status": "done"}).status_code == 409
    RunRepo.finish(run_id, "success", 0, 0, "结果")
    TaskRepo.update(child, status="review")

    for status in ("done", "review"):
        response = client.patch(path, json={"status": status})
        assert response.status_code == 200
        assert response.json()["status"] == status
        assert response.json()["agent_id"] == "child"
        assert response.json()["parent_task_id"] == root
    for status in ("todo", "running", "failed", "cancelled"):
        assert client.patch(path, json={"status": status}).status_code == 409
    assert TaskRepo.get(child)["status"] == TaskRepo.get(root)["status"] == "review"
    assert RunRepo.get(run_id)["status"] == "success"


def test_delete_main_task_cleans_up_its_subtree(client, env):
    from workbench.db.repositories import RunRepo, TaskRepo
    from workbench.services import runlog

    owned_image = env.paths.uploads / "owned.png"
    shared_image = env.paths.uploads / "shared.png"
    for image in (owned_image, shared_image):
        image.write_bytes(b"fixture")

    root = TaskRepo.create(project="alpha", title="主会话")
    child = TaskRepo.create(project="alpha", title="子会话", parent_task_id=root, agent_id="child")
    nested = TaskRepo.create(project="alpha", title="下级会话", parent_task_id=child, agent_id="nested")
    sibling = TaskRepo.create(project="alpha", title="另一子会话", parent_task_id=root, agent_id="sibling")
    other = TaskRepo.create(project="alpha", title="独立主会话", images=json.dumps([str(shared_image)]))
    ids = (root, child, nested, sibling, other)
    runs = {}
    for task_id in ids:
        images = [str(owned_image), str(shared_image)] if task_id == nested else []
        run_id = runs[task_id] = RunRepo.enqueue(task_id, "测试", images)
        RunRepo.finish(run_id, "success", 0, 0, "测试回复")
        TaskRepo.update(task_id, status="review")
        runlog.append_wb(run_id, "end", status="success")

    TaskRepo.update(nested, status="running")
    assert client.delete(f"/api/tasks/{root}").status_code == 409
    assert all(TaskRepo.get(task_id) and runlog.path(runs[task_id]).exists() for task_id in ids)
    TaskRepo.update(nested, status="review")

    assert client.delete(f"/api/tasks/{sibling}").json() == {"ok": True}
    assert {t["id"] for t in client.get("/api/tasks").json()} == {root, child, nested, other}
    assert client.delete(f"/api/tasks/{root}").json() == {"ok": True}
    assert [t["id"] for t in client.get("/api/tasks").json()] == [other]
    for task_id in (root, child, nested, sibling):
        assert TaskRepo.get(task_id) is None and RunRepo.get(runs[task_id]) is None
        assert not runlog.path(runs[task_id]).exists()
    assert not owned_image.exists()
    assert shared_image.exists() and runlog.path(runs[other]).exists()
    assert client.delete(f"/api/tasks/{root}").json() == {"ok": True}


def test_failed_legacy_dispatch_recovers_as_todo_once(env):
    from contextlib import closing

    from workbench.db.connection import connect
    from workbench.db.repositories import TaskRepo
    from workbench.db.schema import init_db

    init_db()
    with closing(connect()) as conn, conn:
        conn.execute("""CREATE TABLE parallel_groups(id INTEGER PRIMARY KEY, project TEXT, title TEXT, prompt TEXT,
                        task_fields TEXT, state TEXT, updated_at REAL, single_task_id INTEGER)""")
        conn.execute("INSERT INTO parallel_groups VALUES (1, 'alpha', '原任务', '继续检查', ?, 'failed', 1, NULL)",
                     (json.dumps({"engine": "codex", "model": "gpt-test", "permission_mode": "plan"}),))
    init_db()
    init_db()
    task, = TaskRepo.list()
    assert (task["title"], task["description"], task["status"]) == ("原任务", "继续检查", "todo")
    assert (task["engine"], task["model"], task["permission_mode"]) == ("codex", "gpt-test", "plan")


def test_progress_from_engine_events():
    from workbench.engines.progress import event_progress

    assert event_progress("codex", {"type": "wb", "subtype": "thinking_delta", "item": "item_1", "text": "思考"}) is None
    assert event_progress("codex", {"type": "wb", "subtype": "plan", "plan": [
        {"step": "列出接口", "status": "completed"}, {"step": "实现接口", "status": "inProgress"},
        {"step": "验证", "status": "pending"},
    ]}) == (33, "实现接口")
    assert event_progress("codex", {"type": "item.updated", "item": {"type": "todo_list", "items": [
        {"text": "列出接口", "completed": True}, {"text": "验证", "completed": False},
    ]}}) == (50, "验证")
    assert event_progress("claude", {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
            {"content": "列出接口", "status": "completed"}, {"content": "实现", "status": "in_progress"},
        ]}},
    ]}}) == (50, "实现")
    assert event_progress("claude", {"type": "assistant", "parent_tool_use_id": "child", "message": {
        "content": [{"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
            {"content": "子代理计划", "status": "completed"},
        ]}}],
    }}) is None
