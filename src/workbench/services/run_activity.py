"""把运行事件转换为看板上的操作和思考摘要。"""
import re
import shlex
from collections.abc import Iterable, Iterator
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from workbench.engines.planning import event_plan


_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")
_AUTH_HEADER = re.compile(r"""(?i)(\bAuthorization["']?\s*:\s*["']?\s*(?:Bearer|Basic)\s+)[^\s"']+""")
_URL_CREDENTIALS = re.compile(r"""(\b[a-z][a-z0-9+.-]*://)[^/\s:@'"]+:[^/\s@'"]+@""", re.I)
_SECRET_NAME = r"[\w.-]*(?:api[_-]?key|access[_-]?key|secret|token|passwd|password|private[_-]?key)"
_SECRET_VALUE = re.compile(
    rf"""(?i)((?:\b{_SECRET_NAME}\b["']?[ \t]*[:=][ \t]*|--{_SECRET_NAME}\b[ \t]+))"""
    r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s"';&,}\]]+)"""
)
_TOKEN = re.compile(
    r"\b(?:sk-(?:ant-|proj-|svcacct-)?[\w-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[\w]{40,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[\w-]{35}|xox[abprs]-[\w-]{10,}|"
    r"eyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,})"
)
_LOGIN_SHELL = re.compile(r"^/(?:usr/)?bin/(?:zsh|bash|sh) -lc\s+")
_SHELL_SYNTAX = re.compile(r"""'[^']*'|"(?:\\.|[^"\\])*"|\\[\s\S]|(?<!\S)#[^\n]*|\$\(|\x60|<<|&>|[()]|&&|\|\||[;\n|&]""")
_SHELL_BOUNDARIES = {"&&", "||", ";", "\n", "|", "&"}
_OPAQUE_SHELL = {"$(", "\x60", "<<", "&>", "(", ")"}
_FILE_ACTIONS = {"add": "新增", "update": "修改", "delete": "删除"}
_TOOLS = {"Read": "读取文件", "Write": "写入文件", "Edit": "编辑文件", "MultiEdit": "编辑文件",
          "NotebookEdit": "编辑笔记本", "Grep": "搜索内容", "Glob": "查找文件", "apply_patch": "修改文件",
          "Task": "启动子任务", "Agent": "启动子任务", "browser_navigate": "打开网页",
          "browser_snapshot": "读取网页内容", "browser_click": "操作网页", "firecrawl_scrape": "读取网页",
          "search": "搜索数据"}
_PROGRAMS = {"pwd": "查看当前目录", "ls": "列出文件", "find": "查找文件", "rg": "搜索内容",
             "grep": "搜索内容", "cat": "读取文件", "sed": "处理文本", "echo": "输出文本",
             "printf": "输出文本", "pytest": "运行测试", "mkdir": "创建目录", "bash": "运行 Bash 脚本",
             "zsh": "运行 Zsh 脚本", "sh": "运行 Shell 脚本", "node": "运行 JavaScript",
             "uname": "检查系统信息", "whoami": "检查当前用户", "id": "检查用户权限",
             "df": "检查磁盘空间", "du": "统计文件占用", "free": "查看内存使用",
             "vm_stat": "查看内存使用", "ps": "查看进程", "lsof": "检查打开的文件或端口",
             "netstat": "检查网络连接和端口", "ss": "检查网络连接和端口", "nc": "检查网络连通性",
             "ping": "检查网络连通性", "kill": "向进程发送信号", "tail": "读取最近的日志或文件内容",
             "head": "读取文件开头", "wc": "统计行数或字节数", "sort": "排序数据",
             "uniq": "合并重复行", "awk": "处理文本数据", "jq": "解析 JSON",
             "cd": "切换目录", "touch": "创建文件或更新时间", "cp": "复制文件", "mv": "移动文件",
             "rm": "删除文件", "chmod": "更改文件权限", "diff": "比较文件",
             "zip": "压缩文件", "unzip": "解压文件", "gzip": "压缩文件", "gunzip": "解压文件",
             "scp": "传输远程文件", "sftp": "传输远程文件", "rsync": "同步文件",
             "ssh": "连接 SSH 服务器", "wget": "下载文件", "sqlite3": "访问 SQLite 数据库",
             "psql": "访问 PostgreSQL 数据库", "mysql": "访问 MySQL 数据库",
             "redis-cli": "访问 Redis 数据源", "make": "构建项目", "cmake": "配置或构建项目"}
_PACKAGE_ACTIONS = {
    "install": "安装依赖", "i": "安装依赖", "ci": "安装依赖", "add": "安装依赖", "sync": "同步依赖",
    "uninstall": "卸载依赖", "remove": "卸载依赖", "list": "检查已安装依赖", "ls": "检查已安装依赖",
    "show": "查看依赖信息", "check": "检查依赖", "build": "构建项目", "test": "运行测试",
    "dev": "启动开发服务", "start": "启动应用", "lint": "检查代码规范", "typecheck": "检查类型",
    "venv": "创建虚拟环境",
}
_REPOSITORY_ACTIONS = {
    "status": "查看 Git 状态", "branch": "查看 Git 分支", "diff": "查看 Git 差异",
    "log": "查看 Git 提交记录", "show": "查看 Git 对象", "fetch": "获取远程仓库更新",
    "pull": "拉取仓库更新", "push": "推送仓库更新", "clone": "克隆仓库", "rev-parse": "查看 Git 引用",
}
_DATA_TOOLS = {
    "get_me": "读取账户信息", "repo_read": "读取仓库", "get_repository": "读取仓库",
    "get_repo": "读取仓库", "read_repository": "读取仓库", "search_code": "搜索仓库代码",
    "get_file_contents": "读取仓库文件", "list_repositories": "列出仓库", "search_repositories": "搜索仓库",
    "list_commits": "读取提交记录", "get_commit": "读取提交", "list_branches": "读取分支列表",
    "list_issues": "读取 Issue 列表", "get_issue": "读取 Issue", "issue_read": "读取 Issue",
    "search_issues": "搜索 Issue", "list_pull_requests": "读取 Pull Request 列表",
    "get_pull_request": "读取 Pull Request", "pull_request_read": "读取 Pull Request",
    "search_pull_requests": "搜索 Pull Request", "list_mcp_resources": "查看可用数据源",
    "list_mcp_resource_templates": "查看数据源模板", "read_mcp_resource": "读取数据源",
    "list_tables": "查看库表", "describe_table": "查看表结构", "query_database": "查询数据库",
    "query": "查询数据源", "execute_sql": "执行数据库操作", "sql_query": "查询数据库",
    "connect": "连接数据源", "connect_database": "连接数据源", "list_files": "列出文件",
    "search_documents": "搜索文档", "get_document": "读取文档", "read_document": "读取文档",
    "list_records": "读取数据记录", "read_records": "读取数据记录",
}
_DATA_STATUS = {
    "starting": "running", "connecting": "running", "ready": "done", "connected": "done",
    "cancelled": "interrupted", "failed": "failed", "needs-auth": "failed", "authenticationRequired": "failed",
}
_REMOTE_ACTIONS = {"connect": "准备 SSH 连接", "upload": "传输任务附件"}
_AGENT_TOOLS = {
    "spawnAgent": "启动子任务", "resumeAgent": "恢复子任务", "wait": "等待子任务",
    "sendInput": "向子任务发送消息", "sendMessage": "向子任务发送消息", "followupTask": "继续子任务",
    "interruptAgent": "停止子任务", "closeAgent": "关闭子任务", "listAgents": "查看子任务",
}
_AGENT_ACTIVITY = {
    "started": "子任务已启动", "interacted": "与子任务交互", "completed": "子任务已完成",
    "interrupted": "子任务已中止",
}


def _display(value: str) -> str:
    """在发送到看板前剥离终端控制符并遮蔽常见凭据格式。"""
    value = _CONTROL.sub("", _CSI.sub("", value)).replace("\r\n", "\n").replace("\r", "\n")
    value = _PRIVATE_KEY.sub("[已隐藏]", value)
    value = _AUTH_HEADER.sub(r"\1[已隐藏]", value)
    value = _URL_CREDENTIALS.sub(r"\1[已隐藏]@", value)
    value = _SECRET_VALUE.sub(r"\1[已隐藏]", value)
    return _TOKEN.sub("[已隐藏]", value).strip()


def _shell_command(command: str) -> str:
    # Codex 会把命令包装成 /bin/zsh -lc '…'。显示内部真正执行的命令。
    if _LOGIN_SHELL.match(command):
        try:
            argv = shlex.split(command)
        except ValueError:
            return command
        if len(argv) == 3:
            return argv[2]
    return command


def _target(label: str, target: str = "") -> str:
    return f"{label} · {target}" if target else label


def _note(value: str | list[str] | None) -> str:
    """取首段可读摘要，完整文本继续由任务详情展示。"""
    parts = value if isinstance(value, list) else [value]
    for part in parts:
        if not isinstance(part, str):
            continue
        prose = re.sub(r"(?ms)^[ \t]*\x60\x60\x60.*?(?:^[ \t]*\x60\x60\x60[^\n]*|\Z)", "", part).strip()
        if prose:
            return " ".join(re.split(r"\n\s*\n", prose, maxsplit=1)[0].split())
    return ""


def _page(url: str) -> str:
    try:
        parts = urlsplit(url)
        if not parts.hostname:
            return parts.path
        port = f":{parts.port}" if parts.port is not None else ""
        return f"{parts.hostname}{port}{parts.path.rstrip('/')}"
    except ValueError:
        return ""


def _web_label(action: dict, query: str = "") -> str:
    kind = action.get("type")
    if kind in ("openPage", "open_page"):
        return _target("打开网页", _page(action.get("url") or ""))
    if kind in ("findInPage", "find_in_page"):
        pattern = action.get("pattern") or ""
        page = _page(action.get("url") or "")
        return _target("在网页中查找", f"{pattern}（{page}）" if pattern and page else pattern or page)
    if kind == "other" and not query:
        return "使用网页工具"
    queries = [value for value in action.get("queries") or [] if isinstance(value, str) and value]
    return _target("搜索网页", "；".join(dict.fromkeys(queries)) or action.get("query") or query)


def _join_actions(labels: Iterable[str]) -> str:
    unique = list(dict.fromkeys(label for label in labels if label))
    summary = "；".join(unique[:4])
    return f"{summary} 等 {len(unique)} 项操作" if len(unique) > 4 else summary


def _shell_actions(command: str) -> Iterator[str]:
    """识别常见的顶层命令组合。复杂 Shell 语法交给 CLI 提供的结构化动作。"""
    start = 0
    for match in _SHELL_SYNTAX.finditer(command):
        syntax = match[0]
        if syntax in _OPAQUE_SHELL:
            break
        if syntax in _SHELL_BOUNDARIES:
            yield _program_label(command[start:match.start()])
            start = match.end()
    yield _program_label(command[start:])


def _command_label(command: str, actions: Iterable[dict] = ()) -> str:
    labels = []
    for action in actions:
        kind = action.get("type")
        path = action.get("path") or ""
        if kind == "read":
            labels.append(_target("读取文件", action.get("name") or path))
        elif kind in ("listFiles", "list_files"):
            labels.append(_target("列出文件", path))
        elif kind == "search":
            labels.append(_target("搜索内容", action.get("query") or path))
        elif action.get("command"):
            labels.extend(_shell_actions(_shell_command(action["command"])))
    return _join_actions(labels or _shell_actions(_shell_command(command))) or "运行终端命令"


def _program_label(command: str) -> str:
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return "运行终端命令"
    position = next((i for i, value in enumerate(tokens) if not re.match(r"^[A-Za-z_]\w*=", value)), len(tokens))
    if position == len(tokens):
        return ""
    program = PurePosixPath(tokens[position]).name
    args = tokens[position + 1:]
    first = args[0] if args else ""
    if re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", program):
        if first in ("--version", "-V"):
            return "检查 Python 版本"
        if args[:2] in (["-m", "pytest"], ["-m", "unittest"]):
            return "运行 Python 测试"
        if args[:2] == ["-m", "http.server"]:
            return "启动本地 Web 服务"
        if args[:2] == ["-m", "venv"]:
            return "创建虚拟环境"
        if args[:2] == ["-m", "pip"]:
            return _PACKAGE_ACTIONS.get(args[2] if len(args) > 2 else "", "管理 Python 依赖")
        return _target("运行 Python 脚本", first if first.endswith(".py") else "")
    if program in ("npm", "pnpm", "yarn", "bun", "pip", "pip3", "uv"):
        if first in ("--version", "-V"):
            return f"检查 {program} 版本"
        action = args[1] if first in ("run", "pip") and len(args) > 1 else first
        fallback = _target("运行项目脚本", action) if first == "run" else f"运行 {program}"
        return _PACKAGE_ACTIONS.get(action, fallback)
    if program == "git":
        return "检查 Git 版本" if first == "--version" else _REPOSITORY_ACTIONS.get(first, "操作 Git 仓库")
    if program == "gh":
        verb = args[1] if len(args) > 1 else ""
        if verb in ("view", "list", "search"):
            return {"repo": "读取仓库", "issue": "读取 Issue", "pr": "读取 Pull Request"}.get(first, "查询 GitHub")
        return {"repo": "操作仓库", "issue": "操作 Issue", "pr": "操作 Pull Request",
                "api": "请求 GitHub API", "auth": "检查 GitHub 认证"}.get(first, "访问 GitHub")
    if program == "sleep" and re.fullmatch(r"\d+(?:\.\d+)?", first):
        return f"等待 {first} 秒"
    if program == "curl":
        url = next((arg for arg in args if arg.startswith(("https://", "http://"))), "")
        return _target("请求网页", _page(url))
    if program == "tar":
        option = first.lstrip("-")
        if option in ("extract", "list", "create"):
            return {"extract": "解压文件", "list": "查看压缩包内容", "create": "打包文件"}[option]
        if option.isalpha() and not first.startswith("--"):
            for letter, label in (("x", "解压文件"), ("t", "查看压缩包内容"), ("c", "打包文件")):
                if letter in option:
                    return label
        return "处理压缩包"
    if program in ("cat", "ls") and first and not first.startswith(("-", "<", ">")):
        return _target(_PROGRAMS[program], first)
    return _PROGRAMS.get(program) or (f"运行 {program}" if re.fullmatch(r"[\w.-]+", program) else "运行终端命令")


def _tool_label(name: str, params: dict, server: str = "") -> str:
    family = name.split("__")[-1]
    if family in _DATA_TOOLS:
        return _DATA_TOOLS[family]
    if not isinstance(params, dict):
        return f"调用 {name}"
    if family in ("Bash", "exec_command"):
        command = params.get("command") or params.get("cmd")
        return _note(params.get("description")) or _command_label(command if isinstance(command, str) else "")
    web_search = family.lower() in ("websearch", "web_search", "search_web", "firecrawl_search")
    if web_search or (family == "search" and (server in ("web", "browser", "firecrawl") or "__web__" in name)):
        return _web_label(params)
    if family in ("WebFetch", "web_fetch"):
        return _target("读取网页", _page(params.get("url") or ""))
    target = next((params[field] for field in ("file_path", "notebook_path", "query", "pattern", "path", "url")
                   if isinstance(params.get(field), str) and params[field]), "")
    if target.startswith(("https://", "http://")):
        target = _page(target)
    return _target(_TOOLS.get(family, f"调用 {family}"), target or _note(params.get("description")))


def activity_updates(event: dict) -> Iterator[dict]:
    if not isinstance(event, dict):
        return

    def row(label: str, state: str = "done", key: str = "", *, operation: bool = False) -> dict:
        result = {"label": _display(label), "state": state, "key": key, "time": event.get("wb_at")}
        if operation:
            result["operation"] = True
        return result

    kind = event.get("type", "")
    plan = event_plan("claude" if kind == "assistant" else "codex", event)
    if plan:
        current = next((step for step in plan if step["state"] == "running"), None)
        label = "更新执行计划"
        if current and current["active_form"]:
            label += f" · {current['active_form']}"
        yield {**row(label), "plan": [{"label": _display(step["label"]), "state": step["state"]} for step in plan]}

    if kind == "wb":
        action = event.get("subtype")
        if action == "start":
            yield row("开始执行任务", key="start")
        elif action == "approval":
            approval_id = event.get("id")
            kind = event.get("kind", "command")
            details = {field: _display(event[field]) for field in ("command", "tool", "cwd", "reason", "grant_root")
                       if isinstance(event.get(field), str) and event[field]}
            if kind == "file":
                operation = _target("修改文件", details.get("command", ""))
            elif kind == "tool":
                operation = _target("使用工具", details.get("tool", ""))
            else:
                operation = _command_label(details.get("command", ""))
            update = row(_target("等待审批", operation), "running", f"approval:{approval_id}")
            if isinstance(approval_id, str) and approval_id:
                update["approval"] = {"id": approval_id, "kind": kind, **details}
            yield update
        elif action == "approval_done":
            decision = event.get("decision")
            if decision in ("accept", "acceptForSession"):
                label, state = "操作已批准", "done"
            elif decision == "expired":
                label, state = "审批已失效", "interrupted"
            else:
                label, state = "操作已拒绝", "failed"
            yield row(label, state, f"approval:{event.get('id')}")
        elif action == "remote_step":
            step = event.get("step")
            if step in _REMOTE_ACTIONS and event.get("state") in ("running", "done", "failed", "interrupted"):
                yield row(_REMOTE_ACTIONS[step], event["state"], f"remote:{step}", operation=True)
        elif action == "mcp_status":
            if state := _DATA_STATUS.get(event.get("status")):
                label = "数据源需要认证" if event.get("status") == "authenticationRequired" else "连接数据源"
                yield row(label, state, f"mcp:{event.get('source', '')}")
        elif action == "end":
            label, state = {
                "success": ("本轮执行完成", "done"), "error": ("本轮执行失败", "failed"),
                "cancelled": ("本轮已停止", "interrupted"), "interrupted": ("本轮已中断", "interrupted"),
            }.get(event.get("status"), ("", ""))
            if label:
                yield row(label, state, "end")
        elif action == "warning":
            yield row(_note(event.get("text")) or "引擎发出警告", "warning")
        elif action == "error":
            yield row(_note(event.get("text")) or "工作台执行出错", "failed")
        return

    if kind in ("item.started", "item.updated", "item.completed"):
        item = event.get("item") or {}
        item_type = item.get("type")
        done = kind == "item.completed"
        key = f"item:{item['id']}" if item.get("id") else ""
        if item_type == "reasoning":
            note = _note(item.get("summary")) or _note(item.get("text")) or _note(item.get("content"))
            if note:
                yield row(_target("思考", note), "done" if done else "running", key)
            elif key and not done:
                yield row("正在整理思路", "running", key)
            elif key:
                yield row("", "done", key)
            return
        if item_type in ("agent_message", "todo_list"):
            if done and item.get("phase") == "commentary" and (note := _note(item.get("text"))):
                yield row(_target("进展", note), key=key)
            return
        raw = item.get("raw") or item
        status = item.get("status") or raw.get("status")
        state = "done" if done else "running"
        if (status == "failed" or item.get("exit_code") not in (None, 0)
                or item.get("error") or item_type == "error" or item.get("success") is False):
            state = "failed"
        elif status in ("interrupted", "cancelled") or raw.get("kind") == "interrupted":
            state = "interrupted"
        if label := _codex_label(item):
            yield row(label, state, key, operation=item_type in (
                "command_execution", "file_change", "mcp_tool_call", "web_search",
            ))
        return

    if kind == "system" and event.get("subtype") == "init":
        for source in event.get("mcp_servers") or []:
            if isinstance(source, dict) and (state := _DATA_STATUS.get(source.get("status"))):
                label = "数据源需要认证" if source["status"] == "needs-auth" else "连接数据源"
                yield row(label, state, f"mcp:{source.get('name', '')}")
    elif kind in ("assistant", "user"):
        blocks = (event.get("message") or {}).get("content")
        for block in blocks if isinstance(blocks, list) else ():
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block.get("name") or "工具"
                if name == "TodoWrite":
                    continue
                yield row(_tool_label(name, block.get("input") or {}), "running",
                          f"tool:{block['id']}" if block.get("id") else "", operation=True)
            elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                yield row("", "failed" if block.get("is_error") else "done", f"tool:{block['tool_use_id']}")
            elif block.get("type") == "thinking" and not event.get("parent_tool_use_id"):
                if note := _note(block.get("thinking")):
                    yield row(_target("思考", note))
    elif kind == "error":
        yield row(_note(event.get("message")) or "引擎执行出错", "failed")
    elif kind == "turn.failed":
        yield row(_note((event.get("error") or {}).get("message")) or "引擎执行出错", "failed")
    elif kind == "result" and event.get("is_error"):
        yield row(_note(event.get("result")) or "引擎执行出错", "failed")


def _codex_label(item: dict) -> str:
    item_type = item.get("type")
    if item_type == "command_execution":
        label = _command_label(item.get("command") or "", item.get("command_actions") or ())
        code = item.get("exit_code")
        return f"{label}（退出码 {code}）" if code not in (None, 0) else label
    if item_type == "file_change":
        files = [f"{_FILE_ACTIONS.get(change.get('kind'), '修改')} {change['path']}"
                 + (f" → {change['move_path']}" if change.get("move_path") else "")
                 for change in item.get("changes") or [] if change.get("path")]
        return "；".join(files) or "修改文件"
    if item_type == "mcp_tool_call":
        server = item.get("server") or ""
        return _tool_label(item.get("tool") or server or "工具", item.get("arguments") or {}, server)
    if item_type == "web_search":
        return _web_label(item.get("action") or {}, item.get("query") or "")
    if item_type == "plan":
        return _target("拟定执行计划", _note(item.get("text")))
    if item_type == "error":
        return _note(item.get("message")) or "引擎执行出错"
    kind = item.get("item_type") or item_type
    raw = item.get("raw") or item
    if kind in ("collabAgentToolCall", "collab_tool_call", "collab_agent_tool_call"):
        return _AGENT_TOOLS.get(raw.get("tool"), "调用子任务工具")
    if kind in ("subAgentActivity", "sub_agent_activity"):
        return _AGENT_ACTIVITY.get(raw.get("kind"), "处理子任务")
    if kind in ("functionCallOutput", "tool_result", "userMessage", "hookPrompt"):
        return ""
    return {
        "contextCompaction": "整理会话上下文", "imageView": "查看图片", "image_view": "查看图片",
        "imageGeneration": "生成图片", "image_generation": "生成图片", "sleep": "等待",
        "enteredReviewMode": "开始审查", "exitedReviewMode": "结束审查",
    }.get(kind, _target("执行引擎操作", kind or ""))
