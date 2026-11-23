"""各表的读写。服务层只通过这里访问数据库，SQL 集中在一处。"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from workbench.db.connection import ex, q

ACTIVE = ("running", "queued")


def _sets(fields: dict[str, Any]) -> str:
    return ", ".join(f"{k}=?" for k in fields)


# ---------------------------------------------------------------- tasks

class TaskRepo:
    SELECT = ("SELECT t.*, COALESCE(s.name, CASE WHEN t.ssh_id THEN '已删除的连接' ELSE '' END) ssh_name "
              "FROM tasks t LEFT JOIN ssh_conns s ON s.id=t.ssh_id")

    @staticmethod
    def get(task_id: int) -> Optional[dict]:
        return q("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)

    @classmethod
    def get_view(cls, task_id: int) -> Optional[dict]:
        """带连接名的任务（给界面用）。"""
        return q(cls.SELECT + " WHERE t.id=?", (task_id,), one=True)

    @classmethod
    def list(cls, project: Optional[str] = None) -> list[dict]:
        if project:
            return q(cls.SELECT + " WHERE t.project=? ORDER BY t.id DESC", (project,))
        return q(cls.SELECT + " ORDER BY t.id DESC")

    @staticmethod
    def create(**fields: Any) -> int:
        now = time.time()
        fields = {**fields, "created_at": now, "updated_at": now}
        return ex(f"INSERT INTO tasks({', '.join(fields)}) VALUES({', '.join('?' * len(fields))})",
                  tuple(fields.values()))

    @staticmethod
    def update(task_id: int, touch: bool = True, **fields: Any) -> None:
        if not fields:
            return
        if touch:
            fields["updated_at"] = time.time()
        ex(f"UPDATE tasks SET {_sets(fields)} WHERE id=?", (*fields.values(), task_id))

    @staticmethod
    def add_cost(task_id: int, status: str, cost: float) -> None:
        ex("UPDATE tasks SET status=?, total_cost=total_cost+?, updated_at=? WHERE id=?",
           (status, cost, time.time(), task_id))

    @staticmethod
    def delete(task_id: int) -> None:
        ex("DELETE FROM runs WHERE task_id=?", (task_id,))
        ex("DELETE FROM tasks WHERE id=?", (task_id,))

    @staticmethod
    def status_counts() -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in q("SELECT project, status, COUNT(*) c FROM tasks GROUP BY project, status"):
            out.setdefault(r["project"], {})[r["status"]] = r["c"]
        return out

    @staticmethod
    def active_on_conn(conn_id: int) -> list[dict]:
        return q(f"SELECT id FROM tasks WHERE ssh_id=? AND status IN {ACTIVE}", (conn_id,))

    @staticmethod
    def images(task: dict) -> list[str]:
        return json.loads(task.get("images") or "[]")


# ---------------------------------------------------------------- runs

class RunRepo:
    @staticmethod
    def create(task_id: int, prompt: str, images: list[str]) -> int:
        return ex("INSERT INTO runs(task_id, prompt, status, images, started_at) VALUES(?,?,?,?,?)",
                  (task_id, prompt, "running", json.dumps(images), time.time()))

    @staticmethod
    def get(run_id: int) -> Optional[dict]:
        return q("SELECT * FROM runs WHERE id=?", (run_id,), one=True)

    @staticmethod
    def of_task(task_id: int) -> list[dict]:
        return q("SELECT * FROM runs WHERE task_id=? ORDER BY id", (task_id,))

    @staticmethod
    def summaries(task_id: int) -> list[dict]:
        return q("SELECT id, status, exit_code, cost, started_at, ended_at, substr(prompt,1,200) prompt "
                 "FROM runs WHERE task_id=? ORDER BY id", (task_id,))

    @staticmethod
    def set_pid(run_id: int, pid: int) -> None:
        ex("UPDATE runs SET pid=? WHERE id=?", (pid, run_id))

    @staticmethod
    def finish(run_id: int, status: str, exit_code: Optional[int], cost: float, result: str) -> None:
        ex("UPDATE runs SET status=?, exit_code=?, cost=?, result=?, ended_at=? WHERE id=?",
           (status, exit_code, cost, result, time.time(), run_id))

    @staticmethod
    def delete(run_id: int) -> None:
        ex("DELETE FROM runs WHERE id=?", (run_id,))


# ---------------------------------------------------------------- ssh_conns

class SshRepo:
    @staticmethod
    def get(conn_id: int) -> Optional[dict]:
        return q("SELECT * FROM ssh_conns WHERE id=?", (conn_id,), one=True)

    @staticmethod
    def of_project(project: str) -> list[dict]:
        return q("SELECT * FROM ssh_conns WHERE project=? ORDER BY name COLLATE NOCASE", (project,))

    @staticmethod
    def list_all() -> list[dict]:
        return q("SELECT * FROM ssh_conns ORDER BY name COLLATE NOCASE, id")

    @staticmethod
    def folder_connections() -> list[dict]:
        return q("SELECT id, project, name, remote_dir FROM ssh_conns ORDER BY name COLLATE NOCASE, id")

    @staticmethod
    def create(project: str, fields: dict[str, Any]) -> int:
        now = time.time()
        return ex(f"INSERT INTO ssh_conns(project, {', '.join(fields)}, created_at, updated_at) "
                  f"VALUES(?, {', '.join('?' * len(fields))}, ?, ?)", (project, *fields.values(), now, now))

    @staticmethod
    def update(conn_id: int, fields: dict[str, Any]) -> None:
        ex(f"UPDATE ssh_conns SET {_sets(fields)}, updated_at=? WHERE id=?", (*fields.values(), time.time(), conn_id))

    @staticmethod
    def delete(conn_id: int) -> None:
        ex("DELETE FROM ssh_conns WHERE id=?", (conn_id,))

    @staticmethod
    def used_by_remote_claude() -> list[dict]:
        return q("""SELECT s.* FROM ssh_conns s WHERE EXISTS
                    (SELECT 1 FROM tasks t WHERE t.ssh_id=s.id AND COALESCE(t.engine,'claude')='claude')
                    ORDER BY s.project, s.name""")


class ProjectSourcesRepo:
    @staticmethod
    def get(project: str) -> Optional[list[dict]]:
        row = q("SELECT sources FROM project_sources WHERE project=?", (project,), one=True)
        return json.loads(row["sources"]) if row else None

    @staticmethod
    def all() -> dict[str, list[dict]]:
        return {r["project"]: json.loads(r["sources"]) for r in q("SELECT project, sources FROM project_sources")}

    @staticmethod
    def create(project: str, sources: list[dict]) -> None:
        ex("INSERT INTO project_sources(project, sources) VALUES(?, ?)", (project, json.dumps(sources)))

    @staticmethod
    def save(project: str, sources: list[dict]) -> None:
        ex("INSERT INTO project_sources(project, sources) VALUES(?, ?) "
           "ON CONFLICT(project) DO UPDATE SET sources=excluded.sources", (project, json.dumps(sources)))
