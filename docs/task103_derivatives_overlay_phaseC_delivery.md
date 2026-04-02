# Task 103：合约 Overlay Phase C 交付说明

## 1. 背景

`Task 100 / Phase A` 已经开放 `opportunistic`，`Task 102 / Phase B` 已经把 `independent` 双书状态机接入到决策层。
但在进入本阶段前，operator / audit / dashboard 仍然主要停留在“知道系统启用了 overlay”，还不能完整回答下面这些问题：

- 当前这条腿到底属于 `protective`、`opportunistic` 还是 `independent`？
- 当前这条腿是 `independent_long_book` 还是 `independent_short_book`？
- 为什么某一条腿现在还能扩张，或者为什么这轮被腿级试盘守护拦住？

本阶段的目标，就是把这些诊断信息正式补进 operator 审计和前端展示链。

## 2. 当前行为

完成本阶段后：

- `/audit/{decision_id}` 返回的 `hedge_mode_audit` 不再只有 position mode / leg orders / leg reconciliation
- 现在会额外返回：
  - `overlay`
  - `leg_trial_guard`
- `overlay` 会给出：
  - `effective_mode`
  - `overlay_source`
  - `state`
  - `reason_codes / blocked_reasons`
  - `long_leg_score / short_leg_score`
  - 每条腿的 `execution_mode / overlay_mode / target / delta / trigger_reason_codes`
- `leg_trial_guard` 会给出 long / short 两条腿各自的：
  - 状态
  - 样本量
  - 最近净收益
  - 胜率
  - 当前是否已经触发腿级试盘守护

前端上：

- 策略页会继续区分 `protective / opportunistic / independent`
- 当模式是 `independent` 时，会展示 independent 专属配置、long/short 双书状态和腿级原因
- 决策抽屉会新增：
  - `Overlay 审计`
  - `腿级试盘守护`

## 3. 主要改动

### 3.1 后端审计结构

在 `aats/services/operator/query_service.py`：

- 扩展 `_hedge_mode_audit_payload()`
- 新增 overlay 摘要
- 新增腿级试盘守护摘要
- 腿级订单摘要新增 `execution_mode / overlay_mode / strategy_leg_role`

这样 audit 链现在能把：

- `independent_long_book`
- `independent_short_book`
- `opportunistic_overlay`
- `protective`

这些来源明确区分开。

### 3.2 前端策略页

在 `aats/api/static/modules/views/strategy-view.js`：

- directional 配置表新增 independent 参数说明
- overlay 状态 / 详情 / 细节说明增加 independent 专属分支
- long book / short book 的目标与阻断原因不再被压扁成单条 overlay 文案

### 3.3 决策抽屉

在 `aats/api/static/modules/detail-drawers.js`：

- 新增 `Overlay 审计` 卡片
- 新增 `腿级试盘守护` 卡片
- 腿级订单审计会展示 `execution_mode`

### 3.4 前端中文术语

在 `aats/api/static/modules/terms.js`：

- 新增 independent 双书相关中文术语
- 新增 independent long/short 原因码的中文映射

## 4. 测试策略

本阶段覆盖了 3 类验证：

### 4.1 后端审计接口

`tests/integration/test_operator_api.py`

- 验证 `/audit/{decision_id}` 会返回 independent overlay 的来源、腿模式和腿级试盘守护结果

### 4.2 前端策略页

`tests/integration/test_dashboard_ui.py`

- 验证策略页能显示 independent 配置项
- 验证策略页能显示 long book / short book 状态和原因

### 4.3 前端决策抽屉

`tests/integration/test_dashboard_ui.py`

- 验证抽屉新增 `Overlay 审计`
- 验证抽屉新增 `腿级试盘守护`
- 验证抽屉能显示 independent long book 的试盘守护拦截文案

## 5. 风险与边界

- 本阶段是 `Phase C`，不是 `Phase D`。还没有进入灰度上线和样本回放手册阶段。
- 这次补的是 operator / dashboard 的诊断结构，不是收益归因系统重构。
- `leg_trial_guard` 当前是按 independent 双书的启发式规则做审计摘要，不等于一个新的独立守护服务。
- JS 文件当前没有仓库级专用 lint 工具；这轮通过 Node 集成测试验证前端行为，没有额外引入新的前端 lint 体系。

## 6. 验收结论

完成本阶段后，系统已经可以在控制面里明确回答：

- 这条腿属于哪种 overlay
- 这条腿是 long book 还是 short book
- 这条腿当前为什么还在持有
- 这条腿是不是被腿级试盘守护拦住了

这意味着 `protective / opportunistic / independent` 三条 overlay 线，已经在 operator / audit / dashboard 层完成了第一版可诊断闭环。
