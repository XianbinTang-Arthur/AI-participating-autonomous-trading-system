# RDP Round Snapshot Consumer Alignment

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 业务目标与边界
- 目标：让 `observation_window` 和 `rollback_policy` 统一基于标准化 round snapshot 读取 Phase3/Phase4 证据，不再各自回扫 research round JSON。
- 边界：
  - 不改 Phase3/Phase4 产物格式。
  - 不改 rollback / observation 的阈值。
  - 只修复“证据读取口径”和“旧 round 兼容性”。

## 当前问题
- `observation_window` 的 DB-first 路径只要看到最新 round 存在就直接返回 `ok`，没有确认目标 combo 是否真的有 Phase3/Phase4 summary。
- `rollback_policy` 虽然优先读 round snapshot，但 snapshot 缺失时仍会直接回扫 `artifacts/research/*_rounds`，并在 combo 缺失时错误退回顶层全局 summary。
- `snapshot_db` 对 Phase3/Phase4 的文件 fallback 仍要求 `round_manifest.json` 存在，导致旧 round 无法被标准化消费。

## 方案
1. `snapshot_db`
   - 对 Phase3/Phase4 的 file fallback 补最小 manifest，允许旧 round 在没有 `round_manifest.json` 时仍能构建标准 snapshot。
2. `observation_window`
   - 只从标准 snapshot 里读取 combo summary。
   - 有 snapshot 但缺 combo summary 时返回 `warn`，不再误判为 `ok`。
3. `rollback_policy`
   - 只从标准 snapshot 里读取 combo summary。
   - 移除对 `artifacts/research/attribution_rounds` / `execution_rounds` 的手工回扫和顶层 summary fallback。

## 输入/输出契约
- 输入不变：
  - `run_observation(project_root, release_id, family, timeframe, ...)`
  - `evaluate_rollback_recommendation(project_root, release_id, family, timeframe, ...)`
- 输出变化：
  - combo 缺证据时，observation 返回 `warn`
  - combo 缺证据时，rollback trigger 返回 `fired=False` 且 detail 明确指出缺 combo summary

## 兼容性
- 仍保留 `load_latest_research_round_snapshot()` 的 file fallback。
- 旧 round 即使没有 manifest，只要目录命名规范、combo summary 文件存在，也能被标准化读取。

## 测试策略
- 单测覆盖：
  - manifestless Phase3/Phase4 round 仍能被 observation/rollback 读取
  - combo 缺失时 observation 返回 `warn`
  - combo 缺失时 rollback 不再误用全局 summary

## 验收标准
- `observation_window` 不再把“只有最新 round、没有 combo summary”的情况当成 `ok`
- `rollback_policy` 不再从顶层 aggregate summary 误推导 combo rollback
- research 侧后续消费者不再重复手工回扫 Phase3/Phase4 round JSON
