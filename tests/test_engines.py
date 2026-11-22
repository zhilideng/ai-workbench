"""引擎命令构造与事件解析（纯函数，不启动 CLI）。"""
from workbench.engines import CommandSpec, RunState, get_engine

TASK = {"permission_mode": "acceptEdits", "model": "", "effort": "", "fork_at": ""}


def test_claude_cmd():
    e = get_engine("claude")
    cmd = e.build_cmd(CommandSpec({**TASK, "model": "opus", "effort": "high"}, "hi", binary="claude"))
    assert cmd[:3] == ["claude", "-p", "hi"]
    assert cmd[cmd.index("--model") + 1] == "opus" and cmd[cmd.index("--effort") + 1] == "high"
    assert e.mask_prompt(cmd)[2] == "<prompt>"
    resumed = e.build_cmd(CommandSpec({**TASK, "fork_at": "u1"}, "more", resume_sid="s1", binary="claude"))
    assert resumed[-5:] == ["--resume", "s1", "--resume-session-at", "u1", "--fork-session"]


def test_claude_images_and_ssh_context():
    cmd = get_engine("claude").build_cmd(CommandSpec(TASK, "", images=["/u/a.png"], uploads_dir="/u",
                                                     ssh_context="ctx", binary="claude"))
    assert "/u/a.png" in cmd[2] and cmd[cmd.index("--add-dir") + 1] == "/u"
    assert cmd[cmd.index("--append-system-prompt") + 1] == "ctx"


def test_codex_cmd():
    e = get_engine("codex")
    cmd = e.build_cmd(CommandSpec({**TASK, "permission_mode": "plan"}, "hi", images=["/a.png"],
                                  ssh_context="ctx", binary="codex"))
    assert cmd[:2] == ["codex", "exec"] and cmd[-2:][0] == "--"
    assert cmd[-1].startswith("ctx") and 'sandbox_mode="read-only"' in cmd
    assert cmd[cmd.index("-i") + 1] == "/a.png"
    assert e.mask_prompt(cmd)[-1] == "<prompt>"
    resumed = e.build_cmd(CommandSpec({**TASK, "permission_mode": "bypassPermissions"}, "go", resume_sid="t1",
                                      ssh_context="ctx", binary="codex"))
    assert resumed[2:4] == ["resume", "t1"] and resumed[-1] == "go"
    assert "--dangerously-bypass-approvals-and-sandbox" in resumed


def test_parse_events():
    st = RunState()
    c = get_engine("claude")
    c.parse_event({"type": "system", "session_id": "s"}, st)
    c.parse_event({"type": "result", "result": "ok", "total_cost_usd": 0.5}, st)
    assert (st.sid, st.result, st.cost, st.status) == ("s", "ok", 0.5, "success")
    st = RunState()
    x = get_engine("codex")
    x.parse_event({"type": "thread.started", "thread_id": "t"}, st)
    x.parse_event({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}, st)
    x.parse_event({"type": "turn.completed"}, st)
    assert (st.sid, st.result, st.status) == ("t", "done", "success")


def test_remote_env():
    c = get_engine("claude")
    assert c.remote_env({"permission_mode": "bypassPermissions"}, {"allow_root_bypass": 1}) == {"IS_SANDBOX": "1"}
    assert c.remote_env({"permission_mode": "acceptEdits"}, {"allow_root_bypass": 1}) == {}
    assert get_engine("codex").remote_env({"permission_mode": "bypassPermissions"}, {"allow_root_bypass": 1}) == {}
