from fastapi import APIRouter

from workbench.services import tasks

router = APIRouter(tags=["runs"])


@router.get("/runs/{run_id}/events")
def run_events(run_id: int, offset: int = 0, activity: bool = False):
    """增量读取运行日志：offset 为字节偏移，返回新事件与下一个 offset。"""
    return tasks.run_events(run_id, offset, activity)
