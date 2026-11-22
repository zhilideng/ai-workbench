"""项目 = 项目根目录下的一个子目录。"""
from __future__ import annotations

from pathlib import Path

from workbench.core.errors import BadRequest, NotFound
from workbench.core.settings import get_settings
from workbench.core.shell import git
from workbench.db.repositories import SshRepo, TaskRepo


def projects_root() -> Path:
    root = get_settings().projects_root
    if root is None:
        raise BadRequest("还没有设置项目目录")
    return root


def project_path(name: str) -> Path:
    root = projects_root()
    p = (root / name).resolve()
    if p.parent != root or not p.is_dir():
        raise NotFound(f"项目不存在: {name}")
    return p


def scan_projects() -> list[dict]:
    root = get_settings().projects_root
    if root is None:
        return []
    counts = TaskRepo.status_counts()
    ssh_counts = SshRepo.counts()
    data_dir = get_settings().paths.data
    out = []
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir() or p.name.startswith(".") or p == data_dir:
            continue
        is_git = (p / ".git").exists()
        out.append({
            "name": p.name,
            "path": str(p),
            "is_git": is_git,
            "branch": git(p, "rev-parse", "--abbrev-ref", "HEAD") if is_git else "",
            "dirty": len(git(p, "status", "--porcelain").splitlines()) if is_git else 0,
            "has_claude_md": (p / "CLAUDE.md").exists(),
            "mtime": p.stat().st_mtime,
            "task_counts": counts.get(p.name, {}),
            "ssh_count": ssh_counts.get(p.name, 0),
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
