"""接口冒烟测试：不启动真实的 claude / codex。"""


def test_config_and_projects(client, env):
    cfg = client.get("/api/config").json()
    assert cfg["projects_root"] == str(env.projects_root)
    assert set(cfg["engines"]) == {"claude", "codex"}
    assert "claude_efforts" in cfg and "codex_models" in cfg
    names = [p["name"] for p in client.get("/api/projects").json()]
    assert names == ["alpha", "beta"]


def test_task_crud(client):
    r = client.post("/api/tasks", json={"project": "alpha", "prompt": "第一行标题\n详细说明"})
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["title"] == "第一行标题" and t["status"] == "todo"
    assert client.patch(f"/api/tasks/{t['id']}", json={"status": "done"}).json()["status"] == "done"
    detail = client.get(f"/api/tasks/{t['id']}").json()
    assert detail["runs"] == [] and detail["resume_cmd"] == ""
    assert [x["id"] for x in client.get("/api/tasks?project=alpha").json()] == [t["id"]]
    assert client.delete(f"/api/tasks/{t['id']}").json() == {"ok": True}
    assert client.get(f"/api/tasks/{t['id']}").status_code == 404


def test_run_activity_uses_current_run_and_incremental_offsets(client):
    import json

    from workbench.db.repositories import RunRepo, TaskRepo
    from workbench.services import runlog

    task_id = client.post("/api/tasks", json={"project": "alpha", "prompt": "检查"}).json()["id"]
    old_run = RunRepo.enqueue(task_id, "previous", [])
    RunRepo.finish(old_run, "success", 0, 0, "")
    run_id = RunRepo.enqueue(task_id, "current", [])
    assert client.get("/api/tasks").json()[0]["active_run_id"] is None
    TaskRepo.update(task_id, status="running")
    assert client.get("/api/tasks?project=alpha").json()[0]["active_run_id"] == run_id

    url = f"/api/runs/{run_id}/events"
    runlog.append_wb(run_id, "start", prompt="current")
    with runlog.path(run_id).open("a", encoding="utf-8") as log:
        # 旧日志没有时间字段，也能显示已有的步骤。
        event = {"type": "item.started", "item": {"type": "command_execution", "id": "1", "command": "echo sample"}}
        log.write(json.dumps(event) + "\n")
    first = client.get(url, params={"activity": True}).json()
    start, command = first["events"]
    assert start["label"] == "开始执行任务" and start["time"] > 0
    assert command == {
        "key": "item:1", "time": None, "label": "输出文本", "state": "running", "operation": True,
    }
    assert client.get(url).json()["events"][1]["item"]["command"] == "echo sample"

    with runlog.path(run_id).open("a", encoding="utf-8") as log:
        runlog.write_event(log, {"type": "item.completed", "item": {
            **event["item"], "exit_code": 1, "aggregated_output": "sample error\n",
        }})
    next_page = client.get(url, params={"activity": True, "offset": first["offset"]}).json()
    assert [(e["key"], e["state"]) for e in next_page["events"]] == [("item:1", "failed")]
    assert next_page["events"][0]["label"] == "输出文本（退出码 1）"
    assert "sample error" not in str(next_page["events"])
    assert client.get(url).json()["events"][-1]["item"]["aggregated_output"] == "sample error\n"
    assert next_page["offset"] > first["offset"]
    RunRepo.finish(run_id, "error", 1, 0, "")
    assert client.get("/api/tasks").json()[0]["active_run_id"] is None
    TaskRepo.update(task_id, status="failed")
    assert client.get("/api/tasks").json()[0]["last_run_id"] == run_id


def test_run_activity_summarizes_work_and_leaves_raw_output_in_details(client):
    from workbench.db.repositories import RunRepo
    from workbench.services import runlog

    task_id = client.post("/api/tasks", json={"project": "alpha", "prompt": "检查"}).json()["id"]
    run_id = RunRepo.enqueue(task_id, "检查", [])
    with runlog.path(run_id).open("a", encoding="utf-8") as log:
        for event in (
            {"type": "item.completed", "item": {"type": "reasoning", "summary": ["先确定合肥的天气来源。"],
             "content": ["完整内部记录"]}},
            {"type": "item.completed", "item": {"type": "agent_message", "phase": "commentary",
             "text": "我先核对今天的预报。"}},
            {"type": "item.completed", "item": {"type": "agent_message", "phase": "final_answer", "text": "最终回复"}},
            {"type": "item.completed", "item": {"type": "web_search", "id": "w",
             "action": {"type": "search", "queries": ["合肥天气", "合肥今日降雨"]},
             "results": [{"type": "text_result", "snippet": "网页原始返回"}]}},
            {"type": "item.completed", "item": {"type": "web_search", "id": "page",
             "action": {"type": "findInPage", "url": "https://example.test/weather?token=qa-hidden", "pattern": "合肥"}}},
            {"type": "item.completed", "item": {"type": "command_execution", "id": "c",
             "command": "/bin/zsh -lc 'API_TOKEN=qa-hidden python3 --version'", "cwd": "/tmp/qa",
             "aggregated_output": "\x1b[32mPython 3.14\x1b[0m\nAuthorization: Bearer qa-hidden", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "file_change", "id": "f",
             "changes": [{"kind": "update", "path": "src/worker.py"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "b",
             "input": {"command": "pwd", "description": "检查目录"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "b",
             "content": [{"type": "text", "text": "/tmp/qa"}]}]}},
        ):
            runlog.write_event(log, event)

    rows = client.get(f"/api/runs/{run_id}/events", params={"activity": True}).json()["events"]
    thought, update, search, page, command, changed, bash, result = rows
    assert thought["label"] == "思考 · 先确定合肥的天气来源。"
    assert update["label"] == "进展 · 我先核对今天的预报。"
    assert "operation" not in thought and "operation" not in update
    assert search["label"] == "搜索网页 · 合肥天气；合肥今日降雨"
    assert page["label"] == "在网页中查找 · 合肥（example.test/weather）"
    assert command["label"] == "检查 Python 版本"
    assert not any(value in str(rows) for value in ("qa-hidden", "网页原始返回", "完整内部记录", "最终回复", "Authorization"))
    assert changed["label"] == "修改 src/worker.py"
    assert all(row["operation"] for row in (search, page, command, changed, bash))
    assert (bash["key"], bash["label"]) == ("tool:b", "检查目录")
    assert (result["key"], result["state"], result["label"]) == ("tool:b", "done", "")


def test_run_activity_describes_shell_and_connector_operations():
    from workbench.services.run_activity import activity_updates

    def label(command, actions=()):
        row, = activity_updates({"type": "item.started", "item": {
            "type": "command_execution", "command": command, "command_actions": actions,
        }})
        return row["label"]

    assert label("pnpm install && pnpm run build") == "安装依赖；构建项目"
    assert label("python -m venv .venv\npython -m pip install example") == "创建虚拟环境；安装依赖"
    assert label("printf 'test && fake_command' | grep test") == "输出文本；搜索内容"
    assert label("cat <<'EOF'\nany_text && fake_command\nEOF") == "读取文件"
    assert label("rg TODO | sort", [
        {"type": "search", "query": "TODO"}, {"type": "unknown", "command": "sort"},
    ]) == "搜索内容 · TODO；排序数据"
    assert label("lsof -i :8765; curl http://localhost:8765/api?secret=qa-hidden") == (
        "检查打开的文件或端口；请求网页 · localhost:8765/api"
    )
    assert label("scp report.txt demo:/tmp; tar -xzf example.tar.gz") == "传输远程文件；解压文件"
    assert label("gh repo view demo/example && gh issue list") == "读取仓库；读取 Issue"
    row, = activity_updates({"type": "item.completed", "item": {
        "id": "m1", "type": "mcp_tool_call", "tool": "list_issues",
        "arguments": {"owner": "demo", "repo": "example"}, "success": False,
    }})
    assert (row["label"], row["state"]) == ("读取 Issue 列表", "failed")
    row, = activity_updates({"type": "item.started", "item": {
        "id": "m2", "type": "mcp_tool_call", "server": "documents", "tool": "search",
        "arguments": {"query": "项目说明"},
    }})
    assert row["label"] == "搜索数据 · 项目说明"
    row, = activity_updates({"type": "item.started", "item": {
        "id": "a1", "type": "other", "item_type": "collabAgentToolCall", "raw": {"tool": "wait"},
    }})
    assert (row["label"], row["state"]) == ("等待子任务", "running")


def test_run_activity_distinguishes_tool_startup_from_data_source_connections():
    from workbench.services.run_activity import activity_updates

    for status, label, state in (
        ("starting", "初始化工具服务", "running"),
        ("ready", "工具服务已就绪", "done"),
        ("failed", "工具服务初始化失败", "failed"),
        ("authenticationRequired", "工具服务需要认证", "failed"),
        ("cancelled", "工具服务初始化已取消", "interrupted"),
    ):
        row, = activity_updates({"type": "wb", "subtype": "mcp_status", "source": ":docs", "status": status})
        assert (row["label"], row["state"], row["key"], row.get("operation")) == (label, state, "mcp::docs", None)

    rows = list(activity_updates({"type": "system", "subtype": "init", "mcp_servers": [
        {"name": "docs", "status": "connected"}, {"name": "repo", "status": "needs-auth"},
    ]}))
    assert [(row["label"], row["state"]) for row in rows] == [
        ("工具服务已就绪", "done"), ("工具服务需要认证", "failed"),
    ]
    row, = activity_updates({"type": "item.completed", "item": {
        "id": "c", "type": "mcp_tool_call", "tool": "connect_database", "status": "completed",
    }})
    assert (row["label"], row["state"], row["operation"]) == ("连接数据源", "done", True)


def test_validation_errors(client):
    assert client.post("/api/tasks", json={"project": "nope", "prompt": "x"}).status_code == 404
    assert client.post("/api/tasks", json={"project": "alpha", "prompt": " "}).status_code == 400
    r = client.post("/api/tasks", json={"project": "alpha", "prompt": "x", "engine": "gpt"})
    assert r.status_code == 400 and "engine" in r.json()["detail"]
    r = client.post("/api/tasks", json={"project": "alpha", "prompt": "x", "effort": "BAD!"})
    assert r.status_code == 400


def test_upload_roundtrip(client):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    r = client.post("/api/uploads?name=a.png", content=png, headers={"Content-Type": "image/png"})
    assert r.status_code == 200, r.text
    assert client.get(r.json()["url"]).content == png
    assert client.post("/api/uploads?name=a.txt", content=b"x", headers={"Content-Type": "text/plain"}).status_code == 400


def test_ssh_crud(client):
    body = {"name": "dev", "host": "example.com", "user": "me", "port": 2222, "remote_dir": "~/app"}
    lst = client.post("/api/projects/alpha/ssh", json=body).json()
    assert lst[0]["target"] == "me@example.com:2222" and lst[0]["connected"] is False
    assert "ssh -t -p 2222 me@example.com" in lst[0]["command"]
    assert client.post("/api/projects/alpha/ssh", json=body).status_code == 409
    assert client.post("/api/projects/alpha/ssh", json={**body, "name": "x", "host": "-oProxy"}).status_code == 400
    cid = lst[0]["id"]
    assert client.delete(f"/api/projects/alpha/ssh/{cid}").json() == []


def test_ssh_connection_is_shared_between_projects(client):
    from workbench.services.connections import ai_context

    body = {"name": "共享测试连接", "host": "example.com", "remote_dir": "/srv/app"}
    created = client.post("/api/projects/alpha/ssh", json=body)
    assert created.status_code == 200, created.text
    cid = created.json()[0]["id"]
    assert [c["id"] for c in client.get("/api/projects/beta/ssh").json()] == [cid]
    assert [p["ssh_count"] for p in client.get("/api/projects").json()] == [1, 1]
    assert "example.com" in ai_context("alpha")
    assert ai_context("beta") == ""

    updated = client.put(f"/api/ssh/folder-connections/{cid}", json={**body, "name": "目录中更名"})
    assert updated.status_code == 200, updated.text
    assert client.get("/api/project-folders/ssh").json()[0]["name"] == "目录中更名"
    assert client.get("/api/projects/beta/ssh").json()[0]["name"] == "目录中更名"

    updated = client.put(f"/api/projects/beta/ssh/{cid}", json={**body, "host": "updated.example.com"})
    assert updated.status_code == 200, updated.text
    assert client.get("/api/projects/alpha/ssh").json()[0]["host"] == "updated.example.com"

    task = client.post("/api/tasks", json={"project": "beta", "prompt": "检查", "ssh_id": cid})
    assert task.status_code == 200, task.text
    detail = client.get(f"/api/tasks/{task.json()['id']}").json()
    assert detail["ssh_target"] == "updated.example.com"
    assert client.delete(f"/api/projects/beta/ssh/{cid}").json() == []
    assert client.get("/api/projects/alpha/ssh").json() == []


def test_settings_change_root(client, tmp_path):
    other = tmp_path / "other"
    (other / "gamma").mkdir(parents=True)
    cfg = client.put("/api/settings", json={"projects_root": str(other)}).json()
    assert cfg["projects_root"] == str(other)
    assert [p["name"] for p in client.get("/api/projects").json()] == ["gamma"]
    assert client.put("/api/settings", json={"projects_root": str(tmp_path / "missing")}).status_code == 400


def test_frontend_served(client):
    html = client.get("/").text
    assert 'src="js/app.js"' in html
    for path in ("/js/app.js", "/css/base.css", "/vendor/vue.global.prod.js", "/fonts/fonts.css"):
        assert client.get(path).status_code == 200, path


def test_project_sources_pin_task_directory(client, env, tmp_path):
    first = env.projects_root / "alpha"
    second = tmp_path / "source folder"
    second.mkdir()
    payload = {"name": "自选项目", "sources": [{"path": str(first)}, {"path": str(second)}]}
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201, response.text
    project = next(p for p in response.json() if p["name"] == payload["name"])
    assert [s["path"] for s in project["sources"]] == [str(first), str(second)]
    assert client.post("/api/projects", json=payload).status_code == 409

    task = client.post("/api/tasks", json={
        "project": project["name"], "prompt": "检查", "source_path": str(second),
    }).json()
    response = client.put(f"/api/projects/{project['name']}/sources", json={"sources": [{"path": str(first)}]})
    assert response.status_code == 200 and second.is_dir()

    from workbench.services.runner import ensure_workdir
    assert ensure_workdir(task) == second
    assert client.post("/api/tasks", json={
        "project": project["name"], "prompt": "检查", "source_path": str(second),
    }).status_code == 400


def test_folder_picker_and_remote_project(client, env, monkeypatch):
    from workbench.services import connections
    from workbench.ssh import transport

    local = client.get("/api/project-folders/local")
    assert local.json()["folders"] == ["alpha", "beta"]
    assert client.get("/api/project-folders/local", params={"path": str(env.projects_root / "missing")}).status_code == 404

    ssh = client.post("/api/projects/alpha/ssh", json={
        "name": "开发服务器", "host": "example.com", "remote_dir": "/srv",
    }).json()[0]
    assert client.get("/api/project-folders/ssh").json()[0]["id"] == ssh["id"]

    async def list_folders(conn, command, *, timeout):
        assert conn["id"] == ssh["id"]
        assert "dir='/srv/with space'" in command
        return 0, "空目录\0source\0", ""

    monkeypatch.setattr(transport, "run", list_folders)
    response = client.get(f"/api/project-folders/ssh/{ssh['id']}", params={"path": "/srv/with space"})
    assert response.json()["folders"] == ["source", "空目录"]

    source = {"path": "/srv/with space/source", "ssh_id": ssh["id"]}
    assert client.post("/api/projects", json={"name": "远程项目", "sources": [source]}).status_code == 201
    task = client.post("/api/tasks", json={
        "project": "远程项目", "prompt": "检查", "ssh_id": ssh["id"], "source_path": source["path"],
    }).json()
    assert connections.of_task(task)["remote_dir"] == source["path"]
    assert client.get(f"/api/tasks/{task['id']}").json()["ssh_project"] == "alpha"


def test_remember_folder_and_fall_back_when_removed(client, env):
    folder = env.projects_root / "alpha"
    assert client.put("/api/project-folders/recent", json={"path": str(folder)}).json() == {"ok": True}
    assert client.get("/api/project-folders/local").json()["path"] == str(folder)
    folder.rmdir()
    assert client.get("/api/project-folders/local").json()["path"] == str(env.projects_root)


def test_local_folder_uses_directory_spelling(client, tmp_path):
    folder = tmp_path / "Parent" / "Shared"
    folder.mkdir(parents=True)
    alias = tmp_path / "parent" / "shared"
    response = client.get("/api/project-folders/local", params={"path": str(alias)})
    if not alias.is_dir():
        assert response.status_code == 404
        return
    assert response.status_code == 200, response.text
    assert response.json()["path"] == str(folder)

    sources = [{"path": str(folder)}, {"path": str(alias)}]
    assert client.post("/api/projects", json={"name": "重复目录", "sources": sources}).status_code == 400


def test_create_ssh_connection_before_project(client, monkeypatch, tmp_path):
    import os
    import subprocess

    from workbench.ssh import transport

    body = {"name": "新连接", "host": "example.com", "share_with_ai": False}
    response = client.post("/api/ssh/folder-connections", json=body)
    assert response.status_code == 201, response.text
    cid = response.json()[0]["id"]
    assert client.get("/api/project-folders/ssh").json()[0]["id"] == cid
    assert client.get("/api/projects/alpha/ssh").json()[0]["id"] == cid

    home = tmp_path / "remote"
    source = home / "team's project"
    (source / "子文件夹").mkdir(parents=True)
    (source / "子文件夹" / "readme.txt").touch()
    (source / "branch" / "line\nbreak").mkdir(parents=True)
    (source / ".hidden").mkdir()
    (source / "large").mkdir()
    for index in range(65):
        (source / "large" / f"file-{index}").touch()

    async def simulated_ssh(conn, command, *, timeout):
        assert conn["id"] == cid
        result = subprocess.run(["/bin/sh", "-c", command], env={**os.environ, "HOME": str(home)},
                                capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr

    monkeypatch.setattr(transport, "run", simulated_ssh)
    response = client.get(f"/api/project-folders/ssh/{cid}", params={"path": "~/team's project"})
    assert response.json() == {
        "path": str(source),
        "folders": ["branch", "large", "子文件夹"],
        "previews": {"branch": ["line\nbreak"], "子文件夹": []},
    }
    response = client.get(f"/api/project-folders/ssh/{cid}", params={"path": str(source / "large")})
    assert response.json() == {"path": str(source / "large"), "folders": []}

    folder = {"path": str(source), "ssh_id": cid}
    assert client.put("/api/project-folders/recent", json=folder).status_code == 200
    assert client.get("/api/project-folders/ssh").json()[0]["recent_path"] == str(source)
    response = client.post("/api/projects", json={"name": "SSH 项目", "sources": [folder]})
    assert response.status_code == 201, response.text
    assert client.get("/api/projects/SSH 项目/ssh").json()[0]["id"] == cid
    response = client.put(f"/api/ssh/folder-connections/{cid}", json={**body, "name": "更名连接"})
    assert response.json()[0]["name"] == "更名连接"
    assert client.get("/api/projects/alpha/ssh").json()[0]["name"] == "更名连接"


def test_web_files_revalidate(client):
    # 前端模块每次重新验证，更新后不会用到缓存里的旧 JS
    r = client.get("/js/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_desktop_server_prefers_stable_port():
    import socket

    from workbench.desktop.server import EmbeddedServer, preferred_port
    port = preferred_port("/tmp/wb-test-data")
    assert port == preferred_port("/tmp/wb-test-data") and 41000 <= port < 49000   # 同一数据目录总是同一个端口
    try:
        s = EmbeddedServer(None, port=port)
    except OSError:
        return
    assert s.port == port   # 端口不变，页面的 localStorage（项目、引擎等设置）重启后还在
    s.sock.close()
    busy = socket.socket()
    busy.bind(("127.0.0.1", port))
    busy.listen()
    try:
        s = EmbeddedServer(None, port=port)
        assert s.port != port   # 被占用：退回随机端口
        s.sock.close()
    finally:
        busy.close()
