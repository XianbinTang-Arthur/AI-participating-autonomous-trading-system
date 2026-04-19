# Task 92: 合约仓位模式契约 Phase 0 + Phase 1 施工单

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 业务目标与边界

本次只实现 [Task 91](/D:/文件/project/AIParticipatingAutonomousTradingSystem/docs/task91_derivatives_hedge_mode_phase_breakdown.md) 的 `Phase 0 + Phase 1`：

- 新增显式配置 `derivatives_position_mode`
- 补齐 `derivatives_hedge_transition_mode`
- 补齐 `derivatives_require_exchange_pos_mode_match`
- 启动时校验交易所实际 `posMode`
- 当配置要求与交易所实际 `posMode` 不匹配时直接 fail-fast
- 在 operator/runtime 查询链路暴露“配置模式 / 交易所模式 / 是否匹配”

本次不做：

- 双腿仓位模型
- 腿级订单语义
- 腿级风控
- 腿级对账与恢复
- hedge overlay 策略

## 2. 模块职责与领域模型

- `aats/bootstrap/settings.py`
  - 承载显式配置项与基础运行时校验
- `aats/bootstrap/managed_profiles.py`
  - 托管 profile 显式带出合约仓位模式配置
- `configs/guarded_derivatives_*.yaml`
  - 手动 config profile 显式声明合约仓位模式配置
- `aats/services/execution_engine/okx_account.py`
  - 解析交易所账户配置，生成“仓位模式契约摘要”
- `aats/bootstrap/config.py`
  - 在启动首次刷新账户状态后执行 fail-fast 校验
- `aats/schemas/runtime_profiles.py`
  - runtime profile 快照暴露模式配置
- `aats/services/operator/account_queries.py`
  - `/account/state` 暴露模式契约状态
- `aats/services/operator/runtime_queries.py`
  - `/system/runtime` 暴露模式契约状态

## 3. 输入 / 输出接口

输入：

- 环境变量 / YAML / managed profile 中的：
  - `derivatives_position_mode`
  - `derivatives_hedge_transition_mode`
  - `derivatives_require_exchange_pos_mode_match`
- OKX `account config` 返回的 `posMode`

输出：

- settings 中的显式配置字段
- account status / runtime profile / account state / system runtime 中的：
  - `configured_derivatives_position_mode`
  - `required_exchange_position_mode`
  - `exchange_position_mode`
  - `exchange_position_mode_matches_configured`
  - `position_mode_contract`

## 4. 数据库 / 表 / 索引 / 约束

本次不做数据库 schema 变更。

## 5. 事务、一致性与并发

- 启动 fail-fast 在首次 `account_service.refresh(force=True)` 后立即执行
- 若模式不匹配，直接抛错终止 runtime 构建，不进入后续基线导入、恢复和自动运行
- 不引入新的异步写路径或并发状态

## 6. 授权、认证与数据安全

- 不新增写接口
- 仅通过现有 operator 只读查询暴露模式状态
- 不改变 operator 鉴权模型

## 7. 错误处理与幂等

- 账户模式缺失：返回固定 blocker，并在受约束的合约 exchange runtime 上 fail-fast
- 账户模式不匹配：返回固定 blocker，并在受约束的合约 exchange runtime 上 fail-fast
- 重复刷新 / 重复读取为纯幂等只读行为

## 8. 状态迁移与生命周期

- `configured mode` 来源于 settings
- `exchange mode` 来源于 OKX account config
- `match result` 由两者比较得到
- 启动阶段仅支持：
  - 匹配后继续启动
  - 不匹配时终止启动

## 9. 缓存与性能

- 不新增新的外部请求
- 继续复用账户快照与 `account config` 缓存
- operator/runtime 仅消费已缓存快照派生字段

## 10. 日志、监控与审计

- 不新增写审计事件
- 模式不匹配通过现有 blocker / runtime 查询可见
- 启动失败通过 `ValueError` 明确报错码暴露

## 11. 测试策略

- 单测：
  - settings 新配置及校验
  - runtime profile 暴露新字段
  - OKX account status 的模式契约摘要与 blocker
  - startup fail-fast
- 最窄集成：
  - `/account/state`
  - `/system/runtime`

## 12. 迁移、回滚与兼容性

- 默认 `derivatives_position_mode = net`
- 默认 `derivatives_hedge_transition_mode = close_then_open`
- 默认 `derivatives_require_exchange_pos_mode_match = true`
- 未开启 hedge 的现有合约 runtime 保持 `net` 语义
- 回滚方式：
  - 回退本次代码与配置字段
  - 或临时关闭 `derivatives_require_exchange_pos_mode_match`

## 13. 配置与环境隔离

- 仅合约 runtime 允许 `hedge`
- 现货 runtime 不允许把 `derivatives_position_mode` 设为 `hedge`
- 托管 `derivatives` / `derivatives_live` profile 与手动 `guarded_derivatives_*` profile 都要显式带出配置

## 14. 代码组织与依赖

- 不新增新服务层
- 尽量把模式契约判断封装在账户服务相关模块中
- 查询层只消费派生结果，不重复写判断逻辑

## 15. 文档与运维手册

- operator 应通过 `/account/state` 和 `/system/runtime` 看到：
  - 当前配置要求的仓位模式
  - 交易所实际仓位模式
  - 是否匹配
  - 是否要求强匹配

## 16. 部署与验收标准

验收通过条件：

- 合约 runtime 能显式读取 `derivatives_position_mode`
- 交易所 `posMode` 缺失或不匹配时，合约 exchange runtime 启动失败
- `/account/state` 暴露模式契约状态
- `/system/runtime` 暴露模式契约状态
- spot runtime 不暴露导数专属字段
