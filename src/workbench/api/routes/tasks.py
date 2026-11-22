from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from workbench.api.deps import get_runner
from workbench.api.schemas import RunIn, TaskIn, TaskPatch
from workbench.services import tasks
from workbench.services.runner import Runner

router = APIRouter(prefix="/tasks", tags=["tasks"])

# 预览 AI 引用的本地文件：禁止脚本执行
FILE_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox",
                "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}


@router.get("")
def list_tasks(project: Optional[str] = None):
    return tasks.list_tasks(project)


@router.post("")
async def create_task(body: TaskIn, runner: Runner = Depends(get_runner)):
    return await tasks.create_task(runner, **body.model_dump())


@router.get("/{task_id}")
def get_task(task_id: int):
    return tasks.get_task(task_id)


@router.patch("/{task_id}")
def patch_task(task_id: int, body: TaskPatch):
    return tasks.patch_task(task_id, body.model_dump())


@router.delete("/{task_id}")
def delete_task(task_id: int, runner: Runner = Depends(get_runner)):
    return tasks.delete_task(runner, task_id)


@router.post("/{task_id}/run")
async def run_task(task_id: int, body: RunIn, runner: Runner = Depends(get_runner)):
    return await tasks.send(runner, task_id, **body.model_dump())


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: int, runner: Runner = Depends(get_runner)):
    return tasks.cancel(runner, task_id)


@router.get("/{task_id}/file")
def task_file(task_id: int, path: str):
    return FileResponse(tasks.resolve_file(task_id, path), headers=FILE_HEADERS)
