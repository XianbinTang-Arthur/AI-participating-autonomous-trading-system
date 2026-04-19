# Task72-D 合约优先版第四批具体开发任务

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 1. 文档定位

这份文档承接 `Task72-C` 已完成的持仓生命周期收益归因、清算距离与保证金缓冲控制面，以及资金费纳入 `trial_guard / forward validation / trial review` 的统一口径。

当前目标继续收敛阶段 5 到阶段 7 之间最影响真钱启盘和盘中存活判断的缺口：清算距离和保证金缓冲不能只停留在展示层，必须能驱动自动阻断和停机；`guarded_live` 不能只靠人工经验启盘，必须先经过结构化自检；小资金试运行也不能只看零散接口，必须形成一张可直接给 operator 使用的运行包。

## 2. 当前批次范围

本批次包含三个高优先级任务：

1. `Task72-B8` 清算距离与保证金缓冲驱动的自动阻断与停机规则 v1
2. `Task72-B9` 合约 `guarded_live` 启盘前自检报告与 operator 预检接口 v1
3. `Task72-B10` 小资金 `guarded_live` 运行包汇总视图并接入试盘 / 风险控制面 v1

这三个任务的共同目标是把“当前还能不能继续开仓、现在能不能启盘、今天的小资金试运行值不值得继续”从分散字段升级成系统内稳定结论。

## 3. Task72-B8 清算距离与保证金缓冲驱动的自动阻断与停机规则 v1

### 3.1 目标

- 把保证金缓冲和强平距离从 operator 展示指标升级成运行时硬约束
- 在风险逼近时自动进入 `only_reduce`，在逼近硬停机阈值时自动触发 halt
- 让风控、健康检查、blocker control 和 operator 视图看到的是同一份结论

### 3.2 代码级开发子项

#### B8.1 合约运行时风险守卫

主要改动：

- 基于 exchange risk snapshot 计算当前 `initial_margin_usage_fraction`
- 基于 exchange positions 计算最近 `liquidation_gap_ratio`
- 统一输出 `status / only_reduce_required / auto_halt_required / closest_position`

当前落点：

- `aats/services/governance_engine/derivatives_live_guard.py`

验收标准：

- 没有合约仓位时允许返回 `idle` 或 `not_applicable`
- 风险逼近 `only_reduce` 阈值时不直接停机，但必须给出稳定 reason code
- 风险逼近硬停机阈值时必须生成稳定 blocker 和执行错误摘要

#### B8.2 风控与健康检查接入

主要改动：

- `risk.py` 把运行时守卫的 `only_reduce` 结果接进合约 pre-trade gate
- `health.py` 暴露 `derivatives_live_guard` 子系统组件
- `config.py` 在账户 refresh 后自动重算运行时风险守卫

当前落点：

- `aats/services/governance_engine/risk.py`
- `aats/services/governance_engine/health.py`
- `aats/bootstrap/config.py`

验收标准：

- 风险守卫进入 `only_reduce` 后，新开仓不能继续放大暴露
- 风险守卫进入 `auto_halt` 后，系统健康视图和 blocker 列表都能看到一致状态

#### B8.3 Blocker control 中文收口

主要改动：

- 为 `derivatives_margin_buffer_auto_halt`
- 为 `derivatives_liquidation_proximity_auto_halt`
- 配置明确优先级、子系统归类、建议动作和 UTF-8 中文解释

当前落点：

- `aats/services/blocker_control/priority.py`
- `aats/services/blocker_control/service.py`
- `aats/api/static/modules/terms.js`

验收标准：

- operator 无需解读内部 code，就能知道为什么自动停机、现在该做什么
- 新增文案必须保持干净 UTF-8 中文

## 4. Task72-B9 合约 guarded_live 启盘前自检报告与 operator 预检接口 v1

### 4.1 目标

- 让 `guarded_live` 启盘前检查从“依赖人工记忆”升级成“有结构化报告的预检流程”
- 明确 runtime 合同、账户可读性、恢复状态、风险缓冲、trial guard 和资金边界是否已经满足启盘条件

### 4.2 代码级开发子项

#### B9.1 预检报告生成器

主要改动：

- 汇总 runtime contract、operator safety、execution route、account readiness、recovery and blockers、risk buffer、trial guard、capital envelope
- 统一生成 `status / launch_ready / counts / operator_actions / summary`

当前落点：

- `aats/services/operator/query_service.py`

验收标准：

- 缺少账户快照、风险快照、主合约规则、鉴权或数据库条件时，必须明确失败
- `warning` 和 `fail` 要区分“可以人工确认继续看”与“不能启盘”

#### B9.2 operator 预检接口

主要改动：

- 新增 `/system/guarded-live-preflight`
- `system/runtime` 挂入 `guarded_live_preflight` 摘要

当前落点：

- `aats/services/operator/query_service.py`
- `aats/api/routes.py`

验收标准：

- operator 不需要翻多个接口，也能直接拿到启盘前结论
- 预检接口必须适合 automation / 手工巡检直接消费

## 5. Task72-B10 小资金 guarded_live 运行包汇总视图并接入试盘 / 风险控制面 v1

### 5.1 目标

- 把小资金试运行需要的关键信号汇总成一张运行包
- 让 operator 能在一个视图里同时看见预检结果、当前风险、trial guard、forward validation、recovery 和 blocker 状态

### 5.2 代码级开发子项

#### B10.1 运行包汇总器

主要改动：

- 聚合 `guarded_live_preflight`
- 聚合 `derivatives_live_guard`
- 聚合 `trial_guard / margin_buffer_overview / recovery / active_blockers / forward_validation`
- 输出 `status / summary / summary_metrics / operator_actions`

当前落点：

- `aats/services/operator/query_service.py`

验收标准：

- 运行包必须能区分 `ready / warning / critical`
- operator 能一眼看出当前限制来自风险、恢复还是试盘守护

#### B10.2 operator 报表接口与控制面接入

主要改动：

- 新增 `/reports/guarded-live-run-packet`
- `risk-view.js` 增加“启盘前自检”和“小资金运行包”卡片
- `trial_review` 挂入运行包摘要

当前落点：

- `aats/services/operator/query_service.py`
- `aats/api/routes.py`
- `aats/api/static/modules/store.js`
- `aats/api/static/modules/views/risk-view.js`
- `aats/api/static/modules/terms.js`

验收标准：

- 风险页能直接看到新卡片
- 前端新增展示文本必须保持干净 UTF-8 中文
- trial review 中能看到运行包摘要，不需要再手工拼字段

## 6. 当前执行顺序

本批次默认顺序如下：

1. 先做 `Task72-B8`，把风险展示升级成运行时硬约束
2. 再做 `Task72-B9`，补齐启盘前结构化预检
3. 最后做 `Task72-B10`，把小资金运行信号收成统一运行包

## 7. 当前完成状态

`Task72-B8` 已实现第一轮版本，当前能力包括：

- 新增 `derivatives_live_guard` 运行时风险守卫
- 合约风险逼近时会自动进入 `only_reduce`
- 清算距离或保证金缓冲越过硬阈值时会自动触发 halt
- blocker control、system health、risk gate 已共享同一套风险结论

`Task72-B9` 已实现第一轮版本，当前能力包括：

- operator 已新增 `/system/guarded-live-preflight`
- `system/runtime` 已暴露 `guarded_live_preflight` 摘要
- 启盘前可结构化校验 runtime、账户、恢复、风险缓冲、trial guard 和资金边界

`Task72-B10` 已实现第一轮版本，当前能力包括：

- operator 已新增 `/reports/guarded-live-run-packet`
- 风险页已新增“启盘前自检”和“小资金运行包”卡片
- `trial_review` 已挂入运行包摘要
- 小资金试运行的关键状态已经可以从一张视图直接读取

## 8. 完成标志

当以下条件同时满足时，可以认为本批次完成：

- 风险逼近时系统会自动进入 `only_reduce` 或 halt，而不是只做展示
- operator 可以在启盘前直接看到结构化自检报告
- 小资金试运行的关键结论可以从一张运行包视图直接读取
- 相关中文文案没有编码污染
