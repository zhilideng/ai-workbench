"""执行引擎抽象。一个引擎 = 一个能以非交互方式运行、输出 JSON 事件流的 AI CLI。

新增引擎：继承 Engine，实现抽象方法，然后在 engines/__init__.py 的 REGISTRY 里注册。
运行器（services/runner.py）只通过这里的接口和引擎打交道，不关心具体 CLI 的参数。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from workbench.core.shell import bin_available, find_bin


@dataclass
class RunState:
    """从事件流中提取出的运行结果。"""
    sid: Optional[str] = None
    result: str = ""
    cost: float = 0.0
    status: Optional[str] = None   # success | error | None（未结束）


@dataclass
class CommandSpec:
    """一次执行的参数（与本机 / 远程无关的部分）。"""
    task: dict
    prompt: str
    resume_sid: Optional[str] = None
    images: list[str] = field(default_factory=list)
    uploads_dir: Optional[str] = None      # 图片所在目录（claude 需要 --add-dir 放行）
    ssh_context: str = ""                  # 给 AI 的 SSH 连接说明，空 = 无
    binary: Optional[str] = None           # 远程执行时用服务器上的路径；None = 本机查找到的路径


EventReader = Callable[[int], Iterable[dict]]   # run_id -> 该次运行的事件


class Engine(ABC):
    name: str = ""
    label: str = ""

    # ---- 可执行文件
    @property
    def bin(self) -> str:
        return find_bin(self.name)

    def available(self) -> bool:
        return bin_available(self.bin)

    def remote_bin(self, conn: dict) -> str:
        """服务器上的可执行文件：连接里配置了路径就用它，否则依赖登录 shell 的 PATH。"""
        return conn.get(f"remote_{self.name}") or self.name

    # ---- 给界面的配置（模型列表、思考等级等），合并进 /api/config
    def client_config(self) -> dict:
        return {}

    # ---- 命令
    @abstractmethod
    def build_cmd(self, spec: CommandSpec) -> list[str]:
        """返回完整的 argv。"""

    @abstractmethod
    def mask_prompt(self, cmd: list[str]) -> list[str]:
        """日志里展示的命令：把提示词替换成 <prompt>。"""

    @abstractmethod
    def resume_command(self, sid: str, binary: Optional[str] = None) -> str:
        """终端接管时执行的命令（不含 cd）。"""

    def remote_env(self, task: dict, conn: dict) -> dict:
        """远程执行时额外的环境变量。"""
        return {}

    # ---- 事件流
    @abstractmethod
    def parse_event(self, ev: dict, st: RunState) -> None:
        """从一条事件中提取 session_id / 结果 / 花费 / 状态，写入 st。"""

    # ---- 编辑历史消息
    @abstractmethod
    async def fork(self, task: dict, earlier_runs: list[int], read_events: EventReader) -> tuple[str, str]:
        """把会话回退到 earlier_runs 之后（不含被编辑的那条），返回 (新 session_id, fork_at)。
        返回 ("", "") 表示找不到可回退的点，从头开新会话。"""
