"""路由依赖。"""
from __future__ import annotations

from fastapi import Request

from workbench.services.runner import Runner


def get_runner(request: Request) -> Runner:
    return request.app.state.runner
