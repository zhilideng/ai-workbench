from fastapi import APIRouter

from workbench.services import quota

router = APIRouter(prefix="/quota", tags=["quota"])


@router.get("")
async def get_quota(refresh: bool = False):
    return await quota.overview(refresh)


@router.get("/claude-remote")
async def get_claude_remote(refresh: bool = False):
    return await quota.claude_remote(refresh)
