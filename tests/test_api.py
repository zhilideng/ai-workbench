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
