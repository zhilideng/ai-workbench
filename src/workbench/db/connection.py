"""每次调用打开一个短连接：SQLite 在本机足够快，也避免跨线程共享连接。"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from workbench.core.settings import get_settings


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_settings().paths.db, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql: str, args=(), one: bool = False):
    """查询，返回 dict 列表；one=True 时返回第一行或 None。"""
    with closing(connect()) as conn:
        cur = conn.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
        return (rows[0] if rows else None) if one else rows


def ex(sql: str, args=()) -> int:
    """执行写操作，返回 lastrowid。"""
    with closing(connect()) as conn:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
