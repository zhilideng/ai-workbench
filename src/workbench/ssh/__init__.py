"""SSH 远程连接：

- keychain   —— 密码存取（macOS 钥匙串）与 SSH_ASKPASS 脚本
- commands   —— 纯函数：拼 ssh 参数、远程 shell 命令（无副作用，便于测试）
- transport  —— 持久连接（ControlMaster）、远程执行、上传、结束远程进程
- connections —— 按项目管理连接：校验、增删改查、测试、给 AI 的说明
"""
