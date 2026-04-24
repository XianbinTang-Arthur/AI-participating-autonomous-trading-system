# Task 213 - execution truth dedicated columns（golden path P1）

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

## 业务目标与边界

- 目标：
  - 把黄金路径当前仍为 JSON-only truth 的三个执行字段升级为硬列：
    - `execution_orders.execution_style`
    - `execution_fills.fee_rate`
    - `execution_fills.exec_type`
  - 让 operator/control-plane/query/report 读路径优先消费硬列，而不是继续依赖 JSON flatten fallback。
  - 在不改变 live 策略语义的前提下，提升 post-fix 真实 fills 的审计密度。
- 边界：
  - 不做 `submit/ack/fill pre/post snapshot linkage`
  - 不做 execution science 指标或盘口研究
  - 不改 readiness gate
  - 不改策略逻辑、family、symbol、venue、AI mode

## 模块职责与领域模型

- `execution_style`
  - 表示一次 order intent / order row 的执行风格语义（如 `taker` / `bounded_limit_ioc` / `post_only`）。
  - 当前 truth 来源主要在 `OrderIntent` 和 `execution_orders.raw_payload`。
- `fee_rate`
  - 表示交易所原始成交费率字符串（OKX `feeRate`），用于事后对账和 execution truth。
- `exec_type`
  - 表示交易所原始 maker/taker 执行类型（OKX `execType`）。
- `liquidity_role`
  - 已有 dedicated column，当前任务不重做，只保持兼容。

## 输入/输出接口

- 输入：
  - `OrderIntent.execution_style`
  - `FillEvent.raw_exchange["feeRate"]`
  - `FillEvent.raw_exchange["execType"]`
- 输出：
  - `execution_orders.execution_style`
  - `execution_fills.fee_rate`
  - `execution_fills.exec_type`
  - operator/control-plane execution records 顶层同名字段

## 数据库 Schema / 表 / 索引 / 约束

- 迁移目标：
  - 给 `execution_orders` 加 `execution_style`
  - 给 `execution_fills` 加 `fee_rate`
  - 给 `execution_fills` 加 `exec_type`
- 索引：
  - 本轮不额外加索引；目标先解决 truth density，不做查询面优化扩张。
- 兼容：
  - 允许旧行为空；但若 payload 中已存在对应 truth，则 migration/backfill 后列不得仍为空。

## 事务、一致性、并发

- 新列写入必须与现有 `raw_payload` 同事务完成，避免 JSON 与硬列分叉。
- 不新增跨表事务模式；继续沿用当前 repo 的事务边界。

## 鉴权、认证、数据安全

- 不新增外部接口权限范围。
- 不读取、不显示凭证。
- 原始交易所字段仅限已白名单的 `feeRate` / `execType`，不扩大敏感面。

## 错误处理与幂等

- migration 必须幂等可重跑。
- backfill 只在列为空且 payload 中可推断时回填。
- 对历史 payload 缺字段的行保持空值，不造假。

## 状态流转与生命周期

- `execution_style` 生命周期：
  - planner / intent 生成
  - order repo create 固化到 `execution_orders.execution_style`
  - query/operator 读硬列，旧数据保留 payload fallback
- `fee_rate` / `exec_type` 生命周期：
  - OKX adapter 生成 `FillEvent.raw_exchange`
  - fill repo save 固化到 `execution_fills.fee_rate / exec_type`
  - query/operator 读硬列，旧数据保留 payload fallback

## 缓存与性能

- 不新增缓存层。
- 通过硬列降低长期 JSON 解包依赖，改善后续审计/查询稳定性。

## 日志、监控、审计

- 本轮不新增 telemetry；通过硬列本身提升审计可消费性。
- control-plane/operator 结果必须能直接看到三个字段。

## 测试策略

- unit：
  - repo/create/save 写列
  - query_service 读列优先级
  - migration/backfill 语义
- integration（最窄）：
  - PostgreSQL-backed execution/control-plane 路径验证新列真实可读

## 迁移、回滚、兼容

- 新增 root migration（按 `migrations/*.sql` 现有链）。
- 兼容旧 payload；query 继续保留 JSON fallback。
- 回滚不在本轮做 destructive drop；仅保证旧读路径仍可工作。

## 配置与环境隔离

- 无新增配置。
- 不改变 live runtime mode。

## 代码组织与依赖

- 仅允许改：
  - `migrations/`
  - `aats/storage/sqlalchemy_models.py`
  - `aats/storage/execution_order_repo_postgres.py`
  - `aats/storage/execution_fill_repo_v2_postgres.py`
  - `aats/services/operator/query_service.py`
  - 直接相关测试

## 文档与运维

- 不写新战略文档。
- 交付说明只需明确：本轮只解决 dedicated-column-gap，不解决 pre/post snapshot linkage。

## 部署与验收标准

- 验收必须同时满足：
  1. `execution_orders.execution_style`、`execution_fills.fee_rate`、`execution_fills.exec_type` 存在于 schema
  2. 新增 order/fill 行自动写入这些列
  3. 若历史 payload 含对应值，则 migration/backfill 后列不再为空
  4. operator/control-plane 读路径优先消费硬列，旧 JSON fallback 仍兼容
  5. lint、Windows unit、最窄 WSL2 integration 实际通过
