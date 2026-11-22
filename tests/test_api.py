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
