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
    def add_cost(task_id: int, status: str, cost: float) -> None:
        ex("""UPDATE tasks SET status=CASE WHEN parallel_pending=1 THEN 'todo' ELSE ? END,
              total_cost=total_cost+?, updated_at=?,
              progress=CASE WHEN parallel_pending=1 THEN NULL
                            WHEN parallel_group_id IS NOT NULL AND ?='review' THEN 100 ELSE progress END,
              progress_label='' WHERE id=?""",
           (status, cost, time.time(), status, task_id))

    @staticmethod
    def delete(task_id: int) -> None:
        with closing(connect()) as conn, conn:
            row = conn.execute("SELECT parallel_group_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            conn.execute("DELETE FROM runs WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM task_dependencies WHERE task_id=? OR prerequisite_id=?", (task_id, task_id))
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            if row and row["parallel_group_id"]:
                conn.execute("""DELETE FROM parallel_groups WHERE id=?
                                AND NOT EXISTS(SELECT 1 FROM tasks WHERE parallel_group_id=parallel_groups.id)""",
                             (row["parallel_group_id"],))

    @staticmethod
    def unreferenced_images(paths: set[str]) -> set[str]:
        if not paths:
            return set()
        rows = q("""SELECT image.value AS path FROM tasks, json_each(tasks.images) AS image
                    UNION SELECT image.value AS path FROM runs, json_each(runs.images) AS image""")
        return paths.difference(row["path"] for row in rows)

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


# ---------------------------------------------------------------- 并行任务组

class ParallelGroupRepo:
    @staticmethod
    def get(group_id: int) -> Optional[dict]:
        return q("SELECT * FROM parallel_groups WHERE id=?", (group_id,), one=True)

    @staticmethod
    def find(request_id: str) -> Optional[dict]:
        return q("SELECT * FROM parallel_groups WHERE request_id=?", (request_id,), one=True)

    @staticmethod
    def list() -> list[dict]:
        return q("SELECT * FROM parallel_groups WHERE state!='single' ORDER BY id DESC")

    @staticmethod
    def create(request_id: str, fields: dict) -> int:
        return ex("""INSERT INTO parallel_groups(request_id, project, title, prompt, task_fields, updated_at)
                     VALUES(?, ?, ?, ?, ?, ?)""",
                  (request_id, fields["project"], fields["title"], fields["description"],
                   json.dumps(fields, ensure_ascii=False), time.time()))

    @staticmethod
    def update(group_id: int, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        ex(f"UPDATE parallel_groups SET {_sets(fields)} WHERE id=?", (*fields.values(), group_id))

    @staticmethod
    def create_members(group_id: int, title: str, members: list[dict], dependencies: list[list[int]]) -> None:
        now = time.time()
        with closing(connect()) as conn, conn:
            ids = [_insert_task(conn, {**fields, "parallel_group_id": group_id,
                        "parallel_position": index, "parallel_pending": 1}, now)
                   for index, fields in enumerate(members)]
            conn.executemany("INSERT INTO task_dependencies(task_id, prerequisite_id) VALUES(?, ?)",
                             ((ids[index], ids[prerequisite]) for index, keys in enumerate(dependencies)
                              for prerequisite in keys))
            conn.execute("UPDATE parallel_groups SET title=?, state='ready', error='', updated_at=? WHERE id=?",
                         (title, now, group_id))

    @staticmethod
    def create_single(group_id: int, fields: dict) -> int:
        now = time.time()
        with closing(connect()) as conn, conn:
            task_id = _insert_task(conn, fields, now)
            conn.execute("UPDATE parallel_groups SET state='single', single_task_id=?, updated_at=? WHERE id=?",
                         (task_id, now, group_id))
        return task_id


class DependencyRepo:
    """前置条件以最新 CLI 运行结果为准，等待复查的结果也不能解锁下游。"""

    SUCCESS = """(p.parallel_pending=0 AND COALESCE(
                    (SELECT r.status FROM runs r WHERE r.task_id=p.id ORDER BY r.id DESC LIMIT 1), '')='success')"""
    DOWNSTREAM = """WITH RECURSIVE downstream(id) AS (
                      SELECT task_id FROM task_dependencies WHERE prerequisite_id=?
                      UNION SELECT d.task_id FROM task_dependencies d JOIN downstream a ON d.prerequisite_id=a.id
                    )"""

    @classmethod
    def prerequisites(cls, task_id: int) -> list[dict]:
        return q(f"""SELECT p.title, {cls.SUCCESS} AS succeeded,
                            (SELECT MAX(r.id) FROM runs r WHERE r.task_id=p.id) AS run_id
                     FROM task_dependencies d JOIN tasks p ON p.id=d.prerequisite_id
                     WHERE d.task_id=? ORDER BY p.parallel_position""", (task_id,))

    @classmethod
    def waiting(cls, task_id: Optional[int] = None, project: Optional[str] = None) -> list[dict]:
        sql = f"""SELECT d.task_id, p.id, p.title, p.status
                  FROM task_dependencies d JOIN tasks p ON p.id=d.prerequisite_id
                  WHERE NOT {cls.SUCCESS}"""
        args = ()
        if task_id is not None:
            sql += " AND d.task_id=?"
            args = (task_id,)
        elif project:
            # 同一任务组的会话处于同一项目。
            sql += " AND p.project=?"
            args = (project,)
        return q(sql + " ORDER BY p.parallel_position", args)

    @classmethod
    def ready(cls, group_id: Optional[int] = None) -> list[int]:
        sql = f"""SELECT t.id FROM tasks t WHERE t.parallel_pending=1 AND t.status='todo'
                  AND NOT EXISTS(SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id=d.prerequisite_id
                                 WHERE d.task_id=t.id AND NOT {cls.SUCCESS})"""
        args = ()
        if group_id is not None:
            sql += " AND t.parallel_group_id=?"
            args = (group_id,)
        return [row["id"] for row in q(sql + " ORDER BY t.id", args)]

    @classmethod
    def has_waiting_dependents(cls, task_id: int) -> bool:
        return bool(q(f"""SELECT d.task_id FROM task_dependencies d JOIN tasks p ON p.id=d.prerequisite_id
                          WHERE d.prerequisite_id=?
                          AND NOT {cls.SUCCESS} LIMIT 1""", (task_id,), one=True))

    @classmethod
    def active_descendants(cls, task_id: int) -> list[int]:
        rows = q(cls.DOWNSTREAM + " SELECT t.id FROM tasks t JOIN downstream a ON t.id=a.id"
                 f" WHERE t.status IN {ACTIVE}", (task_id,))
        return [row["id"] for row in rows]


# ---------------------------------------------------------------- runs

class RunRepo:
    @staticmethod
    def enqueue(task_id: int, prompt: str, images: list[str]) -> int:
        now = time.time()
        with closing(connect()) as conn, conn:
            cursor = conn.execute("INSERT INTO runs(task_id, prompt, status, images, started_at) VALUES(?,?,?,?,?)",
                                  (task_id, prompt, "running", json.dumps(images), now))
            conn.execute("""UPDATE tasks SET status='queued', progress=NULL, progress_label='',
                            parallel_pending=0, updated_at=? WHERE id=?""", (now, task_id))
            conn.execute(DependencyRepo.DOWNSTREAM + f"""
                         UPDATE tasks SET parallel_pending=1, updated_at=?,
                           status=CASE WHEN status IN {ACTIVE} THEN status ELSE 'todo' END,
                           progress=CASE WHEN status IN {ACTIVE} THEN progress END,
                           progress_label=CASE WHEN status IN {ACTIVE} THEN progress_label ELSE '' END
                         WHERE id IN (SELECT id FROM downstream)
                           AND (parallel_pending=0 OR status IN ('failed','cancelled'))""", (task_id, now))
            return cursor.lastrowid

    @staticmethod
    def get(run_id: int) -> Optional[dict]:
        return q("SELECT * FROM runs WHERE id=?", (run_id,), one=True)

    @staticmethod
    def of_task(task_id: int) -> list[dict]:
        return q("SELECT * FROM runs WHERE task_id=? ORDER BY id", (task_id,))

    @staticmethod
    def latest_id(task_id: int) -> Optional[int]:
        return q("SELECT MAX(id) AS id FROM runs WHERE task_id=?", (task_id,), one=True)["id"]

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
