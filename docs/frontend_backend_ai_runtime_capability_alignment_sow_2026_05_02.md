# Frontend/Backend AI Runtime Capability Alignment SOW - 2026-05-02

## Task

- task_type: runtime-reliability-fix
- input: AI 配置页展示管理员可从页面临时切换 AI operating mode，但后端 `/ai/operating-mode/select` 默认被 `AATS_ALLOW_UI_OPERATING_MODE_OVERRIDE` 治理策略拒绝。
- output: `/ai/runtime` 明确返回页面切换能力，前端按后端能力禁用按钮并展示中文治理原因。
- impact_scope: 只读 UI/API truth surface、前端展示、测试与文档。
- out_of_scope: 策略、风控、执行、AI provider 调用、schema、symbol、venue、timeframe、release/promotion/tuning、live order behavior。

## Current Behavior

后端已经把 UI 临时切换 AI operating mode 置于治理门控之后，默认关闭；前端没有读取这一能力，所以管理员仍会看到可点击语义，点击后才收到 403。

## Plan

1. 把 UI override 是否允许抽成后端共享 capability helper。
2. 在 AI runtime payload 中暴露 `ui_operating_mode_override`，让前端拿运行态真相而不是猜测。
3. AI 配置页根据该能力锁定 operating mode 按钮，并展示中文治理说明。
4. 增加后端 helper、runtime payload、前端渲染回归测试。

## Acceptance Criteria

- AC1: `/ai/runtime` 和 dashboard bundle 的 `aiRuntime` payload 包含 `ui_operating_mode_override.enabled` 与 `disabled_reason`。
- AC2: 后端默认策略为 disabled；truthy env 值才报告 enabled。
- AC3: 前端在 disabled 时禁用所有非当前 operating mode 按钮。
- AC4: 前端展示中文治理原因，且不展示 env var 名称。
- AC5: 回归测试覆盖后端 policy、runtime payload、前端渲染。

## Rollback

回滚本变更涉及的 helper、runtime payload 字段、前端渲染逻辑、测试与本文档即可。该变更不写交易状态、不改执行路径，不需要 live order 或数据库回滚。
