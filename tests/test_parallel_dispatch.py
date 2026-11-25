import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest


def test_dispatch_preserves_single_task_and_original_prompt(client, monkeypatch):
    from workbench.services.runner import Runner

    started = []

    async def start(runner, task_id):
        started.append(task_id)

    monkeypatch.setattr(Runner, "start", start)
    for prompt in ("检查并行上传，再补测试", "把以下三项放在一个会话完成"):
        response = client.post("/api/dispatch", json={
            "project": "alpha", "prompt": prompt, "run_now": True,
        })
        assert response.status_code == 200
        result = response.json()
        assert result["group"] is None
        assert result["task"]["description"] == prompt
        assert result["task"]["id"] == started[-1]


def test_dispatch_creates_three_independent_cards_once(client, monkeypatch):
    from workbench.services import dispatch
    from workbench.services.runner import Runner

    calls = []
    started = []

    async def plan(settings, prompt):
        calls.append(prompt)
        return {
            "title": "通信网站功能完善",
            "tasks": [{"title": part, "prompt": f"实现{part}"} for part in ("多人聊天", "文件传输", "视频会议")],
        }

    async def start(runner, task_id):
        started.append(task_id)

    monkeypatch.setattr(dispatch, "make_plan", plan)
    monkeypatch.setattr(Runner, "start", start)
    body = {
        "project": "alpha", "prompt": "下面3个任务，每个任务对应一个会话去并行完成。多人聊天、文件传输、视频会议",
        "permission_mode": "plan", "run_now": True, "request_id": "parallel-test",
    }
    result = client.post("/api/dispatch", json=body)
    assert result.status_code == 200
    assert result.json()["task"] is None
    group_id = result.json()["group"]["id"]
    for _ in range(100):
        group = client.get("/api/dispatch").json()[0]
        if group["state"] != "planning":
            break
        time.sleep(0.01)
    assert group["state"] == "ready", group
    assert group["title"] == "通信网站功能完善"
    duplicate = client.post("/api/dispatch", json=body)
    assert duplicate.json()["group"]["id"] == group_id
    members = sorted(client.get("/api/tasks").json(), key=lambda task: task["parallel_position"])
    assert [task["title"] for task in members] == ["多人聊天", "文件传输", "视频会议"]
    assert {task["parallel_group_id"] for task in members} == {group_id}
    assert started == [task["id"] for task in members]
    assert calls == [body["prompt"]]


def test_parallel_progress_from_engine_events():
    from workbench.engines.planning import event_progress

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


def test_parallel_dispatch_intent_distinguishes_task_subject_from_instruction():
    from workbench.services.dispatch import parallel_intent

    assert not parallel_intent("不要开多个会话，修复登录和搜索")
    assert not parallel_intent("实现并行执行按钮，文案是「请并行处理」")
    assert parallel_intent("这两个需求，分开完成")
    assert parallel_intent("前端和后端分别在一个会话里完成")
    assert parallel_intent("启动十一个独立会话做下面的工作")
    assert not parallel_intent("开一个会话，实现一个支持多条对话的界面")
    assert not parallel_intent("开一个会话，并行处理以下两个任务：A 和 B")
    assert not parallel_intent("不要分别开会话，修复前端和后端")
    assert not parallel_intent("给工具栏添加一个创建多个会话的按钮")


def test_dispatch_accepts_pasted_numbered_task_list(client, monkeypatch):
    from workbench.services import dispatch
    from workbench.services.runner import Runner

    prompt = """’‘’并行执行下面三个任务：
1 在共享工作区创建一个 Python 文件
2 在该文件中哦功能写入一行打印“hello wo r d”的代码
3 在该文件中继续写入计算 3*3 的代码（无需打印结果）
4 将打印代码改成改为打印“hello World。！”
‘’‘"""
    items = [line[2:] for line in prompt.splitlines()[1:-1]]

    async def plan(settings, original):
        assert original == prompt
        return {"tasks": [{"prompt": item, "depends_on": [str(index)] if index else []}
                          for index, item in enumerate(items)]}

    async def start(runner, task_id):
        pass

    monkeypatch.setattr(dispatch, "make_plan", plan)
    monkeypatch.setattr(Runner, "start", start)
    result = client.post("/api/dispatch", json={"project": "alpha", "run_now": True, "prompt": prompt}).json()
    assert result["task"] is None
    group_id = result["group"]["id"]
    assert client.get(f"/api/dispatch/{group_id}").json()["state"] == "ready"
    members = sorted(client.get("/api/tasks").json(), key=lambda task: task["parallel_position"])
    assert [member["description"] for member in members] == items
    assert [[item["id"] for item in member["waiting_for"]] for member in members] == [
        [], *[[member["id"]] for member in members[:-1]],
    ]


def test_dispatch_publishes_single_planned_card_while_execution_slots_are_busy(client, env, monkeypatch):
    from workbench.services import dispatch
    from workbench.services.runner import Runner

    runner = client.app.state.runner
    started = []

    async def occupy_execution_slots():
        for _ in range(env.max_concurrent):
            await runner.sem.acquire()

    async def release_execution_slots():
        for _ in range(env.max_concurrent):
            runner.sem.release()

    async def plan(settings, prompt):
        await asyncio.sleep(0.05)
        return {"tasks": [{"prompt": "一起修改和验证共享文件"}]}

    async def start(runner, task_id):
        assert runner.sem.locked()
        started.append(task_id)

    monkeypatch.setattr(dispatch, "make_plan", plan)
    monkeypatch.setattr(Runner, "start", start)
    client.portal.call(occupy_execution_slots)
    try:
        result = client.post("/api/dispatch", json={"project": "alpha", "run_now": True,
            "prompt": "请开两个会话，修改并验证共享文件"}).json()
        group_id = result["group"]["id"]
        assert result["group"]["state"] == "planning"
        group = client.get(f"/api/dispatch/{group_id}").json()
        assert group["state"] == "ready"
        assert group["title"] == "请开两个会话，修改并验证共享文件"
        member, = client.get("/api/tasks").json()
        assert member["title"] == "一起修改和验证共享文件"
        assert [member["id"]] == started
    finally:
        client.portal.call(release_execution_slots)


def test_dispatch_schedules_dependencies_and_rejects_manual_bypass(client, monkeypatch):
    from workbench.db.repositories import TaskRepo
    from workbench.engines import get_engine
    from workbench.services import dispatch
    from workbench.services.runner import Runner

    outcomes = {"准备": None, "核对": None, "实现": None, "收尾": None}
    prompts = {}
    resumed = {}

    async def plan(settings, prompt):
        return {"title": "依赖调度", "tasks": [
            {"id": "prep", "title": "准备", "prompt": "准备共享文件"},
            {"id": "check", "title": "核对", "prompt": "核对无关文件"},
            {"id": "write", "title": "实现", "prompt": "使用共享文件", "depends_on": ["prep"]},
            {"id": "final", "title": "收尾", "prompt": "收尾", "depends_on": ["check", "write"]},
        ]}

    async def spawn(runner, job, task, prompt, resume_sid, *args):
        prompts[task["title"]] = prompt
        resumed[task["title"]] = resume_sid
        TaskRepo.update(job.task_id, status="running")

        async def lines():
            async with asyncio.timeout(5):
                while outcomes[task["title"]] is None:
                    await asyncio.sleep(0.01)
            yield json.dumps({"type": "thread.started", "thread_id": resume_sid or f"session-{job.task_id}"})
            yield json.dumps({"type": "turn.completed" if outcomes[task["title"]] else "turn.failed"})

        return lines(), None, get_engine("codex"), False

    def wait_for(expected):
        for _ in range(175):
            rows = {task["title"]: task for task in client.get("/api/tasks").json()}
            if all(rows.get(title, {}).get("status") == status for title, status in expected.items()):
                return rows
            time.sleep(0.02)
        raise AssertionError({title: task["status"] for title, task in rows.items()})

    monkeypatch.setattr(dispatch, "make_plan", plan)
    monkeypatch.setattr(Runner, "_spawn", spawn)
    response = client.post("/api/dispatch", json={"project": "alpha", "engine": "codex", "run_now": True,
        "prompt": "请开三个独立会话，并行完成下面的任务：\n- 准备\n- 核对\n- 实现\n- 收尾"})
    assert response.status_code == 200
    try:
        rows = wait_for({"准备": "running", "核对": "running", "实现": "todo", "收尾": "todo"})
        prep, write, final = [f"/api/tasks/{rows[title]['id']}" for title in ("准备", "实现", "收尾")]
        assert [item["title"] for item in rows["实现"]["waiting_for"]] == ["准备"]
        assert client.get(write).json()["waiting_for"] == rows["实现"]["waiting_for"]
        assert client.post(f"{write}/run", json={"message": "提前执行"}).status_code == 409
        assert client.patch(write, json={"status": "running"}).status_code == 409
        assert client.patch(write, json={"status": "done"}).status_code == 409
        assert client.delete(prep).status_code == 409
        outcomes["准备"] = False
        rows = wait_for({"准备": "failed", "实现": "todo"})
        assert rows["实现"]["waiting_for"][0]["status"] == "failed"
        assert client.delete(prep).status_code == 409
        assert client.patch(prep, json={"status": "done"}).status_code == 200
        assert client.post(f"{write}/run", json={"message": "跳过实际执行"}).status_code == 409
        outcomes["准备"] = True
        assert client.post(f"{prep}/run", json={}).status_code == 200
        wait_for({"准备": "review", "实现": "running", "收尾": "todo"})
        assert "工作台已确认以下前置任务执行成功：准备" in prompts["实现"]
        outcomes["实现"] = True
        wait_for({"实现": "review", "收尾": "todo"})
        assert client.post(f"{final}/run", json={}).status_code == 409
        outcomes["核对"] = True
        wait_for({"核对": "review", "收尾": "running"})
        assert "工作台已确认以下前置任务执行成功：核对、实现" in prompts["收尾"]
        outcomes["收尾"] = True
        rows = wait_for({"收尾": "review"})
        assert len(client.get(final).json()["runs"]) == 1
        assert client.patch(write, json={"status": "done"}).status_code == 200

        outcomes.update({"准备": None, "实现": None, "收尾": None})
        assert client.post(f"{prep}/run", json={"message": "修正准备结果"}).status_code == 200
        pending = wait_for({"准备": "running", "实现": "todo", "收尾": "todo", "核对": "review"})
        assert pending["实现"]["progress"] is None
        assert [t["title"] for t in pending["实现"]["waiting_for"]] == ["准备"]
        assert [t["title"] for t in pending["收尾"]["waiting_for"]] == ["实现"]
        outcomes["准备"] = False
        wait_for({"准备": "failed", "实现": "todo", "收尾": "todo"})
        assert client.post(f"{write}/run", json={}).status_code == 409
        outcomes["准备"] = True
        assert client.post(f"{prep}/run", json={"message": "再试一次"}).status_code == 200
        wait_for({"准备": "review", "实现": "running", "收尾": "todo"})
        assert resumed["实现"] == rows["实现"]["session_id"]
        assert prompts["实现"] == "前置任务「准备」已更新。检查是否影响你的结果，仅在需要时修正或补做。"
        outcomes["实现"] = True
        wait_for({"实现": "review", "收尾": "running"})
        assert resumed["收尾"] == rows["收尾"]["session_id"]
        assert prompts["收尾"] == "前置任务「实现」已更新。检查是否影响你的结果，仅在需要时修正或补做。"
        outcomes["收尾"] = True
        wait_for({"收尾": "review"})
        assert len(client.get(final).json()["runs"]) == 2
        assert len(client.get(f"/api/tasks/{rows['核对']['id']}").json()["runs"]) == 1

        # 下游正在续接时，先让该轮收尾，上游再修改共享产物。
        outcomes.update({"准备": None, "实现": None, "收尾": None})
        assert client.post(f"{write}/run", json={"message": "核对实现结果"}).status_code == 200
        wait_for({"实现": "running", "收尾": "todo"})
        assert client.post(f"{prep}/run", json={"message": "再调整准备结果"}).status_code == 200
        wait_for({"准备": "queued", "实现": "running", "收尾": "todo"})
        outcomes["实现"] = True
        wait_for({"准备": "running", "实现": "todo", "收尾": "todo"})
        outcomes["准备"] = True
        wait_for({"准备": "review", "实现": "review", "收尾": "running"})
        outcomes["收尾"] = True
        wait_for({"收尾": "review"})
        assert len(client.get(write).json()["runs"]) == 4
        assert len(client.get(final).json()["runs"]) == 3
    finally:
        outcomes.update({title: True for title in outcomes})


def test_invalid_dependency_plan_creates_no_tasks(client, monkeypatch):
    from workbench.services import dispatch

    async def plan(settings, prompt):
        return {"title": "循环依赖", "tasks": [
            {"id": "one", "title": "一", "prompt": "一", "depends_on": ["two"]},
            {"id": "two", "title": "二", "prompt": "二", "depends_on": ["one"]},
        ]}

    monkeypatch.setattr(dispatch, "make_plan", plan)
    response = client.post("/api/dispatch", json={
        "project": "alpha", "prompt": "请并行完成两个任务", "run_now": True,
    })
    assert response.status_code == 200
    for _ in range(100):
        group = client.get("/api/dispatch").json()[0]
        if group["state"] == "failed":
            break
        time.sleep(0.01)
    assert group["state"] == "failed"
    assert "循环依赖" in group["error"]
    assert client.get("/api/tasks").json() == []


def test_startup_resumes_ready_tasks_but_keeps_unfinished_dependencies_waiting(env, monkeypatch):
    from fastapi.testclient import TestClient

    from workbench.api.app import create_app
    from workbench.db import init_db
    from workbench.db.repositories import ParallelGroupRepo, RunRepo, TaskRepo
    from workbench.services.runner import Runner

    init_db()
    fields = {"project": "alpha", "title": "依赖恢复", "description": "恢复计划"}
    group = ParallelGroupRepo.create("startup-plan", fields)
    ParallelGroupRepo.create_members(group, fields["title"], [
        {**fields, "title": title} for title in ("已完成", "可以开始", "继续等待")
    ], [[], [0], [1]])
    members = sorted(TaskRepo.list(), key=lambda row: row["id"])
    finished, ready, blocked = [row["id"] for row in members]
    run_id = RunRepo.enqueue(finished, "准备", [])
    RunRepo.finish(run_id, "success", 0, 0, "已完成")
    TaskRepo.add_cost(finished, "review", 0)
    started = []

    async def start(runner, task_id):
        started.append(task_id)

    monkeypatch.setattr(Runner, "start", start)
    with TestClient(create_app()) as client:
        assert started == [ready]
        waiting = client.get(f"/api/tasks/{blocked}").json()
        assert waiting["status"] == "todo"
        assert [t["id"] for t in waiting["waiting_for"]] == [ready]


def test_split_failure_retries_same_group_and_uses_normal_permission_controls(client, monkeypatch):
    from workbench.services import dispatch
    from workbench.services.runner import Runner

    calls = []

    async def plan(settings, prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return {"tasks": [], "blocked_reason": "没有可执行的检查"}
        return {"title": "三个检查", "tasks": [
            {"title": f"检查 {i}", "prompt": f"只读检查模块 {i}"} for i in range(3)
        ]}

    async def start(runner, task_id, *args):
        pass

    monkeypatch.setattr(dispatch, "make_plan", plan)
    monkeypatch.setattr(Runner, "start", start)
    response = client.post("/api/dispatch", json={
        "project": "alpha", "prompt": "请各开一个会话并行完成以下三个任务。", "run_now": True,
        "permission_mode": "plan",
    })
    group_id = response.json()["group"]["id"]
    for _ in range(100):
        group = client.get("/api/dispatch").json()[0]
        if group["state"] == "failed":
            break
        time.sleep(0.01)
    assert group["state"] == "failed"
    assert client.get("/api/tasks").json() == []
    assert client.post(f"/api/dispatch/{group_id}/retry").json()["group"]["id"] == group_id
    for _ in range(100):
        if client.get("/api/dispatch").json()[0]["state"] == "ready":
            break
        time.sleep(0.01)
    members = client.get("/api/tasks").json()
    assert len(members) == 3
    assert {member["permission_mode"] for member in members} == {"plan"}
    path = f"/api/tasks/{members[0]['id']}"
    assert client.patch(path, json={"permission_mode": "acceptEdits"}).status_code == 200
    assert client.post(f"{path}/run", json={"message": "检查结果", "permission_mode": "plan"}).status_code == 200
    assert client.get(path).json()["permission_mode"] == "plan"


@pytest.mark.parametrize("with_git", [False, True])
def test_three_cli_turns_share_source_and_card_resumes_twice(client, env, tmp_path, monkeypatch, with_git):
    from workbench.core.shell import find_bin

    source = (Path(env.projects_root) / "alpha").resolve()
    if with_git:
        _git(source, "init", "-q")
        _git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "--allow-empty", "-qm", "base")
    permit = tmp_path / "complete"
    cli = tmp_path / "codex"
    cli.write_text(f'''#!{sys.executable}
import json
import sys
import time
from pathlib import Path

def send(event):
    print(json.dumps(event), flush=True)

args = sys.argv[1:]
if args[0] == "app-server":
    sys.exit(1)
if "--ephemeral" in args:
    plan = {{"title": "三个文件", "tasks": [{{"title": title, "prompt": title}} for title in ("登录", "搜索", "测试")]}}
    send({{"type": "item.completed", "item": {{"type": "agent_message", "text": json.dumps(plan)}}}})
else:
    resumed = args[1] == "resume"
    sid = args[2] if resumed else "test-" + args[-1]
    Path(sid + ".txt").write_text(args[-1], encoding="utf-8")
    send({{"type": "thread.started", "thread_id": sid}})
    send({{"type": "item.updated", "item": {{"type": "todo_list", "items": [
        {{"text": "开始", "completed": True}}, {{"text": "实现", "completed": False}},
    ]}}}})
    for _ in range(250):
        if Path({str(permit)!r}).exists():
            break
        time.sleep(0.02)
    send({{"type": "item.completed", "item": {{"type": "agent_message", "text": args[-1]}}}})
send({{"type": "turn.completed"}})
''')
    cli.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(cli))
    find_bin.cache_clear()

    def wait_members(check):
        for _ in range(250):
            members = client.get("/api/tasks").json()
            if len(members) == 3 and check(members):
                return members
            time.sleep(0.02)
        raise AssertionError(members)

    try:
        result = client.post("/api/dispatch", json={
            "project": "alpha", "run_now": True, "engine": "codex", "permission_mode": "acceptEdits",
            "use_worktree": with_git,
            "prompt": "请各开一个会话并行完成三个任务：登录、搜索、测试。",
        })
        assert result.status_code == 200
        assert result.json()["group"]["state"] == "planning", result.json()
        members = wait_members(lambda rows: all(row["status"] == "running" and row["progress"] == 50 for row in rows))
        assert len({row["session_id"] for row in members}) == 3
        assert {Path(row["workdir"]).resolve() for row in members} == {source}
        assert all(not row["use_worktree"] and not row["branch"] for row in members)
        assert len(list(source.glob("test-*.txt"))) == 3
        permit.touch()
        members = wait_members(lambda rows: all(row["status"] == "review" for row in rows))
        assert [row["progress"] for row in members] == [100, 100, 100]
        member = members[0]
        path = f"/api/tasks/{member['id']}"
        for message in ("调整第一次", "调整第二次"):
            assert client.post(f"{path}/run", json={"message": message}).status_code == 200
            wait_members(lambda rows: all(row["status"] == "review" for row in rows))
        final = client.get(path).json()
        assert final["session_id"] == member["session_id"]
        assert len(final["runs"]) == 3
        assert (source / f"{member['session_id']}.txt").read_text() == "调整第二次"
        assert client.post(f"/api/dispatch/{result.json()['group']['id']}/apply").status_code in (404, 405)
        if with_git:
            assert _git(source, "worktree", "list", "--porcelain").count("worktree ") == 1
        assert sorted(run["prompt"] for run in final["runs"] if run["prompt"].startswith("调整")) == [
            "调整第一次", "调整第二次",
        ]
    finally:
        permit.touch()
        find_bin.cache_clear()


def _git(path, *args):
    return subprocess.run(["git", *args], cwd=path, capture_output=True, check=True, text=True).stdout.strip()


def test_existing_parallel_worktree_is_preserved_and_missing_one_uses_source(client, env):
    from workbench.db.repositories import TaskRepo
    from workbench.services.runner import ensure_workdir

    source = Path(env.projects_root) / "alpha"
    old_dir = env.paths.worktrees / "alpha" / "task-previous"
    old_dir.mkdir(parents=True)
    prior_work = old_dir / "existing.txt"
    prior_work.write_text("旧会话的改动", encoding="utf-8")
    shared = {"project": "alpha", "source_path": str(source), "parallel_group_id": 42,
              "use_worktree": 1, "branch": "wb/parallel-old"}
    previous = TaskRepo.create(title="已有会话", workdir=str(old_dir), **shared)
    missing = TaskRepo.create(title="尚无工作区的会话", workdir=str(old_dir / "missing"), **shared)

    assert ensure_workdir(TaskRepo.get(previous)) == old_dir
    assert prior_work.read_text(encoding="utf-8") == "旧会话的改动"
    assert ensure_workdir(TaskRepo.get(missing)) == source
    assert not TaskRepo.get(missing)["branch"] and not TaskRepo.get(missing)["use_worktree"]
