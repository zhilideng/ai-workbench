"""从看板发送任务、读取任务组和重试拆分。"""
from fastapi import APIRouter, Depends

from workbench.api.deps import get_runner
from workbench.api.schemas import TaskIn
from workbench.services import dispatch
from workbench.services.runner import Runner

router = APIRouter(prefix="/dispatch", tags=["dispatch"])


class DispatchIn(TaskIn):
    request_id: str = ""


@router.get("")
def list_groups():
    return dispatch.list_groups()


@router.post("")
async def send(body: DispatchIn, runner: Runner = Depends(get_runner)):
    return await dispatch.send(runner, **body.model_dump())


@router.get("/{group_id}")
async def wait_for_group(group_id: int, runner: Runner = Depends(get_runner)):
    return await dispatch.wait_for_group(runner, group_id)


@router.post("/{group_id}/retry")
async def retry(group_id: int, runner: Runner = Depends(get_runner)):
    return dispatch.retry(runner, group_id)


@router.post("/{group_id}/single")
async def use_single(group_id: int, runner: Runner = Depends(get_runner)):
    return await dispatch.use_single(runner, group_id)
