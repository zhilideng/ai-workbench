"""各表的读写。服务层只通过这里访问数据库，SQL 集中在一处。"""
from __future__ import annotations

import json
import time
from contextlib import closing
from typing import Any, Optional

from workbench.db.connection import connect, ex, q

ACTIVE = ("running", "queued")


def _sets(fields: dict[str, Any]) -> str:
    return ", ".join(f"{k}=?" for k in fields)


def _insert_task(conn, fields: dict[str, Any], now: float) -> int:
    fields = {**fields, "created_at": now, "updated_at": now}
    marks = ", ".join("?" for _ in fields)
    return conn.execute(f"INSERT INTO tasks({', '.join(fields)}) VALUES({marks})", tuple(fields.values())).lastrowid


# ---------------------------------------------------------------- tasks

class TaskRepo:
    SELECT = """SELECT t.*, COALESCE(s.name, CASE WHEN t.ssh_id THEN '已删除的连接' ELSE '' END) ssh_name,
                       CASE WHEN t.status='running' THEN (
                           SELECT r.id FROM runs r WHERE r.task_id=t.id AND r.status='running' ORDER BY r.id DESC LIMIT 1
                       ) END active_run_id,
                       CASE WHEN t.status IN ('review','done','failed','cancelled','unreported') THEN (
                           SELECT MAX(r.id) FROM runs r WHERE r.task_id=t.id
                       ) END last_run_id
                FROM tasks t LEFT JOIN ssh_conns s ON s.id=t.ssh_id"""
    # 旧版可能把子会话回复的接收方（主会话）也显示成一张子卡。
    VISIBLE = """NOT EXISTS (SELECT 1 FROM tasks parent WHERE parent.id=t.parent_task_id
                             AND t.engine='codex' AND t.agent_id<>'' AND t.agent_id=parent.session_id)"""
    SUBTREE = """WITH RECURSIVE subtree(id) AS (
                    SELECT id FROM tasks WHERE id=?
                    UNION SELECT child.id FROM tasks child JOIN subtree parent ON child.parent_task_id=parent.id
                )"""

    @staticmethod
    def get(task_id: int) -> Optional[dict]:
        return q("SELECT * FROM tasks WHERE id=?", (task_id,), one=True)

    @classmethod
    def get_view(cls, task_id: int) -> Optional[dict]:
        """带连接名的任务（给界面用）。"""
        return q(cls.SELECT + " WHERE t.id=?", (task_id,), one=True)

    @classmethod
    def list(cls, project: Optional[str] = None) -> list[dict]:
        sql = f"{cls.SELECT} WHERE {cls.VISIBLE}"
        if project:
            return q(sql + " AND t.project=? ORDER BY t.id DESC", (project,))
        return q(sql + " ORDER BY t.id DESC")

    @staticmethod
    def create(**fields: Any) -> int:
        with closing(connect()) as conn, conn:
            return _insert_task(conn, fields, time.time())

    @staticmethod
    def update(task_id: int, touch: bool = True, **fields: Any) -> None:
        if not fields:
            return
        if touch:
            fields["updated_at"] = time.time()
        ex(f"UPDATE tasks SET {_sets(fields)} WHERE id=?", (*fields.values(), task_id))

    @staticmethod
    def set_agent_review_status(task_id: int, status: str) -> bool:
        # 核对状态和写入在同一个 SQL 语句内，避免覆盖 CLI 刚启动的新一轮。
        with closing(connect()) as conn, conn:
            return conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=? AND agent_id<>'' "
                                "AND status IN ('review','done')", (status, time.time(), task_id)).rowcount == 1

    @staticmethod
    def add_cost(task_id: int, status: str, cost: float) -> None:
        ex("UPDATE tasks SET status=?, total_cost=total_cost+?, updated_at=?, progress_label='' WHERE id=?",
           (status, cost, time.time(), task_id))

    @staticmethod
    def agents(task_id: int) -> list[dict]:
        return q("SELECT * FROM tasks WHERE parent_task_id=? ORDER BY id", (task_id,))

    @classmethod
    def subtree(cls, task_id: int) -> list[dict]:
        return q(cls.SUBTREE + " SELECT tasks.* FROM tasks JOIN subtree USING (id)", (task_id,))

    @classmethod
    def delete(cls, task_id: int) -> list[dict]:
        """在一个事务内删除整组任务及运行记录，返回待清理的日志和附件。"""
        with closing(connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            runs = conn.execute(cls.SUBTREE + " SELECT runs.id, runs.images FROM runs JOIN subtree ON runs.task_id=subtree.id",
                                (task_id,)).fetchall()
            conn.execute(cls.SUBTREE + " DELETE FROM runs WHERE task_id IN (SELECT id FROM subtree)", (task_id,))
            conn.execute(cls.SUBTREE + " DELETE FROM tasks WHERE id IN (SELECT id FROM subtree)", (task_id,))
            return [dict(run) for run in runs]

    @staticmethod
    def unreferenced_images(paths: set[str]) -> set[str]:
        if not paths:
            return set()
        rows = q("""SELECT image.value AS path FROM tasks, json_each(tasks.images) AS image
                    UNION SELECT image.value AS path FROM runs, json_each(runs.images) AS image""")
        return paths.difference(row["path"] for row in rows)

    @classmethod
    def status_counts(cls) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in q(f"SELECT project, status, COUNT(*) c FROM tasks t WHERE {cls.VISIBLE} GROUP BY project, status"):
            out.setdefault(r["project"], {})[r["status"]] = r["c"]
        return out

    @staticmethod
    def active_on_conn(conn_id: int) -> list[dict]:
        return q(f"SELECT id FROM tasks WHERE ssh_id=? AND agent_id='' AND status IN {ACTIVE}", (conn_id,))

    @staticmethod
    def images(task: dict) -> list[str]:
        return json.loads(task.get("images") or "[]")


# ---------------------------------------------------------------- runs

class RunRepo:
    @staticmethod
    def enqueue(task_id: int, prompt: str, images: list[str]) -> int:
        now = time.time()
        with closing(connect()) as conn, conn:
            cursor = conn.execute("INSERT INTO runs(task_id, prompt, status, images, started_at) VALUES(?,?,?,?,?)",
                                  (task_id, prompt, "running", json.dumps(images), now))
            conn.execute("UPDATE tasks SET status='queued', progress=NULL, progress_label='', updated_at=? WHERE id=?",
                         (now, task_id))
            return cursor.lastrowid

    @staticmethod
    def start_agent(task_id: int, prompt: str, agent_turn_id: str = "") -> int:
        now = time.time()
        with closing(connect()) as conn, conn:
            cur = conn.execute("INSERT INTO runs(task_id, prompt, status, started_at, agent_turn_id) VALUES (?, ?, 'running', ?, ?)",
                               (task_id, prompt, now, agent_turn_id))
            conn.execute("UPDATE tasks SET status='running', progress=NULL, progress_label='', updated_at=? WHERE id=?",
                         (now, task_id))
            return cur.lastrowid

    @staticmethod
    def set_agent_turn(run_id: int, turn_id: str) -> None:
        ex("UPDATE runs SET agent_turn_id=? WHERE id=?", (turn_id, run_id))

    @staticmethod
    def unreported_codex_agents() -> list[dict]:
        return q("""SELECT r.id, r.task_id, r.agent_turn_id, r.started_at, r.ended_at, t.agent_id FROM runs r
                    JOIN tasks t ON t.id=r.task_id
                    WHERE r.status='unreported' AND t.agent_id<>'' AND t.engine='codex' AND COALESCE(t.ssh_id, 0)=0
                    ORDER BY r.id DESC""")

    @staticmethod
    def reconcile_agent(run_id: int, task_id: int, run_status: str, task_status: str, result: str,
                        ended_at: Optional[float]) -> None:
        with closing(connect()) as conn, conn:
            cur = conn.execute("""UPDATE runs SET status=?, result=?, ended_at=COALESCE(?, ended_at)
                                  WHERE id=? AND status='unreported'""", (run_status, result, ended_at, run_id))
            if cur.rowcount:
                conn.execute("""UPDATE tasks SET status=?, updated_at=? WHERE id=? AND status='unreported'
                                AND (SELECT MAX(id) FROM runs WHERE task_id=tasks.id)=?""",
                             (task_status, time.time(), task_id, run_id))

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
