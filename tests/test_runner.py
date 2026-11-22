"""运行器端到端：用一个假的 claude 脚本代替真实 CLI。"""
import time

from workbench.core.shell import find_bin

FAKE = """#!/bin/sh
echo '{"type":"system","subtype":"init","session_id":"sess-1","model":"fake","permissionMode":"acceptEdits"}'
echo '{"type":"assistant","uuid":"u-1","message":{"content":[{"type":"text","text":"working"}]}}'
echo 'warning on stderr' >&2
echo '{"type":"result","result":"all done","total_cost_usd":0.01,"is_error":false}'
"""


def _wait(client, tid, done=("review", "failed", "cancelled"), timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        t = client.get(f"/api/tasks/{tid}").json()
        if t["status"] in done:
            return t
        time.sleep(0.05)
    raise AssertionError(f"任务没有结束：{t['status']}")


def test_run_and_resume(client, tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(fake))
    find_bin.cache_clear()

    tid = client.post("/api/tasks", json={"project": "alpha", "prompt": "do it", "run_now": True}).json()["id"]
    t = _wait(client, tid)
    assert t["status"] == "review" and t["session_id"] == "sess-1"
    assert "--resume sess-1" in t["resume_cmd"]
    run = t["runs"][0]
    ev = client.get(f"/api/runs/{run['id']}/events").json()
    kinds = [(e["type"], e.get("subtype")) for e in ev["events"]]
    assert kinds[0] == ("wb", "start") and kinds[-1] == ("wb", "end") and ("wb", "stderr") in kinds
    assert ev["status"] == "success"

    # 继续对话
    client.post(f"/api/tasks/{tid}/run", json={"message": "again"})
    t = _wait(client, tid)
    assert len(t["runs"]) == 2 and t["status"] == "review"

    # 编辑第二条消息：回退到第一条之后（claude fork）
    client.post(f"/api/tasks/{tid}/run", json={"message": "edited", "edit_run_id": t["runs"][1]["id"]})
    t = _wait(client, tid)
    assert len(t["runs"]) == 2
    find_bin.cache_clear()
