# 个人工作台

管理 `dzl-py/` 下的所有项目：在对话框里写下要做的事，每个任务由一个独立的 `claude` 或 `codex` CLI 进程执行（输入框里可选引擎），实时查看过程，可继续对话或在终端接管。

## 启动

```bash
claude auth login      # 首次：确保 claude CLI 已登录（工作台以你的身份调用 claude -p）
./start.sh             # 打开 http://127.0.0.1:8765
```

依赖：Python 3.10+、`fastapi`、`uvicorn`（`pip install -r requirements.txt`）。

## 功能

- **界面**：中性灰配色（跟随系统明暗）。左侧是任务列表，按 进行中 / 待验收 / 待办 / 最近 分组（分组可折叠），顶部 ⌘K 切换项目、可搜索任务，底部可展开额度；手机上侧边栏变为抽屉。会话里每一轮的工具调用和中间输出折叠成「已工作 12s · N 个操作」，执行中自动展开，只把最终回复直接展示。
- **项目列表**：自动扫描上级目录（`dzl-py/`）的所有子目录，显示 git 分支、未提交改动、是否有 CLAUDE.md。
- **任务看板**：待办 → 执行中 → 待验收 → 已完成 / 失败。新建任务只有一个输入框，写的内容原样作为提示词发给 claude，第一行作为标题。
- **执行**：`claude -p <prompt> --output-format stream-json --permission-mode <mode>`，工作目录为项目目录，日志存于 `data/runs/run-N.jsonl`，页面实时展示文本、工具调用和结果。
- **本地文件预览**：AI 回复里引用的本地图片 / SVG / 文件链接（绝对路径、`file://` 或相对任务目录的路径）经 `/api/tasks/<id>/file` 提供，可直接显示和下载；只允许读任务工作目录和项目目录内的文件。
- **继续对话**：任务完成后在底部输入框补充要求，会以 `--resume <session_id>` 继续同一会话。
- **看板拖拽**：把卡片拖到另一列即改为该列状态；拖到「执行中」即开始执行（只限待办 / 失败 / 已取消的任务），执行中的卡片不能拖。
- **图片附件**：输入框旁的图片按钮、粘贴或直接拖入都可添加 png / jpg / gif / webp（≤20MB），存于 `data/uploads/`。Codex 用 `-i` 传图；Claude 把图片路径写进提示词并 `--add-dir data/uploads` 让它用 Read 查看。
- **删除任务**：卡片悬停时右上角、列表每行末尾、任务详情顶部都可删除；对话、运行日志和附带的图片一并删除。
- **消息复制 / 编辑**：鼠标移到消息上可复制；编辑已发送的消息会回到该消息之前，用新内容重新发送（之后的记录被替换）。Claude 用 `--resume-session-at … --fork-session` 分叉，Codex 用 app-server `thread/fork`（lastTurnId），原会话保留不动；编辑第一条则从头开新会话。
- **中途切换模型 / 思考等级**：任务底部可随时改模型和思考等级，随下一条消息一起提交，下一轮起生效、上下文保留。每条消息悬停时显示该轮实际使用的模型和思考等级。
- **项目 SSH 连接**：项目页右上角「SSH 连接」，每个项目维护自己的远程连接（主机 / 用户 / 端口 / 密钥文件 / 跳板机或 SOCKS5 代理 / 远程目录 / 说明，主机可填 `~/.ssh/config` 里的别名）。可一键复制 ssh 命令、在 macOS「终端」中打开、测试连通性（`BatchMode`，不弹密码、不自动信任新指纹）。支持「用户名 + 密码」登录：密码保存在 macOS 钥匙串（服务名 `workbench-ssh`），数据库里没有；连接时 ssh 经 `SSH_ASKPASS`（`data/bin/wb-askpass`）从钥匙串读取，所以复制的命令、提示词和日志里都没有明文密码，AI 用给出的命令前缀即可登录。SOCKS5 代理通过系统自带的 `nc -X 5 -x host:port` 作为 ProxyCommand 实现（目标主机名由代理解析；不支持带用户名密码的 SOCKS5）。勾选「告诉 AI」的连接会在执行任务时告知 AI：Claude 走 `--append-system-prompt`，Codex 写在新会话提示词开头。
- **在 SSH 服务器上干活**：新建任务时把输入框里的「执行位置」从「本机」改成项目的某个 SSH 连接，任务就会 ssh 到服务器、在连接的「远程目录」里（留空 = home）运行**服务器上的** `claude -p` / `codex exec`，过程实时回传，看板、继续对话、换模型 / 思考等级、停止、终端接管都照常可用。前提：服务器上已安装并登录 claude / codex；SSH 连接面板的「测试」会顺带检查服务器上有没有这两个 CLI。
  - **连接 / 断开**：工作台用 OpenSSH 的持久连接（ControlMaster，socket 在 `/tmp/wb-ssh-<uid>/`）。在 SSH 连接面板点「连接」建立一条连接，之后远程任务、图片上传、额度查询都复用它（不再每次重新登录）；远程任务发送时若未连接会自动连接。「断开」会先停止正在这台服务器上执行的任务（连同服务器上的 CLI 进程），再关闭连接。连接状态在面板列表、输入框「执行位置」旁和远程任务标题下显示，三处都可以断开。修改连接参数或删除连接时旧连接自动断开；有任务在执行时不能删除连接。
  - 远程命令经登录 + 交互 shell（`$SHELL -lic`）执行，以加载 `.bashrc` / `.zshrc` 里的 nvm 等 PATH；仍找不到时在连接里填「服务器上的 claude / codex 路径」。
  - 附图会先传到服务器的 `~/.workbench/uploads/`；停止任务时通过 `~/.workbench/runs/<run>.pid` 结束服务器上的进程。
  - 以 root 登录时 Claude CLI 不允许「全部放行」（`--dangerously-skip-permissions`）。工作台会在发送前提示；确需如此，在该 SSH 连接里勾选「允许以 root 全部放行」，远程 claude 将以 `IS_SANDBOX=1` 启动（AI 拥有服务器完全 root 权限）。Codex 无此限制。
  - 不支持：worktree 隔离、预览服务器上的文件、编辑远程 Codex 任务的历史消息。
- **终端接管**：详情里的「接管会话」命令可复制到终端，用交互式 claude 接着干。
- **worktree 隔离**（git 项目可选）：在 `data/worktrees/<项目>/task-N` 中以分支 `wb/task-N` 执行，可同一项目多任务并行而不污染主工作区；删除任务时移除 worktree、保留分支。
- **思考等级**：输入框里可选。Claude 为 低 / 中 / 高 / 超高 / 最高，传 `--effort`；Codex 按官方客户端命名 轻度 / 中 / 高 / 极高 / Ultra（随模型支持情况变化；「默认」显示 `~/.codex/config.toml` 里配置的等级），传 `-c model_reasoning_effort=…`。「默认思考」沿用 CLI 自身配置。任务详情底部可在继续对话前修改。
- **权限模式**：`acceptEdits`（默认，自动改文件）、`bypassPermissions`（全放行，建议配合 worktree）、`plan`（只出方案）、`default`。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `WB_PORT` | 8765 | 端口 |
| `WB_PROJECTS_ROOT` | 本目录的上级 | 项目根目录 |
| `WB_MAX_CONCURRENT` | 4 | 同时运行的 claude 进程上限，超出排队 |
| `CLAUDE_BIN` / `CODEX_BIN` | 自动查找（PATH 或 nvm） | claude / codex 可执行文件 |

数据都在 `data/`（SQLite + 日志），删掉即重置。

## Codex 引擎

输入框里把引擎切到 **Codex** 即用 `codex exec --json` 执行（需先 `codex login`）。权限模式映射为 codex 沙箱：自动改文件 → `workspace-write`，只出方案/只读 → `read-only`，全部放行 → `--dangerously-bypass-approvals-and-sandbox`。继续对话走 `codex exec resume <session>`，终端接管用 `codex resume <session>`。模型列表读自 `~/.codex/models_cache.json`。

## 额度

侧边栏底部显示 Codex 额度：套餐、各时间窗口剩余百分比与重置时间、积分余额、可用的免费重置次数。数据通过 `codex app-server` 的 `account/rateLimits/read` 实时读取（缓存 2 分钟，每 5 分钟及每个 Codex 任务结束后自动刷新）；读取失败时回退到 `~/.codex/sessions` 里最近一次会话记录的快照。

**远程 Claude 额度**：执行过远程 Claude 任务的 SSH 服务器，各显示一张「Claude · 连接名」卡片：套餐、5 小时 / 每周（及按模型的每周）剩余百分比和重置时间。工作台经 SSH 在服务器上读取 `~/.claude/.credentials.json` 里的 OAuth 令牌，并在服务器上调用 Claude Code `/usage` 所用的 `api.anthropic.com/api/oauth/usage`；令牌经 stdin 交给 curl，不会传回本机，只回传额度数字（需要服务器上有 curl）。同一 user@host:port 只查一次，缓存 2 分钟，远程 Claude 任务结束后自动刷新。用 API Key 计费的服务器没有额度可显示。本机 Claude Code 额度暂未接入。
