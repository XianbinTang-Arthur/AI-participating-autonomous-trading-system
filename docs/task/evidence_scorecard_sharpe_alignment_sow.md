# Evidence Scorecard v0.3 Sharpe / Annualized IR Alignment SoW

> 项目定位声明：本任务默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 `docs/project_positioning.md`。

## 1. 业务目标与边界

### 目标

把现有 `evidence_scorecard.py` 再向
`docs/research/_templates/route_a_phase0_evidence_template.md`
对齐一层，补齐模板真正需要但当前 scorecard 还缺的两个数值指标：

- `IR (annualized)`
- `Sharpe`

### 边界

- **不**输出 verdict / go-no-go / archive
- **不**改 live path / configs / deploy
- **不**重写整套 scorecard 语义
- 只做最小增量字段补齐，保持 backward-compatible

## 2. 当前差距

当前 scorecard：

- OOS 有 `ir`、`hit_rate`、`max_drawdown_bps`、`sample_n`
- Cross-window 有 `ir`、`hit_rate`、`max_drawdown_bps`、`sample_n`

但模板实际写的是：

- `IR (annualized)`
- `Sharpe`

所以当前人工写 proposal 时仍要手工换算 / 补充。

## 3. 模块职责与领域模型

### 受影响模块

- `aats/data_platform/replay/backtest/evidence_scorecard.py`
- `tests/unit/test_backtest_evidence_scorecard.py`

### 数值语义

- `ir_annualized`
  - 基于现有 bar-level `ir`
  - 按 segment 的时间粒度换算年化因子
- `sharpe_ratio`
  - 与 `equity_builder.py` 的口径尽量一致
  - 但按 segment 内可用样本独立计算

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

在不删除既有字段的前提下增量补：

- `oos.train.ir_annualized`
- `oos.train.sharpe_ratio`
- `oos.test.ir_annualized`
- `oos.test.sharpe_ratio`
- `cross_window[*].ir_annualized`
- `cross_window[*].sharpe_ratio`

## 5. 数值约束

- 空样本 / 单点 / 零方差 → 统一返回 `0.0`
- 年化因子必须基于**该 segment 实际时间粒度**推导，不允许硬编码某个 timeframe
- 不允许用未来数据或跨 segment 数据辅助换算

## 6. 数据库 / Schema / 索引

- 不涉及数据库

## 7. 一致性 / 并发 / 事务

- 纯函数，无 I/O，无共享状态

## 8. 错误处理

- 时间戳退化（如所有点同一毫秒）时，`ir_annualized = 0.0`
- 可用 return 样本不足时，`sharpe_ratio = 0.0`

## 9. 日志 / 监控 / 审计

- 无新增日志
- 单测锁定数值行为和零值边界

## 10. 测试策略

至少覆盖：

1. OOS / cross_window 新字段存在
2. 单调上涨样本下 `ir_annualized >= ir`
3. 零方差 / 空样本时两个新字段为 `0.0`
4. 顶层 schema 不变

## 11. 迁移 / 兼容

- 只增字段，不删字段
- 保持既有 JSON 可继续被旧调用方读取（忽略新字段）

## 12. 配置与环境隔离

- 无配置依赖

## 13. 代码组织与依赖

- 只在现有 scorecard builder 内补最小 helper
- 避免 unrelated refactor

## 14. 部署与验收标准

### 不需要 deploy

该任务只增强 research/evidence 工具链。

### 验收标准

- `build_scorecard()` 增量输出 `ir_annualized` / `sharpe_ratio`
- 最窄测试通过
- 全量 unit 不回归
- 不影响 live 行为
