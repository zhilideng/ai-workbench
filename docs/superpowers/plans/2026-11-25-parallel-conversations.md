# 并行会话实现计划

> 分步实现接口、CLI 适配、共享工作区和原生界面。

**目标**　从一条明确要求并行的提示词发起独立会话，在侧栏查看进度，并直接在所选项目目录中逐项调整。

**架构**　增量 SQLite 迁移保存任务组和进度，选中的 CLI 负责生成计划，原运行器在共享源目录执行实际任务。Vue 用同一组服务器记录驱动侧栏和看板。

**设计稿**　[从一条提示词发起并行会话](../specs/2026-11-25-parallel-conversations-design.md)

## 接口

- `POST /api/dispatch` 接收原 `TaskIn` 字段和 `request_id`。响应包含 `task` 或 `group`，另一项为 `null`。普通发送沿用任务服务，拆分异步运行。
- `GET /api/dispatch` 返回任务组数组。组包含 `id`、`title`、`project`、`state`、`prompt`、`error`、`updated_at`。状态为 `planning`、`ready` 或 `failed`。
- `POST /api/dispatch/{id}/retry` 重试失败的拆分。`POST /api/dispatch/{id}/single` 用同一原始请求创建单会话。两者返回同样的发送响应。
- `GET /api/dispatch/{id}` 等待拆分结果，最多等 25 秒，返回该任务组的当前状态。
- `GET /api/tasks` 的任务带可空的 `parallel_group_id`、组内顺序 `parallel_position`、可空百分比 `progress`、阶段文字 `progress_label` 和尚未完成的前置任务 `waiting_for`。现有操作仍以任务 ID 为入口。

## 执行步骤

- [x] 测试普通发送与显式派发合同。实现任务组迁移、持久化、事务创建、有界调度、复用现有继续会话和失败恢复。派发测试验证三个任务重叠执行、重复发送和重试。
- [x] 测试 CLI 提示词路由、计划验证、结构化输出和百分比。使用 CLI 自己的账号和引擎参数生成计划，复用运行器并解析 Claude 与 Codex 的计划事件。
- [x] 让新组的成员直接使用所选源目录。已有独立目录的旧会话仍能续接并读取原文件。
- [x] 接入 Vue 发送流程，在 `v1.4.0` 的侧栏任务列表内增加并行组。支持折叠、搜索、组状态迁移、单卡对话及拆分失败重试。主区域沿用原版模板和样式。
- [x] 更新 README 和未发布说明。
- [x] 验证同目录并行写入、两次续接和原生侧栏。
- [x] 验证有依赖的子任务等待、禁止提前启动、失败后重试解锁，以及原生看板的自动流转。
