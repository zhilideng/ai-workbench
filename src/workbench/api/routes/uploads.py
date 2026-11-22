from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from workbench.api.routes.tasks import FILE_HEADERS
from workbench.services import uploads

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post("")
async def upload(request: Request, name: str = "image.png"):
    """上传图片：请求体为文件原始字节，返回本机路径（随消息一起提交）。"""
    return uploads.save(await request.body(), name, request.headers.get("content-type", ""))


@router.get("/{name}")
def get_upload(name: str):
    f, media = uploads.resolve(name)
    return FileResponse(f, media_type=media, headers=FILE_HEADERS)
