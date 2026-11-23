"""工作台项目与关联的本机 / SSH 源文件夹。"""
from __future__ import annotations

import fcntl
import os
import posixpath
import shlex
import sqlite3
from pathlib import Path

from workbench.core.errors import BadRequest, Conflict, NotFound, UpstreamError
from workbench.core.settings import get_settings
from workbench.core.shell import git
from workbench.db.repositories import ProjectSourcesRepo, SshRepo, TaskRepo
from workbench.ssh import transport


def _legacy_path(name: str) -> Path | None:
    settings = get_settings()
    root = settings.projects_root
    if root is None or not name or name.startswith(".") or Path(name).name != name:
        return None
    path = (root / name).resolve()
    if path.parent == root and path != settings.paths.data and path.is_dir():
        return path
    return None


def project_sources(name: str) -> list[dict]:
    saved = ProjectSourcesRepo.get(name)
    if saved is not None:
        return saved
    path = _legacy_path(name)
    if path is None:
        raise NotFound(f"项目不存在：{name}")
    return [{"path": str(path), "ssh_id": 0}]


def project_path(name: str) -> Path:
    for source in project_sources(name):
        if not source["ssh_id"]:
            path = Path(source["path"])
            if path.is_dir():
                return path
    raise BadRequest("此项目没有可用的本机源文件夹")


def _source_path(path: str, ssh_id: int) -> str:
    if not path or len(path) > 4096 or "\0" in path:
        raise BadRequest("目录路径无效")
    if ssh_id:
        if not path.startswith("/"):
            raise BadRequest("请选择远程服务器上的绝对路径")
        return posixpath.normpath("/" + path.lstrip("/"))
    resolved = Path(path).expanduser().resolve()
    if hasattr(fcntl, "F_GETPATH"):
        # macOS 上 resolve() 可能保留输入的大小写。用已打开的目录获取磁盘上的路径。
        try:
            fd = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY)
            try:
                actual = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0]
            finally:
                os.close(fd)
            if actual:
                return os.fsdecode(actual)
        except OSError:
            pass
    return str(resolved)


def _validate_sources(sources: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for source in sources:
        ssh_id = source["ssh_id"]
        path = _source_path(source["path"], ssh_id)
        if (ssh_id, path) in seen:
            raise BadRequest("已关联此源文件夹")
        if ssh_id and SshRepo.get(ssh_id) is None:
            raise NotFound("SSH 连接不存在")
        if not ssh_id and not Path(path).is_dir():
            raise NotFound("目录不存在")
        seen.add((ssh_id, path))
        out.append({"path": path, "ssh_id": ssh_id})
    return out


def create_project(name: str, sources: list[dict]) -> list[dict]:
    name = name.strip()
    if not name or len(name) > 100 or name.startswith(".") or any(c in name for c in "/\\\0\n\r"):
        raise BadRequest("请输入有效的项目名称（最多 100 个字符）")
    if not sources:
        raise BadRequest("请选择源文件夹")
    if _legacy_path(name) is not None:
        raise Conflict("已有同名项目")
    try:
        ProjectSourcesRepo.create(name, _validate_sources(sources))
    except sqlite3.IntegrityError as exc:
        raise Conflict("已有同名项目") from exc
    return scan_projects()


def save_sources(name: str, sources: list[dict]) -> list[dict]:
    existing = project_sources(name)
    # 已关联的目录可能暂时离线。保存其他改动时允许保留这些关联。
    retained = {(s["ssh_id"], s["path"]) for s in existing}
    result = []
    for source in sources:
        if (source["ssh_id"], source["path"]) not in retained:
            source, = _validate_sources([source])
        elif not source["ssh_id"]:
            source = {**source, "path": _source_path(source["path"], 0)}
        if source in result:
            raise BadRequest("已关联此源文件夹")
        result.append(source)
    ProjectSourcesRepo.save(name, result)
    return scan_projects()


def _source_view(source: dict, connections: dict[int, dict]) -> dict:
    ssh_id = source["ssh_id"]
    path = source["path"]
    view = {**source, "name": Path(path).name or "/"}
    if ssh_id:
        conn = connections.get(ssh_id)
        return {**view, "connection": conn["name"] if conn else "已删除的连接",
                "ssh_project": conn["project"] if conn else "", "available": conn is not None}
    p = Path(path)
    is_git = (p / ".git").exists()
    return {**view, "available": p.is_dir(), "is_git": is_git,
            "branch": git(p, "rev-parse", "--abbrev-ref", "HEAD") if is_git else "",
            "dirty": len(git(p, "status", "--porcelain").splitlines()) if is_git else 0,
            "has_claude_md": (p / "CLAUDE.md").exists()}


def scan_projects() -> list[dict]:
    root = get_settings().projects_root
    saved = ProjectSourcesRepo.all()
    if root is not None:
        for entry in root.iterdir():
            if entry.name not in saved and (path := _legacy_path(entry.name)) is not None:
                saved[entry.name] = [{"path": str(path), "ssh_id": 0}]
    counts = TaskRepo.status_counts()
    ssh_counts = SshRepo.counts()
    connections = {c["id"]: c for c in SshRepo.folder_connections()}
    out = []
    for name, sources in sorted(saved.items(), key=lambda item: item[0].casefold()):
        sources = [_source_view(s, connections) for s in sources]
        local = next((s for s in sources if not s["ssh_id"] and s["available"]), None)
        primary = local or (sources[0] if sources else {})
        path = primary.get("path", "")
        try:
            mtime = Path(path).stat().st_mtime if local else 0
        except OSError:
            mtime = 0
        out.append({
            "name": name, "path": path, "sources": sources,
            "is_git": primary.get("is_git", False), "branch": primary.get("branch", ""),
            "dirty": primary.get("dirty", 0), "has_claude_md": primary.get("has_claude_md", False),
            "mtime": mtime, "task_counts": counts.get(name, {}),
            "ssh_count": ssh_counts.get(name, 0) + len({s["ssh_id"] for s in sources
                                                        if s["ssh_id"] and s["available"] and not s["ssh_project"]}),
        })
    return out


def set_projects_root(path: str) -> Path:
    s = get_settings()
    if s.projects_root_locked:
        raise BadRequest("项目目录由环境变量 WB_PROJECTS_ROOT 指定，不能在应用内修改")
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise BadRequest(f"目录不存在：{path}")
    s.update(projects_root=str(p))
    return p


def folder_connections() -> list[dict]:
    settings = get_settings()
    return [{**c, "recent_path": settings.recent_folder(c["id"])} for c in SshRepo.folder_connections()]


def remember_folder(ssh_id: int, path: str) -> None:
    if ssh_id and SshRepo.get(ssh_id) is None:
        raise NotFound("SSH 连接不存在")
    get_settings().remember_folder(ssh_id, _source_path(path, ssh_id))


def local_folder(path: str) -> dict:
    home = Path.home()
    if not path:
        settings = get_settings()
        recent = settings.recent_folder(0)
        if recent:
            try:
                return local_folder(recent)
            except (BadRequest, NotFound):
                pass
        path = str(settings.projects_root or home)
    folder = Path(_source_path(path, 0))
    try:
        names = [p.name for p in folder.iterdir() if not p.name.startswith(".") and p.is_dir()]
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise NotFound("目录不存在") from exc
    except PermissionError as exc:
        raise BadRequest("无权访问该目录") from exc
    except OSError as exc:
        raise BadRequest("无法读取该目录") from exc
    return {"path": str(folder), "home": str(home), "folders": sorted(names, key=str.casefold)}


async def remote_folder(ssh_id: int, path: str) -> dict:
    conn = SshRepo.get(ssh_id)
    if conn is None:
        raise NotFound("SSH 连接不存在")
    home_relative = path == "~" or path.startswith("~/")
    if home_relative:
        if len(path) > 4096 or "\0" in path:
            raise BadRequest("目录路径无效")
        directory = '"$HOME"/' + shlex.quote(path[2:].lstrip("/"))
    else:
        path = _source_path(path, ssh_id)
        directory = shlex.quote(path)
    # 一次列出当前层并有限预读下一层。记录 name/ 标识完整预读，
    # name/child 表示子目录。用 NUL 分隔记录，保留名称中的空格和换行。
    cmd = (
        f"dir={directory}; "
        '[ -d "$dir" ] || exit 20; [ -r "$dir" ] && [ -x "$dir" ] || exit 21; '
        'cd "$dir" || exit 21; '
        + ('printf "%s\\0" "$PWD"; ' if home_relative else '')
        + '''
preview_folder() {
    [ -r "$1" ] && [ -x "$1" ] || return
    set -- "$1"/*
    [ "$#" -le 64 ] || return
    printf '%s/\\0' "$name"
    for entry do
        if [ -d "$entry" ]; then printf '%s/%s\\0' "$name" "${entry##*/}"; fi
    done
}
remaining=32
for child in ./*; do
    [ -d "$child" ] || continue
    name=${child#./}
    printf '%s\\0' "$name"
    if [ "$remaining" -gt 0 ]; then
        remaining=$((remaining - 1))
        preview_folder "$child"
    fi
done
exit 0
'''
    )
    try:
        code, output, _ = await transport.run(conn, cmd, timeout=20)
    except (OSError, RuntimeError) as exc:
        raise UpstreamError(f"无法连接到{conn['name']}") from exc
    if code == 20:
        raise NotFound("目录不存在")
    if code == 21:
        raise BadRequest("无权访问该目录")
    if code:
        raise UpstreamError(f"无法连接到{conn['name']}")
    parts = output.split("\0")
    if home_relative:
        path = _source_path(parts.pop(0), ssh_id)
    folders = []
    previews: dict[str, list[str]] = {}
    for item in parts:
        name, separator, child = item.partition("/")
        if not name or name in (".", ".."):
            continue
        if not separator:
            folders.append(name)
        elif not child:
            previews[name] = []
        elif name in previews and "/" not in child and child not in (".", ".."):
            previews[name].append(child)
    result = {"path": path, "folders": sorted(folders, key=str.casefold)}
    if previews:
        result["previews"] = {name: sorted(children, key=str.casefold)
                              for name, children in previews.items()}
    return result
