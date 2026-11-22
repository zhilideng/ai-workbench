"""FastAPI 应用工厂。"""
from __future__ import annotations

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


def create_app(on_runner: Optional[callable] = None) -> FastAPI:
    """on_runner：运行器创建后回调（桌面壳用它查询是否有任务在执行）。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db()
        ensure_askpass()
        app.state.runner = Runner()
        if on_runner:
            on_runner(app.state.runner)
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

    app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
    return app
