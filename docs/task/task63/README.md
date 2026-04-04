# Task63：试盘运行档位与自动停机守护

## 目标
- 为小资金前向验证提供一个可直接启用的保守配置档。
- 在现有收益归因和执行异常报表基础上，补一层自动停机守护。
- 在 operator 控制面和前端风险页中明确展示当前试盘守护状态。

## 现有相似功能
### 已有功能
- 系统已有只读 runtime profile 快照。
- 系统已有手动暂停和恢复链路。
- 系统已有执行质量、收益概览、异常报表：
  - `/reports/execution-quality`
  - `/reports/profitability-overview`
  - `/reports/execution-anomalies`

### 当前缺口
- 没有一套专门面向小资金前向验证的保守配置档。
- 没有把收益/费用/滑点异常自动转成停机动作。
- 风险页无法直接看见“试盘守护是否已触发自动暂停”。

## 本次实现
### 1. 新增试盘配置档
- 新配置文件：
  - `configs/forward_test_small_capital.yaml`
- 特点：
  - 真实市场观察
  - 本地 paper 执行
  - PostgreSQL 持久化
  - 更低仓位、更低名义敞口、更低挂单数
  - 更严格的决策频率

### 2. 新增自动停机守护
- 新服务：
  - `aats/services/governance_engine/trial_guard.py`
- 触发依据：
  - 最近 24 小时净收益超过亏损阈值
  - 连续亏损笔数超过阈值
  - 费用 / 成交额超过阈值
  - 高滑点成交比例超过阈值
  - 慢成交比例超过阈值
- 触发结果：
  - 自动调用 kill switch
  - 写入 execution error summary
  - 写入 processing failure

### 3. 接入运行时
- runtime 构建后自动装配试盘守护服务。
- 启动后台循环持续评估。
- 风险页与 operator API 可直接读取快照。

### 4. 前端展示
- 风险页新增“试盘守护”卡片。
- 展示：
  - 当前状态
  - 样本量
  - 最近 24 小时净收益
  - 连续亏损
  - 费用拖累
  - 高滑点与慢成交比例

## 新增接口
- `GET /system/trial-guard`

## 配置项
- `trial_guard_enabled`
- `trial_guard_poll_interval_seconds`
- `trial_guard_lookback_fills`
- `trial_guard_min_closed_fills`
- `trial_guard_max_daily_loss_usdt`
- `trial_guard_max_consecutive_losses`
- `trial_guard_max_fee_to_notional_ratio`
- `trial_guard_max_high_slippage_ratio`
- `trial_guard_max_slow_submit_to_fill_ratio`

## 验收标准
- 试盘配置档可通过 settings/config 正常启用。
- 守护器在样本不足时只显示预热，不误触发停机。
- 守护器在阈值 breach 时会自动暂停系统。
- `/system/trial-guard` 返回完整状态。
- 风险页可显示试盘守护状态。
