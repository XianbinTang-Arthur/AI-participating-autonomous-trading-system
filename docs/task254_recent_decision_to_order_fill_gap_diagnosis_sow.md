# Task254 Recent Decision To Order/Fill Gap Diagnosis SOW

## 业务目标与边界

本轮目标是让自动化 runtime truth report 能直接解释最新 `portfolio_allocation_decisions` 为什么没有继续生成 order/fill，减少后续 heartbeat 把“无成交”误判成执行链故障或 AI 超时问题。

边界是只读诊断面：不改变策略、风险、执行、provider、symbol、venue、strategy family、release/promotion/tuning、schema 或 live order 行为。

## 模块

- `scripts/runtime_truth_report.py`
- `tests/unit/scripts/test_runtime_truth_report.py`
- 本 SOW 文档

## 输入与输出

输入来自 gateway 容器内既有数据库环境，只读取：

- `portfolio_allocation_decisions`
- `decision_audit_records`
- `execution_orders`
- `order_states`
- `execution_fills`
- `fill_events`

输出为无密钥 JSON 摘要，新增最新 decision 的：

- allocation notional 摘要
- audit refs 是否存在
- execution plan/order/fill 引用数量
- DB order/order_state/fill 计数
- no-trade attribution，包括 classification、primary_blocker、reason_codes、operator_summary 摘要

## 数据库与事务

只读查询，不新增表、不修改 schema、不写入数据、不打开显式事务。

## 权限与安全

探针仍通过 gateway 容器现有环境读取连接信息；脚本不打印连接串、密码、token、API key 或完整原始 payload。

## 错误处理与幂等

数据库不可用、缺少最新 decision、payload 缺失或 JSON 字段结构变化时，报告应保持可生成，并返回缺失字段的空摘要或 `None`。

## 生命周期

该诊断在每次 runtime truth report 生成时运行，用于 Navigator/PM Loop 判断当前 no-fill 是否由策略/allocator 无目标导致。

## 性能

查询只针对最新 allocation 的 decision_id 做索引查询和计数，避免扫描大表 join。

## 日志与审计

报告新增稳定原因码和计数，不输出原始 payload，便于自动化状态与人工复查引用。

## 测试

- 单元测试覆盖 payload 去除、reason code 分类、execution_chain 计数和 sleeve intent 摘要。
- 运行 runtime truth report smoke，确认生产态能生成 no-trade attribution。

## 回滚

回滚 `scripts/runtime_truth_report.py`、`tests/unit/scripts/test_runtime_truth_report.py` 和本文档即可；不涉及数据库或运行时状态回滚。

## 配置

无新增配置项。

## 代码组织

保持在 runtime truth report 脚本内部新增小型纯函数，避免引入应用 settings 或运行时依赖。

## 部署与验收

验收标准：

1. 最新 decision 的 `latest_decision.no_trade_attribution.primary_blocker` 能解释无 order/fill 主因。
2. `latest_decision.execution_chain` 同时包含 audit refs 和 DB 计数。
3. 报告不含原始 payload 或敏感连接信息。
4. 聚焦单元测试和 runtime truth report smoke 通过。
