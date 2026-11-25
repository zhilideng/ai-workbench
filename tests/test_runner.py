"""运行器端到端：用一个假的 claude 脚本代替真实 CLI。"""
import time

from workbench.core.shell import find_bin

FAKE = """#!/bin/sh
echo '{"type":"system","subtype":"init","session_id":"sess-1","model":"fake","permissionMode":"acceptEdits"}'
echo '{"type":"stream_event","event":{"type":"message_start"}}'
echo '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"work"}}}'
echo '{"type":"stream_event","parent_tool_use_id":"t-1","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"sub"}}}'
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

    # 结构化执行（claude -p / codex exec）：任务默认在内嵌终端里执行，这条路径用于以前的记录和 API
    tid = client.post("/api/tasks", json={"project": "alpha", "prompt": "do it"}).json()["id"]
    client.post(f"/api/tasks/{tid}/run", json={})
    t = _wait(client, tid)
    assert t["status"] == "review" and t["session_id"] == "sess-1"
    assert "--resume sess-1" in t["resume_cmd"]
    run = t["runs"][0]
    ev = client.get(f"/api/runs/{run['id']}/events").json()
    kinds = [(e["type"], e.get("subtype")) for e in ev["events"]]
    assert ("wb", "start") in kinds and kinds[-1] == ("wb", "end") and ("wb", "stderr") in kinds
    # 假 claude 不懂 stream-json 握手：常驻会话启动失败，自动改用 claude -p
    assert any("改为启动 claude exec" in e.get("text", "") for e in ev["events"])
    # 逐字输出：只保留主会话的文字增量，写成精简事件；原始 stream_event 不进日志
    assert [e["text"] for e in ev["events"] if e.get("subtype") == "delta"] == ["work"]
    assert not any(e["type"] == "stream_event" for e in ev["events"])
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


FAKE_CODEX = """#!/bin/sh
printf '%s' "$@" > "$(dirname "$0")/codex-args.txt"
echo '{"type":"thread.started","thread_id":"th-1"}'
echo '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"codex done"}}'
echo '{"type":"turn.completed","usage":{}}'
"""


def test_switch_engine_hands_off_history(client, tmp_path, monkeypatch):
    for name, body in (("claude", FAKE), ("codex", FAKE_CODEX)):
        f = tmp_path / name
        f.write_text(body)
        f.chmod(0o755)
        monkeypatch.setenv(f"{name.upper()}_BIN", str(f))
    find_bin.cache_clear()

    tid = client.post("/api/tasks", json={"project": "alpha", "prompt": "first ask"}).json()["id"]
    client.post(f"/api/tasks/{tid}/run", json={})
    t = _wait(client, tid)
    assert t["engine"] == "claude" and t["session_id"] == "sess-1"

    # 会话中切换引擎：原会话属于 Claude，清空后由 Codex 开新会话，并带上此前的对话
    t = client.patch(f"/api/tasks/{tid}", json={"engine": "codex", "model": ""}).json()
    assert t["engine"] == "codex" and t["session_id"] == ""
    client.post(f"/api/tasks/{tid}/run", json={"message": "second ask", "engine": "codex"})
    t = _wait(client, tid)
    assert t["status"] == "review" and t["session_id"] == "th-1" and len(t["runs"]) == 2
    args = (tmp_path / "codex-args.txt").read_text()
    assert "resume" not in args and "first ask" in args and "all done" in args and "second ask" in args
    events = client.get(f"/api/runs/{t['runs'][1]['id']}/events").json()["events"]
    start = next(e for e in events if e.get("subtype") == "start")
    assert start["prompt"] == "second ask" and start["handoff"] is True   # 界面只显示用户这条消息

    # 编辑切换后的消息：回退点之前是 Claude 的记录，不能 fork Codex 会话，改为再次交接
    client.post(f"/api/tasks/{tid}/run", json={"message": "second edited", "edit_run_id": t["runs"][1]["id"]})
    t = _wait(client, tid)
    assert len(t["runs"]) == 2
    args = (tmp_path / "codex-args.txt").read_text()
    assert "resume" not in args and "first ask" in args and "second edited" in args
    find_bin.cache_clear()



FAKE_APP_SERVER = r"""#!{python}
import json, sys
log = open(sys.argv[0] + ".log", "a")
def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def note(method, **p):
    send({{"method": method, "params": p}})
if sys.argv[1:2] == ["exec"]:
    send({{"type": "thread.started", "thread_id": "fallback"}})
    send({{"type": "turn.completed"}})
    sys.exit(0)
turns = threads = 0
waiting = None   # 等待审批结果的轮次 (threadId, turnId)
for raw in sys.stdin:
    m = json.loads(raw)
    log.write(raw); log.flush()
    if "method" not in m:          # 工作台对审批请求的回复
        tid, turn = waiting
        note("item/completed", threadId=tid, turnId=turn,
             item={{"id": "m2", "type": "agentMessage", "text": "decision=" + m["result"]["decision"]}})
        note("turn/completed", threadId=tid, turn={{"id": turn, "status": "completed"}})
        continue
    if "id" not in m:
        continue
    meth, p = m["method"], m.get("params") or {{}}
    if meth == "thread/start":
        if p.get("model") == "test-fd-error":
            send({{"id": m["id"], "error": {{"message": "Too many open files (os error 24)"}}}})
            continue
        threads += 1
        send({{"id": m["id"], "result": {{"thread": {{"id": "th-" + "ABCDEFGH"[threads - 1]}}}}}})
    elif meth == "turn/start":
        turns += 1
        tid, turn = p["threadId"], "turn-%d" % turns
        send({{"id": m["id"], "result": {{"turn": {{"id": turn}}}}}})
        note("turn/started", threadId=tid, turnId=turn)
        text = p["input"][0]["text"]
        if "slow" in text:
            continue   # 等 turn/interrupt
        if "approve" in text:
            waiting = (tid, turn)
            send({{"id": 900, "method": "item/commandExecution/requestApproval",
                   "params": {{"threadId": tid, "turnId": turn, "itemId": "c9", "startedAtMs": 1,
                              "command": "rm -rf build", "cwd": "/proj", "reason": "清理"}}}})
            continue
        note("item/reasoning/summaryTextDelta", threadId=tid, turnId=turn, itemId="r1", delta="先看")
        note("item/completed", threadId=tid, turnId=turn,
             item={{"id": "r1", "type": "reasoning", "summary": ["先看目录"], "content": ["完整思考"]}})
        note("turn/plan/updated", threadId=tid, turnId=turn, explanation="步骤", plan=[{{"step": "看目录", "status": "completed"}}])
        note("warning", threadId=tid, message="注意")
        note("item/completed", threadId=tid, turnId=turn, item={{"id": "f1", "type": "fileChange", "status": "completed",
             "changes": [{{"path": "/p/a.py", "kind": {{"type": "update", "move_path": None}}, "diff": "@@ -1 +1 @@\n-a\n+b"}}]}})
        note("item/completed", threadId=tid, turnId=turn, item={{"id": "x1", "type": "contextCompaction"}})
        note("item/completed", threadId=tid, turnId=turn, item={{"id": "u1", "type": "userMessage", "content": []}})
        for d in ("你", "好"):
            note("item/agentMessage/delta", threadId=tid, turnId=turn, itemId="m1", delta=d)
        note("item/completed", threadId=tid, turnId=turn,
             item={{"id": "c1", "type": "commandExecution", "command": "ls", "aggregatedOutput": "a.py", "exitCode": 0}})
        note("item/completed", threadId=tid, turnId=turn, item={{"id": "m1", "type": "agentMessage", "text": "你好"}})
        note("thread/tokenUsage/updated", threadId=tid, turnId=turn, tokenUsage={{"last": {{"inputTokens": 5, "outputTokens": 2}}}})
        note("turn/completed", threadId=tid, turn={{"id": turn, "status": "completed"}})
    elif meth == "turn/interrupt":
        send({{"id": m["id"], "result": {{}}}})
        note("turn/completed", threadId=p["threadId"], turn={{"id": p["turnId"], "status": "interrupted"}})
    else:
        send({{"id": m["id"], "result": {{}}}})
"""


def _events(client, t, i=-1):
    return client.get(f"/api/runs/{t['runs'][i]['id']}/events").json()["events"]


def test_codex_app_server(client, tmp_path, monkeypatch):
    import json
    import sys
    fake = tmp_path / "codex"
    fake.write_text(FAKE_APP_SERVER.format(python=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(fake))
    find_bin.cache_clear()

    tid = client.post("/api/tasks", json={"project": "alpha", "prompt": "hi", "engine": "codex",
                                          "run_now": True}).json()["id"]
    t = _wait(client, tid)
    assert t["status"] == "review" and t["session_id"] == "th-A"
    ev = _events(client, t)
    assert [e["text"] for e in ev if e.get("subtype") == "delta"] == ["你", "好"]   # 逐字输出
    done = [e["item"] for e in ev if e.get("type") == "item.completed"]
    # 完整展示：思考（增量 + 摘要 + 正文）、计划、警告、diff、其他条目都在；用户自己的消息不重复记录
    assert [e["text"] for e in ev if e.get("subtype") == "thinking_delta"] == ["先看"]
    by_type = {d["type"]: d for d in done}
    assert by_type["reasoning"]["summary"] == ["先看目录"] and by_type["reasoning"]["content"] == ["完整思考"]
    assert by_type["file_change"]["changes"][0]["diff"] == "@@ -1 +1 @@\n-a\n+b"
    assert by_type["other"]["item_type"] == "contextCompaction" and "userMessage" not in str(done)
    assert any(e.get("subtype") == "plan" and e["plan"][0]["step"] == "看目录" for e in ev)
    assert any(e.get("subtype") == "warning" and e["text"] == "注意" for e in ev)
    assert by_type["command_execution"]["aggregated_output"] == "a.py"
    assert {"id": "m1", "type": "agent_message", "text": "你好"}.items() <= by_type["agent_message"].items()
    assert next(e for e in ev if e.get("type") == "turn.completed")["usage"]["input_tokens"] == 5

    # 审批：请求转给界面，批准后 Codex 收到 accept
    client.post(f"/api/tasks/{tid}/run", json={"message": "approve this"})
    end = time.time() + 5
    while True:
        t = client.get(f"/api/tasks/{tid}").json()
        appr = [e for e in _events(client, t) if e.get("subtype") == "approval"]
        if appr:
            break
        assert time.time() < end
        time.sleep(0.05)
    assert appr[0]["kind"] == "command" and appr[0]["command"] == "rm -rf build" and appr[0]["reason"] == "清理"
    assert client.post(f"/api/tasks/{tid}/approval", json={"id": appr[0]["id"], "decision": "accept"}).json() == {"ok": True}
    assert client.post(f"/api/tasks/{tid}/approval", json={"id": appr[0]["id"], "decision": "accept"}).status_code == 409
    t = _wait(client, tid)
    ev = _events(client, t)
    assert any(e.get("subtype") == "approval_done" and e["decision"] == "accept" for e in ev)
    assert t["status"] == "review"

    # 停止：中断当前轮次，进程保留
    client.post(f"/api/tasks/{tid}/run", json={"message": "slow"})
    _wait(client, tid, done=("running",))
    time.sleep(0.2)
    client.post(f"/api/tasks/{tid}/cancel")
    assert _wait(client, tid)["status"] == "cancelled"

    reqs = [json.loads(line) for line in (tmp_path / "codex.log").read_text().splitlines()]
    methods = [r.get("method") for r in reqs]
    assert methods.count("initialize") == 1 and methods.count("thread/start") == 1 and "thread/resume" not in methods
    assert methods.count("turn/start") == 3 and "turn/interrupt" in methods
    start = next(r for r in reqs if r.get("method") == "thread/start")["params"]
    assert start["approvalPolicy"] == "on-request" and start["sandbox"] == "workspace-write" and start["cwd"]
    turn = next(r for r in reqs if r.get("method") == "turn/start")["params"]
    assert turn["sandboxPolicy"] == {"type": "workspaceWrite"} and turn["approvalPolicy"] == "on-request"
    assert {"id": 900, "result": {"decision": "accept"}}.items() <= next(r for r in reqs if r.get("id") == 900).items()
    find_bin.cache_clear()



FAKE_CLAUDE_STREAM = r"""#!{python}
import json, sys
log = open(sys.argv[0] + ".log", "a")
log.write(json.dumps(sys.argv[1:]) + "\n"); log.flush()
sid = sys.argv[sys.argv.index("--session-id") + 1] if "--session-id" in sys.argv else sys.argv[sys.argv.index("--resume") + 1]
def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
cost, waiting = 0.0, None
for raw in sys.stdin:
    m = json.loads(raw)
    log.write(raw); log.flush()
    if m["type"] == "control_request" and m["request"]["subtype"] == "initialize":
        send({{"type": "control_response", "response": {{"subtype": "success", "request_id": m["request_id"], "response": {{}}}}}})
    elif m["type"] == "control_request" and m["request"]["subtype"] == "interrupt":
        send({{"type": "control_response", "response": {{"subtype": "success", "request_id": m["request_id"], "response": {{}}}}}})
        send({{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "", "total_cost_usd": cost}})
    elif m["type"] == "control_response":     # 工作台对审批的回复
        r = m["response"]["response"]
        cost += 0.01
        send({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "behavior=" + r["behavior"]}}]}}}})
        send({{"type": "result", "subtype": "success", "is_error": False, "result": "ok", "total_cost_usd": cost}})
    elif m["type"] == "user":
        text = m["message"]["content"]
        send({{"type": "system", "subtype": "init", "session_id": sid, "model": "fake", "permissionMode": "acceptEdits"}})
        if "slow" in text:
            continue
        if "approve" in text:
            send({{"type": "control_request", "request_id": "perm-1", "request": {{"subtype": "can_use_tool",
                  "tool_name": "Bash", "input": {{"command": "rm -rf build", "description": "清理"}}}}}})
            continue
        send({{"type": "stream_event", "event": {{"type": "content_block_delta", "delta": {{"type": "thinking_delta", "thinking": "想"}}}}}})
        send({{"type": "assistant", "message": {{"content": [{{"type": "thinking", "thinking": "想一想", "signature": "s"}}]}}}})
        send({{"type": "stream_event", "event": {{"type": "content_block_delta", "delta": {{"type": "text_delta", "text": "你"}}}}}})
        send({{"type": "assistant", "uuid": "u1", "message": {{"content": [{{"type": "text", "text": "你好"}}]}}}})
        cost += 0.01
        send({{"type": "result", "subtype": "success", "is_error": False, "result": "你好", "total_cost_usd": cost}})
"""


def test_claude_persistent_session(client, tmp_path, monkeypatch):
    import json
    import sys
    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE_STREAM.format(python=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(fake))
    find_bin.cache_clear()

    tid = client.post("/api/tasks", json={"project": "alpha", "prompt": "hi", "run_now": True}).json()["id"]
    t = _wait(client, tid)
    sid = t["session_id"]
    assert t["status"] == "review" and sid and abs(t["total_cost"] - 0.01) < 1e-9
    ev = _events(client, t)
    assert [e["text"] for e in ev if e.get("subtype") == "delta"] == ["你"] and ev[-1]["status"] == "success"
    # 思考：增量写成精简事件，完整的思考块原样记录
    assert [e["text"] for e in ev if e.get("subtype") == "thinking_delta"] == ["想"]
    assert any(c.get("type") == "thinking" and c["thinking"] == "想一想"
               for e in ev if e.get("type") == "assistant" for c in e["message"]["content"])

    # 续接：同一个进程，费用按本轮增量计
    client.post(f"/api/tasks/{tid}/run", json={"message": "again"})
    t = _wait(client, tid)
    assert t["session_id"] == sid and abs(t["total_cost"] - 0.02) < 1e-9

    # 审批：can_use_tool 转给界面，批准后 CLI 收到 allow
    client.post(f"/api/tasks/{tid}/run", json={"message": "approve this"})
    end = time.time() + 5
    while not (appr := [e for e in _events(client, client.get(f"/api/tasks/{tid}").json())
                        if e.get("subtype") == "approval"]):
        assert time.time() < end
        time.sleep(0.05)
    assert appr[0]["kind"] == "command" and appr[0]["command"] == "rm -rf build" and appr[0]["reason"] == "清理"
    client.post(f"/api/tasks/{tid}/approval", json={"id": appr[0]["id"], "decision": "accept"})
    t = _wait(client, tid)
    assert t["status"] == "review" and any(e.get("subtype") == "approval_done" for e in _events(client, t))

    # 停止：发 interrupt，进程保留
    client.post(f"/api/tasks/{tid}/run", json={"message": "slow"})
    _wait(client, tid, done=("running",))
    time.sleep(0.2)
    client.post(f"/api/tasks/{tid}/cancel")
    assert _wait(client, tid)["status"] == "cancelled"

    lines = (tmp_path / "claude.log").read_text().splitlines()
    starts = [json.loads(x) for x in lines if x.startswith("[")]
    assert len(starts) == 1   # 四条消息只启动了一个进程
    assert "--permission-prompt-tool" in starts[0] and starts[0][starts[0].index("--session-id") + 1] == sid
    msgs = [json.loads(x) for x in lines if x.startswith("{")]
    assert [m["message"]["content"] for m in msgs if m["type"] == "user"] == ["hi", "again", "approve this", "slow"]
    allow = next(m for m in msgs if m["type"] == "control_response")["response"]["response"]
    assert allow == {"behavior": "allow", "updatedInput": {"command": "rm -rf build", "description": "清理"}}
    assert any(m["type"] == "control_request" and m["request"]["subtype"] == "interrupt" for m in msgs)
    find_bin.cache_clear()


def _wait_log(path, pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and pred(path.read_text()):
            return
        time.sleep(0.05)
    raise AssertionError(path.read_text() if path.exists() else "日志不存在")


def test_codex_prewarm(client, tmp_path, monkeypatch):
    import json
    import sys

    from workbench.db.repositories import TaskRepo
    fake = tmp_path / "codex"
    fake.write_text(FAKE_APP_SERVER.format(python=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(fake))
    find_bin.cache_clear()
    log = tmp_path / "codex.log"

    def reqs():
        return [json.loads(x) for x in log.read_text().splitlines()]

    # 输入框就绪：按发送时的设置预建会话；同样的设置重复预热不会重复建
    body = {"project": "alpha", "engine": "codex", "effort": "high"}
    assert client.post("/api/tasks/prewarm", json=body).json() == {"ok": True}
    assert client.post("/api/tasks/prewarm", json=body).json() == {"ok": True}
    _wait_log(log, lambda s: s.count('"thread/start"') == 1)
    start = next(r for r in reqs() if r.get("method") == "thread/start")["params"]
    assert start["config"] == {"model_reasoning_effort": "high"} and start["sandbox"] == "workspace-write"

    tid = client.post("/api/tasks", json={"project": "alpha", "prompt": "hi", "engine": "codex", "effort": "high",
                                          "run_now": True}).json()["id"]
    assert _wait(client, tid)["session_id"] == "th-A"   # 用的是预建的会话
    first = next(r for r in reqs() if r.get("method") == "turn/start")["params"]
    assert set(first) == {"threadId", "input"}           # 设置与预建时一致，第一轮不再重复传
    _wait_log(log, lambda s: s.count('"thread/start"') == 2)   # 用掉后补一个

    # 设置不同（思考等级）：不用预建的会话
    tid2 = client.post("/api/tasks", json={"project": "alpha", "prompt": "x", "engine": "codex", "effort": "low",
                                           "run_now": True}).json()["id"]
    assert _wait(client, tid2)["session_id"] == "th-C"
    assert "effort" in [r for r in reqs() if r.get("method") == "turn/start"][-1]["params"]

    # 打开会话未加载的任务：提前 thread/resume，发送时不再恢复
    assert client.post("/api/tasks/prewarm", json={"project": "alpha", "engine": "codex", "use_worktree": True}).json() == {"ok": False}
    TaskRepo.update(tid, session_id="th-OLD")
    assert client.post(f"/api/tasks/{tid}/prewarm").json() == {"ok": True}
    _wait_log(log, lambda s: '"thread/resume"' in s)
    client.post(f"/api/tasks/{tid}/run", json={"message": "again"})
    _wait(client, tid)
    assert [r.get("method") for r in reqs()].count("thread/resume") == 1
    assert [r for r in reqs() if r.get("method") == "turn/start"][-1]["params"]["threadId"] == "th-OLD"
    find_bin.cache_clear()


def test_codex_recovers_from_file_descriptor_exhaustion(client, tmp_path, monkeypatch):
    import sys

    from workbench.engines import codex_server

    fake = tmp_path / "codex"
    fake.write_text(FAKE_APP_SERVER.format(python=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(fake))
    monkeypatch.setattr(codex_server, "RECYCLE_SECONDS", 0.05, raising=False)
    find_bin.cache_clear()

    try:
        body = {"project": "alpha", "prompt": "hi", "engine": "codex", "run_now": True}
        active_id = client.post("/api/tasks", json={**body, "prompt": "slow"}).json()["id"]
        log = tmp_path / "codex.log"
        _wait_log(log, lambda s: '"turn/start"' in s)
        old_server = codex_server.get_server()
        old_proc = old_server.proc

        failed_id = client.post("/api/tasks", json={**body, "model": "test-fd-error"}).json()["id"]
        recovered = _wait(client, failed_id)
        assert recovered["status"] == "review" and recovered["session_id"] == "fallback"
        assert old_server.proc is old_proc and old_server.alive
        assert client.get(f"/api/tasks/{active_id}").json()["status"] == "running"

        client.post(f"/api/tasks/{active_id}/cancel")
        assert _wait(client, active_id)["status"] == "cancelled"
        deadline = time.monotonic() + 3
        while old_server.alive and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not old_server.alive
        next_id = client.post("/api/tasks", json=body).json()["id"]
        assert _wait(client, next_id)["status"] == "review"
        assert log.read_text().count('"initialize"') == 2
    finally:
        find_bin.cache_clear()


def test_claude_prewarm(client, tmp_path, monkeypatch):
    import json
    import sys
    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE_STREAM.format(python=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(fake))
    find_bin.cache_clear()
    log = tmp_path / "claude.log"

    def spawns():
        return [json.loads(x) for x in log.read_text().splitlines() if x.startswith("[")]

    # 预启动进程并完成握手；发送时直接用，不再启动新进程
    assert client.post("/api/tasks/prewarm", json={"project": "alpha", "model": "opus"}).json() == {"ok": True}
    _wait_log(log, lambda s: '"initialize"' in s)
    warm_sid = spawns()[0][spawns()[0].index("--session-id") + 1]
    tid = client.post("/api/tasks", json={"project": "alpha", "prompt": "hi", "model": "opus",
                                          "run_now": True}).json()["id"]
    t = _wait(client, tid)
    assert t["status"] == "review" and t["session_id"] == warm_sid
    _wait_log(log, lambda s: sum(1 for x in s.splitlines() if x.startswith("[")) == 2)   # 用掉后补一个
    assert all("--model" in a and a[a.index("--model") + 1] == "opus" for a in spawns())
    find_bin.cache_clear()
