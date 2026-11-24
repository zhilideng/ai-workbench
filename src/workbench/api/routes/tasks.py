from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from workbench.api.deps import get_runner
from workbench.api.schemas import ApprovalIn, PrewarmIn, RunIn, TaskIn, TaskPatch
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


@router.post("/prewarm")
async def prewarm(body: PrewarmIn):
    """新建任务的输入框就绪时预热（只在后台准备会话，立即返回）。"""
    return tasks.prewarm(**body.model_dump())


@router.get("/{task_id}")
def get_task(task_id: int):
    return tasks.get_task(task_id)


@router.patch("/{task_id}")
def patch_task(task_id: int, body: TaskPatch):
    return tasks.patch_task(task_id, body.model_dump())


@router.delete("/{task_id}")
def delete_task(task_id: int, runner: Runner = Depends(get_runner)):
    return tasks.delete_task(runner, task_id)


@router.post("/{task_id}/prewarm")
async def prewarm_task(task_id: int):
    """打开已有任务时预热：提前恢复它的会话。"""
    return tasks.prewarm_task(task_id)


@router.post("/{task_id}/approval")
def approval(task_id: int, body: ApprovalIn, runner: Runner = Depends(get_runner)):
    """回复 Codex 的审批请求：accept 批准、acceptForSession 本会话都批准、decline 拒绝、cancel 拒绝并停止。"""
    runner.decide(task_id, body.id, body.decision)
    return {"ok": True}


@router.post("/{task_id}/run")
async def run_task(task_id: int, body: RunIn, runner: Runner = Depends(get_runner)):
    return await tasks.send(runner, task_id, **body.model_dump())


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: int, runner: Runner = Depends(get_runner)):
    return tasks.cancel(runner, task_id)


@router.get("/{task_id}/file")
def task_file(task_id: int, path: str):
    return FileResponse(tasks.resolve_file(task_id, path), headers=FILE_HEADERS)
