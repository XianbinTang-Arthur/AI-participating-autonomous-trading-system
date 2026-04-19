# Task62 执行质量治理

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 目标

在已有执行质量报表基础上，把明显异常的成交和订单模式标记出来，为后续试盘停机和执行优化提供输入。

## 本轮实现

- 新增 `/reports/execution-anomalies`
- 基于近期 fill / fill outcome 做异常扫描

## 当前异常规则

- `high_adverse_slippage`
- `high_fee_ratio`
- `slow_decision_to_submit`
- `slow_submit_to_fill`

## 输出

- 异常摘要计数
- 每条异常 fill 的标记结果
- 异常对应的时间链路、价格链路、费用链路

## 目的

这一步不是直接自动停机，而是先把可用于停机和执行优化的信号标准化输出。
