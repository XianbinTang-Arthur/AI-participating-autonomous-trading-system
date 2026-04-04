# Task 112: Independent 实盘最小安全配置修正

## 1. 业务目标与边界

### 1.1 目标

把当前 `derivatives_live` 托管配置收敛成“只为 `independent` 实盘准备”的最小安全状态，避免在真实合约交易前继续出现以下两类配置冲突：

- 运行域仍是 `net`，导致 `independent` 根本不会进入 hedge 双书路径
- 自动家族选择或 `smart_arbitrage` 抢占 `directional`，导致你以为主模式是 `independent`，实际每轮并不稳定走到 `directional` 双书执行

### 1.2 非目标

本次不做以下事项：

- 不改 `independent` 业务逻辑、风控或恢复主链
- 不重构 `protective / opportunistic / independent` 的并行组合架构
- 不修改任何明文凭证文件
- 不扩大到 `derivatives` 模拟盘或其他非 live profile

## 2. 当前行为摘要

当前 `derivatives_live` 的配置存在两条会直接影响 `independent` 实盘的冲突：

- `managed_profiles.py` 里 `derivatives_live` 的 `derivatives_position_mode` 仍是 `net`
- `derivatives_live.yaml` 里同时开启了 `strategy_family_auto_selection_enabled: true` 和 `smart_arbitrage_enabled: true`

而代码要求：

- `independent` 只在 `derivatives + 非 cash + derivatives_position_mode == hedge` 时运行
- `independent` 不是独立策略家族，而是 `directional` 内部的 hedge overlay mode
- 自动家族选择会优先考虑 `smart_arbitrage`，并在合约 allocator 中压掉 `directional`

## 3. 模块责任与输入输出

### 3.1 `aats/bootstrap/managed_profiles.py`

- 负责托管 profile 的运行时默认值
- 本次只修改 `derivatives_live.runtime_defaults.derivatives_position_mode`

### 3.2 `configs/strategy_profiles/derivatives_live.yaml`

- 负责 live 交易线的策略调参
- 本次只修改：
  - `strategy_family_auto_selection_enabled`
  - `smart_arbitrage_enabled`
  - 相关中文说明

### 3.3 测试

- `tests/unit/test_env_profiles.py`
  - 校验托管 `derivatives_live` 配置加载后具备 `hedge + directional fixed + smart_arbitrage off`
- `tests/integration/test_strategy_runtime_integration.py`
  - 校验按托管 `derivatives_live` 配置构建的最小 runtime，`independent` 作为主模式时不会被 `smart_arbitrage` 抢占

## 4. 一致性、幂等与生命周期

- 不引入新的数据库结构
- 不改变下单接口
- 不改变 event topic
- 改动是纯配置/默认值修正，重复加载幂等
- 启动顺序保持不变，只是让 live 配置与 `independent` 的实际运行前提对齐

## 5. 安全与环境隔离

- 不读取、不写入任何新的 live 凭证
- 不把 `.env.derivatives.live` 作为代码默认输入去修改
- 仅通过托管 profile 默认值和 live 策略配置修正行为

## 6. 测试策略

- lint：只跑本次涉及文件
- unit：`test_env_profiles.py`
- integration：`test_strategy_runtime_integration.py` 中最窄的 independent live-safe 场景

## 7. 回滚与兼容

- 如需回滚，只需恢复：
  - `managed_profiles.py` 中 `derivatives_live.derivatives_position_mode`
  - `derivatives_live.yaml` 中 `strategy_family_auto_selection_enabled`
  - `derivatives_live.yaml` 中 `smart_arbitrage_enabled`
- 对公共 API 和存储兼容，无迁移脚本

## 8. 验收标准

- `derivatives_live` 加载后，`derivatives_position_mode == hedge`
- `derivatives_live` 加载后，`strategy_family_auto_selection_enabled == false`
- `derivatives_live` 加载后，`smart_arbitrage_enabled == false`
- 最窄 runtime 集成测试证明 `independent` 作为主模式进入 `directional` 双书路径
