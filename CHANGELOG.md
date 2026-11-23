# 更新日志

每个版本对应一个 Git Tag（`vX.Y.Z`），Tag 说明与 GitHub Release 的内容取自这里对应版本的段落。
开发时把改动写进「未发布」，发布时 `scripts/release.sh` 会把它改成版本号并生成 Tag。

## 未发布

## v1.3.0 - 2026-11-23

### 新增
- 左栏支持创建项目，可浏览本机和已配置的 SSH 目录，为项目关联多个源文件夹。
- 看板顶部可切换和管理源文件夹，新任务使用选定的目录，移除关联时保留原文件。

### 改进
- 选择源文件夹时自动填入空白项目名，可以粘贴路径直达，也会记住最近确认的目录。
- 在选择远程目录时可以直接添加 SSH 连接，保存后回到目录选择并保留项目草稿。
- SSH 目录浏览预读下一层，提前隐藏没有子目录的展开箭头，展开时先显示已预读的内容。

### 修复
- 本机目录选择器用磁盘上的实际名称显示目录，避免因输入大小写不同出现重复项。

## v1.0.2 - 2026-11-22

### 新增
- 每个版本 Tag 都附带更新说明：Tag 说明、GitHub Release 与本文件三处一致
- `scripts/release.sh` 从本文件「未发布」段落生成版本说明，内容为空时拒绝发布；`--push` 时同时创建 GitHub Release

### 文档
- AGENTS.md 增加「版本更新说明」规范

## v1.0.1 - 2026-11-22

### 新增
- 项目规范 `AGENTS.md`：模块边界、开发规则、验证流程、版本 Tag、密钥与隐私、提交格式
- 提交前密钥检查：`scripts/check_secrets.py` + `.githooks/pre-commit`，暂存内容含疑似密钥、令牌、私钥时阻止提交
- 测试 `tests/test_no_secrets.py`：扫描全部已跟踪文件
- 发布脚本 `scripts/release.sh`：改版本号、测试、密钥检查、提交、打附注 Tag、更新本地应用

### 改进
- `scripts/build_app.sh --install`：先正常退出正在运行的应用，安装后自动重新打开

## v1.0.0 - 2026-11-22

### 新增
- 改为 macOS 桌面应用 **AI Workbench.app**：原生窗口（pywebview / WKWebView），不再需要浏览器
- 本地 API 监听 127.0.0.1 随机端口，并使用每次启动随机生成的会话令牌，其他程序和网页无法调用
- 首次启动在应用内选择项目目录，菜单「工作台」提供新任务、切换项目、选择项目目录、打开数据目录
- 有任务在执行时关闭窗口会先确认
- 从「访达」启动时读取登录 shell 的 PATH，能找到 nvm / Homebrew 安装的 claude、codex
- AI 回复里的本地文件链接用系统默认应用打开
- 旧版数据迁移工具 `python -m workbench.tools.import_legacy`
- PyInstaller 打包脚本、应用图标、测试

### 重构
- 后端拆为 `core / db / engines / ssh / services / api / desktop` 分层包，claude 与 codex 实现统一的 `Engine` 接口
- 前端拆为按区域的样式文件、基础工具、功能模块（Vue mixin）和组件；Vue、marked、DOMPurify 与字体随应用打包，离线可用

### 变更
- 数据目录移到 `~/Library/Application Support/AIWorkbench/`
- 移除网页版入口（`app.py`、`static/`、`start.sh`）

## v0.1.0 - 2026-11-22

首个版本（网页版，`./start.sh` 启动后在浏览器访问 http://127.0.0.1:8765）。

### 新增
- 项目列表与任务看板：待办 → 执行中 → 待验收 → 已完成 / 失败，支持拖拽改状态
- 每个任务由独立的 `claude -p` 或 `codex exec` 进程执行，实时展示过程，可继续对话、在终端接管
- 编辑已发送的消息并从该处分叉会话；中途切换模型与思考等级
- 图片附件、本地文件预览、worktree 隔离、权限模式
- 项目级 SSH 连接（密码存 macOS 钥匙串），可在远程服务器上执行任务
- Codex 与远程 Claude 额度显示
