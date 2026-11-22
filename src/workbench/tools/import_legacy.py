"""把旧版（网页版）的数据目录导入桌面应用的数据目录。

    python -m workbench.tools.import_legacy /path/to/old/workbench/data [--projects-root ~/dzl-py]

- 复制数据库、运行日志、图片（旧目录保持不动）
- worktree 用 `git worktree move` 移到新位置（git 记录随之更新）
- 数据库里的绝对路径（工作目录、图片）改写为新位置
- 设置项目根目录（默认：旧数据目录所在项目的上级目录）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from workbench.core.settings import configure


def _rewrite(value: str, old: str, new: str) -> str:
    return value.replace(old, new) if value and old in value else value


def import_legacy(old: Path, projects_root: Path | None, data_dir: Path | None = None) -> None:
    old = old.expanduser().resolve()
    if not (old / "workbench.db").is_file():
        sys.exit(f"不是旧版数据目录（找不到 workbench.db）：{old}")
    s = configure(data_dir)
    new = s.paths.data
    if s.paths.db.exists():
        with closing(sqlite3.connect(s.paths.db)) as c:
            if c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='tasks'").fetchone()[0] and \
                    c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]:
                sys.exit(f"新数据目录里已经有任务，不覆盖：{new}")
    print(f"导入 {old} → {new}")

    shutil.copy2(old / "workbench.db", s.paths.db)
    for sub in ("runs", "uploads"):
        if (old / sub).is_dir():
            shutil.copytree(old / sub, new / sub, dirs_exist_ok=True)
            print(f"  已复制 {sub}/")

    with closing(sqlite3.connect(s.paths.db)) as db:
        # worktree：data/worktrees/<项目>/task-N
        for tid, wd, project in db.execute("SELECT id, workdir, project FROM tasks WHERE branch != '' AND workdir LIKE ?",
                                           (str(old / "worktrees") + "/%",)).fetchall():
            src, dst = Path(wd), Path(_rewrite(wd, str(old), str(new)))
            if not src.is_dir():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["git", "worktree", "move", str(src), str(dst)], cwd=src, capture_output=True, text=True)
            print(f"  worktree #{tid} → {dst}" if r.returncode == 0 else f"  ! worktree #{tid} 移动失败：{r.stderr.strip()}")
        o, n = str(old), str(new)
        for tid, wd, imgs in db.execute("SELECT id, workdir, images FROM tasks").fetchall():
            db.execute("UPDATE tasks SET workdir=?, images=? WHERE id=?", (_rewrite(wd, o, n), _rewrite(imgs, o, n), tid))
        for rid, imgs in db.execute("SELECT id, images FROM runs").fetchall():
            db.execute("UPDATE runs SET images=? WHERE id=?", (_rewrite(imgs, o, n), rid))
        db.commit()

    root = (projects_root or old.parent.parent).expanduser().resolve()
    s.update(projects_root=str(root))
    print(f"  项目目录：{root}")
    print("完成。旧数据目录未删除，确认无误后可手动删除。")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("old_data_dir", type=Path)
    p.add_argument("--projects-root", type=Path)
    p.add_argument("--data-dir", type=Path, help="目标数据目录（默认应用数据目录）")
    a = p.parse_args()
    import_legacy(a.old_data_dir, a.projects_root, a.data_dir)


if __name__ == "__main__":
    main()
