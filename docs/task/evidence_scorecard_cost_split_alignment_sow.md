# Evidence Scorecard v0.4 Cost Split Alignment SoW

> 项目定位声明：本任务默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 `docs/project_positioning.md`。

## 1. 业务目标与边界

### 目标

把 `evidence_scorecard.py` 的 `cost_adjusted` 段从“只有 overall aggregate”补齐到
“overall + train/test split”，使其更贴近
`docs/research/_templates/route_a_phase0_evidence_template.md` §6.3 的填表需求。

### 边界

- **不**输出 verdict / pass / fail / archive
- **不**改 live path / configs / deploy
- **不**重写现有 `cost_adjusted` overall 语义
- 只做最小增量字段补齐

## 2. 当前差距

当前 `cost_adjusted` 只有：

- `realized_edge_bps`
- `fee_bps`
- `slip_bps`
- `exec_buffer_bps`
- `net_edge_bps`

且仅是全窗口 aggregate。

模板 §6.3 实际需要：

- train 列
- test 列

## 3. 模块职责与领域模型

### 受影响模块

- `aats/data_platform/replay/backtest/evidence_scorecard.py`
- `tests/unit/test_backtest_evidence_scorecard.py`

### 领域对象

- `cost_adjusted.overall`
- `cost_adjusted.train`
- `cost_adjusted.test`

其中 `train/test` 必须与 `oos` 的 split 规则一致：

- explicit `split_ts` 优先
- 否则回退到现有 time-midpoint

## 4. 输入 / 输出接口

### 输入

- 仍然只接受 `BacktestResult`
- 不新增 CLI 参数

### 输出

保持顶层 schema 不变：

- `meta`
- `oos`
- `cross_window`
- `cost_adjusted`
- `regime_slice`

保持 backward-compatible：

- 现有 `cost_adjusted` 顶层 5 个 overall 字段继续保留

新增：

- `cost_adjusted.train.{realized_edge_bps, fee_bps, slip_bps, exec_buffer_bps, net_edge_bps}`
- `cost_adjusted.test.{realized_edge_bps, fee_bps, slip_bps, exec_buffer_bps, net_edge_bps}`

## 5. 数值约束

- `train/test` 分桶必须按 decision timestamp 对 diagnostics 做切分
- 空桶统一返回零值结构
- `slip_bps` 继续沿用当前 order_type 语义：
  - `ioc` → `config.ioc_slippage_bps`
  - 其他 → `0.0`

## 6. 数据库 / Schema / 索引

- 不涉及数据库

## 7. 一致性 / 并发 / 事务

- 纯函数，无 I/O，无共享状态

## 8. 错误处理

- 无 diagnostics 时：
  - overall 保持零值
  - train/test 也必须稳定为零值
- diagnostics 时间戳异常无法解析时，延续现有“保守忽略该条”的本地 helper 语义，不引入异常抛出

## 9. 日志 / 监控 / 审计

- 无新增日志
- 单测锁定：
  - top-level schema 未变
  - overall 旧字段仍可用
  - train/test cost split 与 OOS split 一致

## 10. 测试策略

至少覆盖：

1. `cost_adjusted.train/test` 字段存在
2. explicit `split_ts` 时 diagnostics 正确落到 train/test
3. time-midpoint fallback 时 train/test 仍可生成
4. 空 diagnostics 时 overall/train/test 都为零值
5. 顶层 schema 不变，existing callers 仍能读 overall

## 11. 迁移 / 兼容

- 只增字段，不删字段
- 旧调用方仍可继续读 `cost_adjusted.realized_edge_bps` 等 aggregate 值

## 12. 配置与环境隔离

- 无配置依赖

## 13. 代码组织与依赖

- 只在现有 scorecard builder 内补最小 helper
- 避免 unrelated refactor

## 14. 部署与验收标准

### 不需要 deploy

该任务只增强 research/evidence 工具链。

### 验收标准

- `cost_adjusted` 能同时给 overall 和 train/test
- 最窄测试通过
- 全量 unit 不回归
- 不影响 live 行为
