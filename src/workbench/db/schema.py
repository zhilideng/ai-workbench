"""表结构与增量迁移。新增列：在 MIGRATIONS 里追加一行即可，旧库启动时自动补上。"""
from __future__ import annotations

import json
import time
from contextlib import closing

from workbench.db.connection import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'task',
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    priority TEXT DEFAULT 'P2',
    status TEXT DEFAULT 'todo',       -- todo | queued | running | review | done | failed | cancelled | unreported
    permission_mode TEXT DEFAULT 'acceptEdits',
    model TEXT DEFAULT '',
    effort TEXT DEFAULT '',
    engine TEXT DEFAULT 'claude',     -- 见 engines 注册表
    use_worktree INTEGER DEFAULT 0,
    ssh_id INTEGER DEFAULT 0,         -- 执行位置：0 = 本机；否则为 ssh_conns.id
    source_path TEXT DEFAULT '',
    workdir TEXT DEFAULT '',
    branch TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    parent_task_id INTEGER,          -- CLI 创建子会话时，对应的主任务
    agent_id TEXT NOT NULL DEFAULT '',
    progress INTEGER,
    progress_label TEXT NOT NULL DEFAULT '',
    fork_at TEXT DEFAULT '',          -- claude：下次继续时先回退到这条消息（--resume-session-at）
    images TEXT DEFAULT '[]',         -- 首条消息附带的图片（uploads 下的绝对路径，JSON）
    total_cost REAL DEFAULT 0,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    prompt TEXT,
    status TEXT DEFAULT 'running',    -- running | success | error | cancelled | interrupted | unreported
    agent_turn_id TEXT NOT NULL DEFAULT '', -- Codex 子会话原生轮次 ID
    pid INTEGER,
    exit_code INTEGER,
    cost REAL DEFAULT 0,
    result TEXT DEFAULT '',
    images TEXT DEFAULT '[]',
    started_at REAL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS ssh_conns (      -- 工作台共享的 SSH 远程连接（密码存在钥匙串）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,           -- 来源项目，用于 AI 上下文；从目录选择器添加时为空
    name TEXT NOT NULL,
    host TEXT NOT NULL,               -- 主机名 / IP，或 ~/.ssh/config 里的 Host 别名
    port INTEGER DEFAULT 0,           -- 0 = 默认（22 或 ssh config）
    user TEXT DEFAULT '',
    identity_file TEXT DEFAULT '',
    proxy_jump TEXT DEFAULT '',
    socks_proxy TEXT DEFAULT '',      -- SOCKS5 代理 host:port（经 nc -X 5 转发）
    auth TEXT DEFAULT 'key',          -- key | password（密码存在 macOS 钥匙串，不进数据库）
    remote_claude TEXT DEFAULT '',    -- 服务器上 claude / codex 的路径，空 = 用登录 shell 的 PATH
    remote_codex TEXT DEFAULT '',
    allow_root_bypass INTEGER DEFAULT 0,
    remote_dir TEXT DEFAULT '',
    note TEXT DEFAULT '',
    share_with_ai INTEGER DEFAULT 1,
    created_at REAL,
    updated_at REAL,
    UNIQUE(project, name)
);
CREATE TABLE IF NOT EXISTS project_sources (
    project TEXT PRIMARY KEY,
    sources TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
"""

# (表, 列, 定义)：旧库缺这一列时补上
MIGRATIONS: list[tuple[str, str, str]] = [
    ("tasks", "engine", "TEXT DEFAULT 'claude'"),
    ("tasks", "effort", "TEXT DEFAULT ''"),
    ("tasks", "fork_at", "TEXT DEFAULT ''"),
    ("tasks", "images", "TEXT DEFAULT '[]'"),
    ("tasks", "ssh_id", "INTEGER DEFAULT 0"),
    ("tasks", "source_path", "TEXT DEFAULT ''"),
    ("tasks", "parent_task_id", "INTEGER"),
    ("tasks", "agent_id", "TEXT NOT NULL DEFAULT ''"),
    ("tasks", "progress", "INTEGER"),
    ("tasks", "progress_label", "TEXT NOT NULL DEFAULT ''"),
    ("ssh_conns", "socks_proxy", "TEXT DEFAULT ''"),
    ("ssh_conns", "auth", "TEXT DEFAULT 'key'"),
    ("ssh_conns", "remote_claude", "TEXT DEFAULT ''"),
    ("ssh_conns", "remote_codex", "TEXT DEFAULT ''"),
    ("ssh_conns", "allow_root_bypass", "INTEGER DEFAULT 0"),
    ("runs", "images", "TEXT DEFAULT '[]'"),
    ("runs", "agent_turn_id", "TEXT NOT NULL DEFAULT ''"),
]


def init_db() -> None:
    with closing(connect()) as conn:
        # 先补列再建索引：旧库的表已存在，CREATE TABLE IF NOT EXISTS 不会改它
        conn.executescript(SCHEMA.split("CREATE INDEX")[0])
        cols: dict[str, set[str]] = {}
        for table, col, ddl in MIGRATIONS:
            have = cols.setdefault(table, {r[1] for r in conn.execute(f"PRAGMA table_info({table})")})
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                have.add(col)
        conn.executescript("CREATE INDEX" + "CREATE INDEX".join(SCHEMA.split("CREATE INDEX")[1:]))
        # 应用重启时，之前在跑的进程已经不受管理，标记为中断
        conn.execute("UPDATE runs SET status='interrupted', ended_at=? WHERE status='running'", (time.time(),))
        conn.execute("UPDATE tasks SET status='failed' WHERE status IN ('running','queued')")
        # 旧版将没有收到 CLI 最终事件的子会话误记为中断和失败。
        conn.execute("UPDATE runs SET status='unreported' WHERE status='interrupted' AND task_id IN "
                     "(SELECT id FROM tasks WHERE agent_id<>'')")
        conn.execute("""UPDATE tasks SET status='unreported' WHERE agent_id<>'' AND status='failed'
                        AND (SELECT status FROM runs WHERE task_id=tasks.id ORDER BY id DESC LIMIT 1)='unreported'""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(parent_task_id, agent_id)")
        # 旧版已生成的卡片继续按独立任务使用。尚未生成卡片的请求恢复为待办。
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='parallel_groups'").fetchone():
            now = time.time()
            for group in conn.execute("SELECT * FROM parallel_groups WHERE state IN ('planning', 'failed')"):
                try:
                    fields = json.loads(group["task_fields"])
                except (TypeError, ValueError):
                    fields = {}
                if not isinstance(fields, dict):
                    fields = {}
                cur = conn.execute("""INSERT INTO tasks(project, title, description, engine, permission_mode,
                                      model, effort, use_worktree, ssh_id, source_path, images, created_at, updated_at)
                                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                   (group["project"], group["title"], group["prompt"], fields.get("engine", "claude"),
                                    fields.get("permission_mode", "acceptEdits"), fields.get("model", ""),
                                    fields.get("effort", ""), fields.get("use_worktree", 0), fields.get("ssh_id", 0),
                                    fields.get("source_path", ""), fields.get("images", "[]"), group["updated_at"], now))
                conn.execute("UPDATE parallel_groups SET state='single', single_task_id=? WHERE id=?",
                             (cur.lastrowid, group["id"]))
        conn.commit()
