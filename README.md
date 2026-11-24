# AI Workbench

macOS 桌面应用：管理本机项目目录下的所有项目。在对话框里写下要做的事，每个任务由一个独立的 `claude` 或 `codex` CLI 进程执行（本机或 SSH 服务器上），实时查看过程，可继续对话或在终端接管。

## 安装与启动

```bash
claude auth login              # 首次：确保 claude CLI 已登录（工作台以你的身份调用 claude -p）
./scripts/build_app.sh --install   # 打包并安装到 /Applications/AI Workbench.app
```

之后从「启动台」或「应用程序」打开 **AI Workbench**。在左栏点击「新建项目」，为项目关联一个或多个本机或 SSH 源文件夹。已有项目目录仍会自动识别，也可以通过菜单「工作台 → 选择项目目录…」设定。

依赖：macOS 12+、Python 3.11+（仅打包时需要）。打包后的应用自带 Python 运行时，不依赖系统 Python。

### 数据位置

| 内容 | 位置 |
|---|---|
| 数据库、运行日志、图片、worktree、设置 | `~/Library/Application Support/AIWorkbench/` |
| 应用日志 | 同上 `logs/app.log` |
| SSH 密码 | macOS 钥匙串，服务名 `workbench-ssh` |

菜单「工作台 → 在访达中打开数据目录」可直接打开。删除该目录即重置。

### 从旧版（网页版）迁移

```bash
PYTHONPATH=src .venv/bin/python -m workbench.tools.import_legacy <旧的 workbench/data 目录> --projects-root ~/dzl-py
```

复制数据库、日志、图片（旧目录不动），用 `git worktree move` 移动 worktree，并改写数据库里的路径。

## 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
git config core.hooksPath .githooks        # 启用提交前密钥检查（克隆后执行一次）
.venv/bin/python -m pytest -q
WB_KEYCHAIN_SERVICE=workbench-ssh-dev .venv/bin/python -m workbench --debug --data-dir .devdata/desktop
```

改完后重新运行最后一条命令，在 macOS 模拟桌面验证。模拟桌面使用 `.devdata/desktop` 存放数据库和设置，SSH 密码使用独立的钥匙串服务。

### 打包和发布

```bash
./scripts/build_app.sh                    # 打包到 dist/AI Workbench.app
./scripts/build_app.sh --install          # 打包并替换、重新打开正式应用
./scripts/release.sh 1.2.0 --push          # 明确发布新版本时使用
```

项目规范（版本 Tag、密钥、提交格式、验证流程）见 [AGENTS.md](AGENTS.md)。

## 架构

应用由三层组成：Python 后端（业务逻辑 + 本地 HTTP API）、Web 前端（Vue 3，无构建步骤）、macOS 原生壳（pywebview / WKWebView）。

```
src/workbench/
├── core/          基础设施：settings（配置与路径）、errors（业务异常）、shell（PATH、子进程环境、git）
├── db/            SQLite：connection、schema（表结构 + 增量迁移）、repositories（全部 SQL）
├── engines/       执行引擎：base（抽象接口）、claude、codex；__init__ 为注册表
├── ssh/           SSH 基础能力：commands（纯函数拼命令）、transport（ControlMaster）、keychain
├── services/      业务逻辑（不依赖 FastAPI）：tasks、runner、connections、projects、quota、uploads、runlog
├── api/           HTTP 层：app（工厂）、schemas、routes/（按资源划分，只做参数解析）
├── desktop/       macOS 壳：main（入口、窗口、菜单）、server（后台线程跑 uvicorn）、
│                  security（每次启动的访问令牌）、bridge（给前端的原生能力）、instance（单实例锁）
├── tools/         维护工具：import_legacy
└── web/           前端
    ├── index.html     模板
    ├── css/           按区域拆分的样式
    ├── js/core/       api 客户端、图标、路径、原生桥接
    ├── js/features/   功能模块（Vue mixin）：tasks、board、logs、ssh、quota…
    ├── js/components/ 组件：图标、状态灯、模型选择器
    └── vendor/ fonts/ 第三方库与字体（随应用打包，离线可用）
packaging/         PyInstaller 配置、图标
scripts/           build_app.sh、dev_server.sh、make_icon.py
tests/             接口、引擎、SSH、运行器测试
```

依赖方向：`api / desktop → services → engines / ssh / db → core`。下层不引用上层。

### 扩展

- **新增引擎**：在 `engines/` 下实现 `Engine`（`build_cmd`、`parse_event`、`resume_command`、`fork` 等），在 `engines/__init__.py` 的 `REGISTRY` 注册。运行器、终端接管、编辑历史消息都会自动使用它；前端在 `features/models.js` 补上模型列表和权限模式说明。
- **新增接口**：在 `services/` 写逻辑，在 `api/routes/` 加模块并放进 `routes/__init__.py` 的 `ROUTERS`。
- **新增前端功能**：在 `web/js/features/` 加一个 mixin，放进 `app.js` 的 `FEATURES`；组件放 `components/` 并加入 `COMPONENTS`。
- **新增表字段**：改 `db/schema.py` 的 `SCHEMA`，并在 `MIGRATIONS` 加一行，旧库启动时自动补列。
- **新增原生能力**：在 `desktop/bridge.py` 加公开方法，前端用 `core/native.js` 的 `call()` 调用（浏览器调试时需要降级）。

### 安全

本地 API 只监听 `127.0.0.1` 的随机端口，并要求每次启动随机生成的会话令牌（SameSite=Strict 的 HttpOnly Cookie），本机其他程序和浏览器网页无法调用它。

## 功能

- **界面**：中性灰配色（跟随系统明暗）。左侧是任务列表，按 进行中 / 待验收 / 待办 / 最近 分组（分组可折叠），顶部 ⌘K 切换项目、可搜索任务，底部可展开额度。会话里每一轮的工具调用和中间输出折叠成「已工作 12s · N 个操作」，执行中自动展开，只把最终回复直接展示。
- **项目列表与源文件夹**：新建项目时可以浏览本机或 SSH 目录，也可以粘贴路径直达。选择源文件夹后会自动填入空白项目名。SSH 连接可以在目录选择器中添加并复用。看板顶部可以切换新任务的默认源文件夹，也可以更改或移除关联。源文件保留在原位置，已配置的项目目录下的子目录仍会显示。
- **任务看板**：待办 → 执行中 → 待验收 → 已完成 / 失败。新建任务只有一个输入框，写的内容原样作为提示词发给 AI，第一行作为标题。
- **执行**：本机任务在常驻进程里执行，后续消息不再冷启动：Claude 每个会话一个 `claude --input-format stream-json --output-format stream-json --permission-prompt-tool stdio` 进程（`engines/claude_server.py`，与官方 Agent SDK 同一套协议），Codex 见下文「Codex 引擎」。远程任务和常驻进程启动失败时按每条消息执行 `claude -p` / `codex exec`。日志存于数据目录的 `runs/run-N.jsonl`，界面实时展示文本、工具调用与结果，回复逐字显示。
- **本地文件**：AI 回复里引用的本地图片 / SVG 直接显示；文件链接用系统默认应用打开（如「预览」）。只允许读任务工作目录和项目目录内的文件。
- **继续对话**：任务完成后在底部输入框补充要求，在同一会话里接着干（常驻进程直接续接；远程任务以 `--resume <session_id>` / `codex exec resume` 续接）。
- **完整展示**：对话里显示 CLI 的全部输出：思考过程（可折叠，逐字出现）、回复、工具调用的完整参数与输出、Codex 的文件 diff 与计划 / 待办更新、警告和其他条目，长内容不截断。Codex 的思考摘要取决于模型与 `model_reasoning_summary` 设置，为 none 时只有加密内容，界面上会注明。
- **审批**：本机任务里需要审批的命令、文件修改和工具调用会在对话里显示审批卡片，可「批准」「本会话都批准」或「拒绝」；这一轮结束时还没处理的按拒绝处理。远程任务没有审批，按权限模式直接放行或拒绝。
- **看板拖拽**：把卡片拖到另一列即改为该列状态；拖到「执行中」即开始执行（只限待办 / 失败 / 已取消的任务）。
- **图片附件**：输入框旁的图片按钮、粘贴或直接拖入都可添加 png / jpg / gif / webp（≤20MB）。Codex 用 `-i` 传图；Claude 把图片路径写进提示词并 `--add-dir` 让它用 Read 查看。
- **删除任务**：卡片悬停时右上角、列表每行末尾、任务详情顶部都可删除；对话、运行日志和附带的图片一并删除。
- **消息复制 / 编辑**：鼠标移到消息上可复制；编辑已发送的消息会回到该消息之前，用新内容重新发送。Claude 用 `--resume-session-at … --fork-session` 分叉，Codex 用 app-server `thread/fork`，原会话保留不动；编辑第一条则从头开新会话。
- **中途切换模型 / 思考等级**：任务底部可随时改模型和思考等级，下一轮起生效、上下文保留。
- **共享 SSH 连接**：所有项目共用一份远程连接列表，可在看板的「SSH」窗口集中管理，也可在源文件夹选择器里点连接旁的铅笔修改。连接支持主机 / 用户 / 端口 / 密钥文件 / 跳板机或 SOCKS5 代理 / 远程目录 / 说明，主机可填 `~/.ssh/config` 里的别名。可复制 ssh 命令、在「终端」中打开、测试连通性。密码保存在 macOS 钥匙串，连接时 ssh 经 `SSH_ASKPASS` 读取。勾选「告诉本机任务的 AI」后，从看板添加的连接会告知当时项目中的 AI，从目录选择器添加的连接会告知关联了该连接目录的项目中的 AI。
- **在 SSH 服务器上干活**：新建任务时可把「执行位置」改成任一共享 SSH 连接。任务通过 ssh 在关联的远程源文件夹或连接的默认远程目录里运行**服务器上的** `claude -p` / `codex exec`，过程实时回传，继续对话、换模型、停止、终端接管都照常可用。
  - 用 OpenSSH 持久连接（ControlMaster），「连接」后远程任务、图片上传、额度查询都复用它；「断开」会先停止正在这台服务器上执行的任务再关闭连接。
  - 远程命令经登录 + 交互 shell 执行，以加载 nvm 等 PATH；仍找不到时在连接里填服务器上的 CLI 路径。
  - 以 root 登录时 Claude CLI 不允许「全部放行」，可在连接里勾选「允许以 root 全部放行」（以 `IS_SANDBOX=1` 启动）。
  - 不支持：worktree 隔离、预览服务器上的文件、编辑远程 Codex 任务的历史消息。
- **终端接管**：详情里的「接管会话」命令可复制到终端，用交互式 CLI 接着干。
- **worktree 隔离**（git 项目可选）：在数据目录 `worktrees/<项目>/task-N` 中以分支 `wb/task-N` 执行，同一项目可多任务并行；删除任务时移除 worktree、保留分支。
- **思考等级**：Claude 为 低 / 中 / 高 / 超高 / 最高（`--effort`）；Codex 为 轻度 / 中 / 高 / 极高 / Ultra（`-c model_reasoning_effort=…`）。
- **权限模式**：`acceptEdits`（默认，自动改文件）、`bypassPermissions`（全放行，建议配合 worktree）、`plan`（只出方案）、`default`。
- **退出保护**：有任务在执行时关闭窗口会先确认，退出会停止这些任务。

## Codex 引擎

输入框里把引擎切到 **Codex** 即可（需先 `codex login`）。模型列表读自 `~/.codex/models_cache.json`。

- **本机任务**在一个常驻的 `codex app-server` 里执行（`engines/codex_server.py`，与 t3code 的做法一致）：会话在第一条消息时 `thread/start`，一次带上工作目录、模型、沙箱和审批策略；每条消息是一次 `turn/start`，停止用 `turn/interrupt`，回复逐字显示。所有任务空闲 15 分钟后进程退出，下次发消息时再启动；应用退出时一并关闭。
- **权限模式**映射为沙箱 + 审批策略：自动改文件 → `workspace-write` + `on-request`；只读 → `read-only` + `on-request`；全部放行 → `danger-full-access` + `never`。需要审批时 Codex 发来 `requestApproval`，对话里显示审批卡片，回复 `accept` / `acceptForSession` / `decline`。
- **预热**：与 CLI 打开时就建好会话一样，新建任务的输入框一就绪（`POST /api/tasks/prewarm`）就按发送时的目录、模型、思考等级、权限预建会话（Codex `thread/start`，Claude 预启动进程并完成 `initialize`），被新任务用掉后自动补建；打开已有任务时（`POST /api/tasks/{id}/prewarm`）提前恢复会话。设置不一致就不用预建的会话。远程任务与 worktree 新任务不预热。桌面应用监听由数据目录算出的固定端口，重启后页面设置（项目、引擎、思考等级等）保留，打开应用时就能按上次的设置预热；启动时就拉起 `codex app-server`；输入框同时为两个引擎预热当前项目（另一个引擎按切换过去后的设置）；窗口重新获得焦点时补一次。预热过程记在应用日志 `logs/app.log`（`workbench.prewarm`）里。
- **远程任务**，以及常驻进程启动失败时，按每条消息启动一次 `codex exec --json`（带 `-c analytics.enabled=false`，回复完立即退出）。

## 额度

侧边栏底部显示 Codex 额度（经 `codex app-server` 的 `account/rateLimits/read` 实时读取，缓存 2 分钟；失败时回退到 `~/.codex/sessions` 里最近的快照）。执行过远程 Claude 任务的 SSH 服务器各显示一张 Claude 额度卡片（在服务器上读取 OAuth 令牌调用 `/api/oauth/usage`，令牌不传回本机）。

## 配置（环境变量，可选）

| 变量 | 默认 | 说明 |
|---|---|---|
| `WB_DATA_DIR` | `~/Library/Application Support/AIWorkbench` | 数据目录 |
| `WB_PROJECTS_ROOT` | 应用内设置 | 项目目录（设置后应用内不能修改） |
| `WB_MAX_CONCURRENT` | 4 | 同时运行的 CLI 进程上限，超出排队 |
| `CLAUDE_BIN` / `CODEX_BIN` | 自动查找（登录 shell 的 PATH、nvm、Homebrew） | claude / codex 可执行文件 |

从「访达」启动的应用拿不到终端里的 PATH，工作台启动时会向你的登录 shell 读取一次 PATH，所以 nvm / Homebrew 安装的 CLI 都能找到。
