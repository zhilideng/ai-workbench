"""Claude Code CLI：`claude -p <prompt> --output-format stream-json`。"""
from __future__ import annotations

import shlex
from typing import Optional

from workbench.engines.base import CommandSpec, Engine, EventReader, RunState

CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
ROOT_BYPASS_MSG = "Claude CLI 不允许以 root 身份使用「全部放行」"
ROOT_BYPASS_HINT = (ROOT_BYPASS_MSG + "。可以：① 改用非 root 用户登录；② 权限模式改为「自动改文件」；"
                    "③ 在该 SSH 连接里勾选「允许以 root 全部放行」（会以 IS_SANDBOX=1 启动 claude，AI 将拥有服务器的完全 root 权限）。")


def with_images(prompt: str, images: list[str]) -> str:
    if not images:
        return prompt
    return (prompt or "请查看附带的图片。") + "\n\n附带图片（用 Read 工具查看）：\n" + "\n".join(f"- {i}" for i in images)


class ClaudeEngine(Engine):
    name = "claude"
    label = "Claude"

    def client_config(self) -> dict:
        return {"claude_efforts": CLAUDE_EFFORTS}

    def build_cmd(self, spec: CommandSpec) -> list[str]:
        t = spec.task
        cmd = [spec.binary or self.bin, "-p", with_images(spec.prompt, spec.images), "--output-format", "stream-json", "--verbose"]
        if spec.images and spec.uploads_dir:  # 图片不在项目目录内，需显式放行读取
            cmd += ["--add-dir", spec.uploads_dir]
        cmd += ["--permission-mode", t.get("permission_mode") or "acceptEdits"]
        if spec.ssh_context:
            cmd += ["--append-system-prompt", spec.ssh_context]
        if t.get("model"):
            cmd += ["--model", t["model"]]
        if t.get("effort"):
            cmd += ["--effort", t["effort"]]
        if spec.resume_sid:
            cmd += ["--resume", spec.resume_sid]
            if t.get("fork_at"):  # 编辑过历史消息：从该点分叉出新会话，原会话不动
                cmd += ["--resume-session-at", t["fork_at"], "--fork-session"]
        return cmd

    def mask_prompt(self, cmd: list[str]) -> list[str]:
        shown = list(cmd)
        shown[2] = "<prompt>"
        return shown

    def resume_command(self, sid: str, binary: Optional[str] = None) -> str:
        return f"{shlex.quote(binary or 'claude')} --resume {shlex.quote(sid)}"

    def remote_env(self, task: dict, conn: dict) -> dict:
        # 连接允许时，让 claude 在 root 下也能「全部放行」
        if task.get("permission_mode") == "bypassPermissions" and conn.get("allow_root_bypass"):
            return {"IS_SANDBOX": "1"}
        return {}

    def parse_event(self, ev: dict, st: RunState) -> None:
        t = ev.get("type")
        if t == "system" and ev.get("session_id"):
            st.sid = ev["session_id"]
        elif t == "result":
            st.result = ev.get("result") or ""
            st.cost = float(ev.get("total_cost_usd") or 0)
            st.status = "error" if ev.get("is_error") else "success"

    async def fork(self, task: dict, earlier_runs: list[int], read_events: EventReader) -> tuple[str, str]:
        # --resume <sid> --resume-session-at <上一条消息 uuid> --fork-session（原会话不动）
        for rid in reversed(earlier_runs):
            uuids = [e["uuid"] for e in read_events(rid) if e.get("type") in ("assistant", "user") and e.get("uuid")]
            if uuids:
                return task["session_id"], uuids[-1]
        return "", ""
