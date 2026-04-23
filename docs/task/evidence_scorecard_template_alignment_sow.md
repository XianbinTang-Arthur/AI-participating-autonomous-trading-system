# Evidence Scorecard v0.2 Template Alignment SoW

> 项目定位声明：本任务默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 `docs/project_positioning.md`。

## 1. 业务目标与边界

### 目标

把现有 backtest `scorecard.json` 的结构再补齐一层，使其更接近
`docs/research/_templates/route_a_phase0_evidence_template.md` 真正需要的人类评审输入，
减少 future candidate 出现时的手工补表工作。

### 边界

- **不**输出 verdict / go-no-go / pass / fail / archive
- **不**改 live path / configs / deploy
- **不**发明 candidate
- **不**重写整套 scorecard 语义，只补“模板确实需要、当前 scorecard 明显缺”的结构字段

## 2. 当前差距

当前 `evidence_scorecard.py` 已有：

- `oos.train/test`: `ir` / `hit_rate` / `fills`
- `cross_window[*]`: `ir` / `hit_rate` / `fills` / `max_drawdown_bps`
- `cost_adjusted`
- `regime_slice.vol.low/high`: `ir` / `fills`

但 Route A 模板实际还需要至少这些字段：

- OOS: `max_drawdown_bps`, `sample_n`
- Cross-window: `sample_n`
- Regime-slice: `sample_n`

本次任务聚焦补这些“结构性缺口”。

## 3. 模块职责与领域模型

### 受影响模块

- `aats/data_platform/replay/backtest/evidence_scorecard.py`
- `tests/unit/test_backtest_evidence_scorecard.py`

### 领域对象

- bar-level return count (`sample_n`)
- segment drawdown (`max_drawdown_bps`)
- regime bucket sample count

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

在不破坏现有 key 的前提下新增：

- `oos.train.sample_n`
- `oos.train.max_drawdown_bps`
- `oos.test.sample_n`
- `oos.test.max_drawdown_bps`
- `cross_window[*].sample_n`
- `regime_slice.vol.low.sample_n`
- `regime_slice.vol.high.sample_n`

## 5. 数据库 / Schema / 索引

- 不涉及数据库

## 6. 一致性 / 并发 / 事务

- 纯函数逻辑，无 I/O，无共享状态

## 7. 错误处理

- 空 curve / 空 diagnostics 继续返回零值结构，不抛异常
- 新增字段在空样本情况下也必须稳定返回 `0` 或等价零值

## 8. 日志 / 监控 / 审计

- 无新增日志
- 通过单测锁定结构字段和边界行为

## 9. 测试策略

至少覆盖：

1. `oos.train/test` 含 `sample_n` 和 `max_drawdown_bps`
2. `cross_window[*]` 含 `sample_n`
3. `regime_slice.vol.low/high` 含 `sample_n`
4. 空 curve 情况下新字段为零值
5. 顶层 schema 不变

## 10. 迁移 / 兼容

- 不改 CLI 参数
- 不删除既有字段
- 新字段只做增量补充，保持 backward-compatible

## 11. 配置与环境隔离

- 无环境依赖

## 12. 代码组织与依赖

- 仅在现有 scorecard builder 内补充最小 helper / 字段
- 避免 unrelated refactor

## 13. 文档与运维

- 本 SoW 即边界
- 如有必要，仅在代码 docstring 中简短说明 `sample_n` 的口径

## 14. 部署与验收标准

### 不需要 deploy

本任务只增强 research/governance 工具链。

### 验收标准

- `build_scorecard()` 输出新增字段
- 现有 scorecard 测试 + 新增测试通过
- 不影响 live 行为
