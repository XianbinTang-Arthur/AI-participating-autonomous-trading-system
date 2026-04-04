# Task 89: 运行模式读取点审计

## 目标

单独做一轮“读取点审计”，把仍然默认 `spot` / `derivatives` 共用配置载荷的服务逐个扫掉，重点确认：

- 主运行链是否还会误读跨运行模式参数
- operator / profile / snapshot 摘要链是否还会把另一种运行模式的字段当成当前模式字段展示或比较
- 现货运行模式下是否还会把 `directional short`、杠杆和做空偏置字段当成真实可调参数

本次继续保持最小 diff，不重做全量 settings schema，也不改公开接口名。

## 审计结果

### 已确认主运行链没有残余共用假设

以下执行与账户链路已经按运行模式分流，不是这轮问题来源：

- `okx_rest.py`
- `okx_private_websocket.py`
- `okx_account.py`
- `runtime_scope.py`

这些读取点已经根据 `trading_product_type` 或运行时 scope 控制订阅、仓位或账户视图。

### 剩余问题集中在 operator / profile 链路

审计后确认，残余耦合主要在两类地方：

1. `strategy profile` payload、summary、diff、axes 仍默认把 `strategy_short_*` 当成所有运行模式都有效的独立参数。
2. `runtime profile` snapshot 仍默认把 `max_target_leverage`、`default_target_leverage`、`strategy_short_bias_enabled`、`strategy_dynamic_leverage_enabled` 当成通用字段返回。

这会导致：

- 现货 profile 摘要看起来像仍然有一整套独立 short 阈值
- 现货 profile diff / axes 会被并不存在的 short 参数影响
- operator 侧的 runtime/profile 快照继续向前端暴露合约专属字段

## 修复

### 1. 现货 strategy profile 归一化

- 在 `strategy_profiles.py` schema 层新增按 `product_type` 的 payload 归一化
- 现货模式下把 `strategy_short_*` 统一映射回共享 long 阈值
- `summary` / `diff` / `axes` 计算时按运行模式裁剪，现货不再把 short 侧当成独立配置域
- seed revision 也按运行模式归一化，避免现货种子继续持久化历史 short 阈值

### 2. runtime profile 输出裁剪

- `runtime_profiles.py` 只把杠杆、short bias、dynamic leverage 作为合约专属字段输出
- 现货 runtime snapshot 不再对外暴露这些字段

### 3. operator 侧摘要链路同步 product type

- `strategy_profiles.py`
- `strategy_profile_context.py`
- `strategy_profile_optimization.py`
- `strategy_profile_activation.py`

这些服务在构建 summary / axes / diff 时，统一改为透传 revision 的 `product_type`。

## 风险

- 这次清理的是“载荷解释与展示口径”，不是执行引擎参数模型重构。
- 如果仓库里还有未覆盖到的隐藏读取点继续直接消费原始 payload，它们仍可能看到完整字段；本次先把主 operator 链路和快照链路清干净。
- 现货 revision 的 short 字段现在会被归一化为共享阈值，旧数据在摘要上会显得比过去更“简化”，这是刻意修正，不是数据丢失。

## 验证

- `tests/unit/test_runtime_profiles.py`
- `tests/unit/test_strategy_profile_payload_schema.py`
- `tests/integration/test_operator_api.py`
