"""引擎注册表。新增引擎：实现 engines.base.Engine，并加入 REGISTRY。"""
from __future__ import annotations

from workbench.core.errors import BadRequest
from workbench.engines.base import CommandSpec, Engine, RunState
from workbench.engines.claude import ClaudeEngine
from workbench.engines.codex import CodexEngine

REGISTRY: dict[str, Engine] = {e.name: e for e in (ClaudeEngine(), CodexEngine())}
DEFAULT_ENGINE = "claude"


def get_engine(name: str | None) -> Engine:
    eng = REGISTRY.get(name or DEFAULT_ENGINE)
    if not eng:
        raise BadRequest(f"engine 必须是 {' / '.join(REGISTRY)} 之一")
    return eng


__all__ = ["REGISTRY", "DEFAULT_ENGINE", "get_engine", "Engine", "CommandSpec", "RunState"]
