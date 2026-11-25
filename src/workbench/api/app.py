"""FastAPI 应用工厂。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from workbench import __version__
from workbench.api.routes import ROUTERS
from workbench.core.errors import WorkbenchError
from workbench.db import init_db
from workbench.services.runner import Runner
from workbench.ssh.keychain import ensure_askpass

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class WebFiles(StaticFiles):
    """前端文件每次都按 ETag 重新验证：更新应用后不会出现新页面配旧 JS 模块的情况（未变时只返回 304）。"""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def create_app(on_runner: Optional[callable] = None, warm: bool = False) -> FastAPI:
    """on_runner：运行器创建后回调（桌面壳用它查询是否有任务在执行）。
    warm：启动后在后台拉起引擎的常驻进程（桌面应用开启；测试和开发服务器不开）。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db()
        ensure_askpass()
        app.state.runner = Runner()
        if on_runner:
            on_runner(app.state.runner)
        if warm:
            asyncio.create_task(app.state.runner.warm())
        await app.state.runner.start_ready()
        yield
        await app.state.runner.shutdown()

    app = FastAPI(title="AI Workbench", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.exception_handler(WorkbenchError)
    async def _workbench_error(_: Request, e: WorkbenchError):
        return JSONResponse({"detail": e.message}, status_code=e.status)

    for r in ROUTERS:
        app.include_router(r, prefix="/api")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/", WebFiles(directory=WEB_DIR), name="web")
    return app
