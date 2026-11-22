"""图片附件：存在数据目录的 uploads/ 下，随消息以绝对路径提交。"""
from __future__ import annotations

import uuid
from pathlib import Path

from workbench.core.errors import BadRequest, NotFound, PayloadTooLarge
from workbench.core.settings import get_settings

IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
MAX_UPLOAD = 20 * 1024 * 1024


def uploads_dir() -> Path:
    return get_settings().paths.uploads


def url_of(path: str) -> str:
    return f"/api/uploads/{Path(path).name}"


def validate(paths: list[str]) -> list[str]:
    """只接受 uploads/ 下已存在的文件。"""
    root = uploads_dir().resolve()
    out = []
    for p in paths:
        f = Path(p).resolve()
        if f.parent != root or not f.is_file():
            raise BadRequest(f"图片不存在：{p}")
        out.append(str(f))
    return out


def save(data: bytes, name: str, content_type: str) -> dict:
    ext = Path(name).suffix.lower().lstrip(".")
    if ext not in IMAGE_TYPES:
        ext = {v: k for k, v in IMAGE_TYPES.items()}.get(content_type, "")
    if ext not in IMAGE_TYPES:
        raise BadRequest("只支持 png / jpg / gif / webp 图片")
    if not data:
        raise BadRequest("文件为空")
    if len(data) > MAX_UPLOAD:
        raise PayloadTooLarge("图片不能超过 20MB")
    f = uploads_dir() / f"{uuid.uuid4().hex}.{ext}"
    f.write_bytes(data)
    return {"path": str(f), "url": url_of(str(f)), "name": name, "size": len(data)}


def resolve(name: str) -> tuple[Path, str]:
    root = uploads_dir().resolve()
    f = (root / name).resolve()
    ext = f.suffix.lstrip(".")
    if f.parent != root or not f.is_file() or ext not in IMAGE_TYPES:
        raise NotFound("图片不存在")
    return f, IMAGE_TYPES[ext]


def remove(paths: set[str]) -> None:
    root = uploads_dir().resolve()
    for i in paths:
        if Path(i).resolve().parent == root:
            Path(i).unlink(missing_ok=True)
