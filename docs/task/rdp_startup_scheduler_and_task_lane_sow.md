
> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

﻿# RDP 首启调度顺序与任务展示拆分 SOW

## 1. 业务目标与边界

- 修正 RDP 冷启动时的自动调度顺序，避免“研究流程”在控制面上看起来先于“数据刷新”。
- 修正 RDP 控制面的任务展示口径，把“当前正在执行”和“该 workflow 最新排队任务”拆开显示。
- 只调整 scheduler 与 control-summary/UI 展示，不改手动触发 API，不改 workflow 配置语义，不改 daemon 的 FIFO claim 逻辑。

## 2. 当前行为总结

- daemon 取任务是 FIFO，`data_maintenance` 实际先于 `research_cycle` 被 claim。
- 但首启后 scheduler 会很快补入整批 workflow，且 `data_maintenance` 完成后又立刻生成一条新的 `data_maintenance`。
- 控制面当前只按 workflow 取“最近一条任务”，导致“旧 research 在运行 + 新 data_maintenance 在排队”被渲染成像“研究先于数据刷新运行”。

## 3. 模块职责

- `aats/data_platform/operations/workflow_scheduler.py`
  - 负责冷启动 bootstrap 顺序控制。
- `aats/data_platform/governance/rdp_task_db.py`
  - 提供 workflow 最近任务状态查询能力，供 scheduler/control-summary 使用。
- `aats/api/rdp_control_summary.py`
  - 输出按 workflow 拆分后的 `running_task / pending_task / latest_task`。
- `aats/api/static/modules/views/rdp-control-panel.js`
  - 把执行中与排队中的任务拆开渲染，避免混成单状态卡。

## 4. 输入输出接口

- Scheduler 输入：
  - 现有 workflow schedule 配置
  - scheduler state
  - task queue 最近状态
- Scheduler 输出：
  - 冷启动阶段只允许一个 bootstrap workflow 入队
  - state 中持久化 bootstrap 阶段
- Control summary 输出：
  - 每个 workflow 返回：
    - `latest_task`
    - `running_task`
    - `pending_task`
    - 兼容旧前端的扁平字段

## 5. 状态机与生命周期

- 新增 scheduler bootstrap 顺序：
  1. `data_maintenance`
  2. `research_cycle`
  3. bootstrap 完成，恢复正常 schedule 判定
- bootstrap 只影响自动调度，不影响手动按钮触发。
- `data_maintenance` 未成功完成前，不允许自动 enqueue `research_cycle`。
- `research_cycle` 未成功完成前，不允许自动 enqueue 其它 workflow。

## 6. 一致性、并发与幂等

- 保持现有 advisory lock 与 FIFO claim 逻辑不变。
- bootstrap 阶段最多只允许一个目标 workflow 进入 active/pending。
- scheduler 重启后根据 state 和 task queue 成功记录恢复 bootstrap 阶段，不依赖进程内内存。

## 7. 错误处理

- 如果 bootstrap 目标 workflow 已经有 pending/running 任务，scheduler 只记录 skipped，不重复入队。
- 如果 bootstrap 目标 workflow 已完成，则推进到下一阶段。
- 如果 bootstrap 状态不完整，优先根据已完成任务推导当前阶段，而不是直接恢复全量 schedule。

## 8. 前端展示要求

- `数据刷新` 与 `研究流程` 各自展示两块状态：
  - 当前执行
  - 最新排队
- 不再用单一 workflow 卡同时表达运行中与排队中的两个不同任务。
- 所有新展示文案保持 UTF-8 中文。

## 9. 测试策略

- 单测：
  - scheduler 冷启动只放行 `data_maintenance`
  - `data_maintenance` 成功后才放行 `research_cycle`
  - bootstrap 完成前不放行其它 workflow
  - control-summary 正确拆分 `running_task / pending_task / latest_task`
- 前端集成：
  - RDP 面板在“研究运行中 + 数据刷新排队中”场景下同时展示两条独立状态，而不是错看成顺序反转

## 10. 部署与验收

- 不涉及 DB schema 迁移。
- 验收标准：
  - 干净环境首次自动调度时，只能看到 `data_maintenance` 自动入队
  - `data_maintenance` 成功后，才允许 `research_cycle` 自动入队
  - 控制面能同时清楚显示“研究运行中”和“数据刷新排队中”，且语义不混淆
