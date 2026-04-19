# Task 208 - auto_parallel `automatic_enabled` 对齐与腿级 note 规范化

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 背景

本轮针对 auto-parallel 剩余的几处语义债务做收口：

1. `automatic_enabled` 之前只反映“配置开且 candidate 启用”，在 `candidate.state == incompatible` 或 `candidate.execution_compatible == false` 时会误报为已启用。
2. 内部字段 `state_runtime_supported` 容易和 `candidate_execution_compatible` 混淆，边界不够自解释。
3. 腿级 note 已经能区分 permission deny 和 budget-zero suppression，但整体前缀体系还不统一，不利于 operator 按腿排障。

## 本轮改动

- 将 `automatic_enabled` 收紧为与 `approved_for_execution` 对齐，避免出现“看起来已启用、实际却不可自动执行”的歧义。
- 将内部 raw / permission 模型字段重命名为 `candidate_state_runtime_supported`，更明确它来自 `candidate.state` 层。
- 在 `control_trace.permission` 中同时保留：
  - `runtime_supported`
  - `state_runtime_supported`
  - `candidate_state_runtime_supported`
  以兼顾兼容和更清晰的新语义。
- 统一腿级 note 前缀：
  - `budget_control:effective_scale=...`
  - `composition:permission_denied:*`
  - `composition:budget_zero_suppressed:*`
  - `composition:approved:*`
  - `composition:protective_override:*`
- 进一步明确 `automation_state` 仅为 compatibility-only 粗粒度投影，不应用于新的主诊断逻辑。

## 验收点

- execution-incompatible / unsupported candidate 不再出现 `automatic_enabled=true`。
- budget-zero-suppressed 仍保持：
  - `approved_for_execution=true`
  - `automatic_enabled=true`
- operator / runtime / intent `control_trace` 里仍保留旧兼容键，同时新增更自解释的内部字段镜像。
- 腿级 note 可直接区分：
  - permission denied
  - budget zero suppressed
  - protective override
  - approved target execution
