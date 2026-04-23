# Evidence Scorecard v0.5 Cost Sensitivity SoW

> 项目定位声明：本任务默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 `docs/project_positioning.md`。

## 1. 业务目标与边界

### 目标

把 `evidence_scorecard.py` 的 `cost_adjusted` 段再补齐一层，使其能直接回答
Route A 模板 §6.3 里那句关键问题：

- `fee 上调 20% 后，net edge 是否仍 > 0?`
- `slip +0.5 bps 后，net edge 是否仍 > 0?`

### 边界

- **不**输出 verdict / pass / fail / archive
- **不**改 live path / configs / deploy
- **不**重写现有 `cost_adjusted` 的 overall / train / test 语义
- 只补最小 sensitivity 数值层

## 2. 当前差距

当前 `cost_adjusted` 已有：

- overall aggregate 5 字段
- train/test 5 字段

但还没有 sensitivity 压力测试结果，因此模板 §6.3 仍要人工手算。

## 3. 模块职责与领域模型

### 受影响模块

- `aats/data_platform/replay/backtest/evidence_scorecard.py`
- `tests/unit/test_backtest_evidence_scorecard.py`

### 领域对象

- `cost_adjusted.sensitivity.overall`
- `cost_adjusted.sensitivity.train`
- `cost_adjusted.sensitivity.test`

每个 bucket 输出：

- `net_edge_fee_up_20pct_bps`
- `net_edge_slip_plus_0_5bps_bps`

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

在 `cost_adjusted` 内新增：

```json
"sensitivity": {
  "overall": {
    "net_edge_fee_up_20pct_bps": ...,
    "net_edge_slip_plus_0_5bps_bps": ...
  },
  "train": { ... },
  "test": { ... }
}
```

并保持现有：

- overall 5 个 flat 字段
- `train` / `test` 5 字段

## 5. 数值约束

Sensitivity 必须基于已存在 bucket 数值推导，不新增新的经验假设：

- `fee_up_20pct`:
  - `net_edge_fee_up_20pct_bps = realized_edge_bps - (fee_bps * 1.2) - slip_bps - exec_buffer_bps`
- `slip_plus_0_5bps`:
  - `net_edge_slip_plus_0_5bps_bps = realized_edge_bps - fee_bps - (slip_bps + 0.5) - exec_buffer_bps`

空 bucket 统一返回 `0.0`。

## 6. 数据库 / Schema / 索引

- 不涉及数据库

## 7. 一致性 / 并发 / 事务

- 纯函数，无 I/O，无共享状态

## 8. 错误处理

- empty / degenerate buckets 不抛异常
- sensitivity 字段在零值 bucket 下也必须稳定存在

## 9. 日志 / 监控 / 审计

- 无新增日志
- 单测锁定：
  - top-level schema 未变
  - sensitivity 结构稳定
  - 公式计算正确

## 10. 测试策略

至少覆盖：

1. `cost_adjusted.sensitivity.overall/train/test` 结构存在
2. `fee_up_20pct` 公式正确
3. `slip_plus_0_5bps` 公式正确
4. empty diagnostics 时 sensitivity 全零
5. 顶层 schema 不变

## 11. 迁移 / 兼容

- 只增字段，不删字段
- 旧调用方继续可读 overall / train / test 原字段

## 12. 配置与环境隔离

- 无配置依赖

## 13. 代码组织与依赖

- 只在现有 scorecard builder 内补最小 helper
- 避免 unrelated refactor

## 14. 部署与验收标准

### 不需要 deploy

该任务只增强 research/evidence 工具链。

### 验收标准

- `cost_adjusted.sensitivity` 可直接服务模板 §6.3 的 sensitivity 问答
- 最窄测试通过
- 全量 unit 不回归
- 不影响 live 行为
