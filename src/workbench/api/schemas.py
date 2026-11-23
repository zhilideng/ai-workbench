"""请求体模型。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class TaskIn(BaseModel):
    project: str
    prompt: str
    title: str = ""
    engine: str = "claude"
    permission_mode: str = "acceptEdits"
    model: str = ""
    effort: str = ""
    use_worktree: bool = False
    ssh_id: int = 0                   # 执行位置：0 = 本机；否则为本项目的 SSH 连接 id
    source_path: str = ""
    run_now: bool = False
    images: list[str] = []


class TaskPatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    permission_mode: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    kind: Optional[str] = None


class RunIn(BaseModel):
    message: Optional[str] = None
    # 本次（及之后）使用的模型 / 思考等级 / 权限，随发送一起提交，避免与单独的 PATCH 竞争
    model: Optional[str] = None
    effort: Optional[str] = None
    permission_mode: Optional[str] = None
    # 编辑某条已发送的消息：回退到该消息之前，用新内容重新发送（该条及之后的记录会被替换）
    edit_run_id: Optional[int] = None
    images: Optional[list[str]] = None  # 本条消息附带的图片（/api/uploads 返回的 path）


class SshIn(BaseModel):
    name: str
    host: str
    port: int = 0
    user: str = ""
    identity_file: str = ""
    proxy_jump: str = ""
    socks_proxy: str = ""
    remote_dir: str = ""
    note: str = ""
    share_with_ai: bool = True
    remote_claude: str = ""
    remote_codex: str = ""
    allow_root_bypass: bool = False
    auth: str = "key"                 # key | password
    password: Optional[str] = None    # 只在设置 / 修改时提交；留空表示不改


class SettingsIn(BaseModel):
    projects_root: Optional[str] = None


class ProjectSourceIn(BaseModel):
    path: str
    ssh_id: int = 0


class ProjectIn(BaseModel):
    name: str
    sources: list[ProjectSourceIn]


class ProjectSourcesIn(BaseModel):
    sources: list[ProjectSourceIn]
