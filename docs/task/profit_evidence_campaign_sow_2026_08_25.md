# 候选收益证据 Campaign 自动化 SOW

> 文档状态：已实施任务书
> 最后核对：2026-08-25（起始 HEAD `cd0e4daa`；实现提交 `d026bc19455f2e6a21e0695b5e98294d930db9dc`）
> 核对范围：Research Factory v2 development 实验、候选回放计划、统计证据与测试契约
> 运行时边界：本任务只处理研究开发段证据，不读取封存 test/holdout，不写运行参数，
> 不提交订单，不解除任何 live profile 的失败关闭门禁，也不承诺盈利。

## 1. 业务目标与边界

目标是消除“实验指标存在，但统计输入仍可由人工填写”的证据断点，使每个候选的开发段
净收益序列都由同一次真实数据实验自动产出，并以整个计划族为单位执行重复假设识别、
block bootstrap、Holm-Bonferroni 多重检验、deflated Sharpe 和 purged walk-forward。

本次交付必须：

1. 自动保存 train/valid 两个开发段的净收益序列及指纹；
2. 明确证明 holdout 只记录封存状态和内容指纹，不暴露收益或指标；
3. 把所有已计划尝试计入 trial count，不能只统计成功或看起来最好的候选；
4. 将同一因子、同一市场范围和同一成本假设的重放标记为重复假设，只保留一个代表参与选择；
5. 从实验产物自动构造候选族 p 值并生成不可覆盖的逐候选统计证据与 campaign 总表；
6. 保持旧的单候选统计 CLI 兼容，不自动产生资金资格。

非目标包括 L2 成交回放、模拟成交校准、holdout 开封、参数发布、canary 或真实资金试单；
这些仍是后续独立门禁，不能由本任务结果替代。

## 2. 模块职责与领域模型

`benchmarks.baseline` 是收益计算唯一实现，向实验层提供与指标完全同源的 gross/net return
series；`real_data` 负责把 train/valid 系列连同数据集、成本和代码版本写入实验目录；新的
campaign CLI 负责验证计划与实验的闭环、构建试验族、识别重复假设并计算统计证据。

新增领域对象以 JSON schema 表达：`DevelopmentReturnSeries`、`CandidateCampaignEntry` 和
`CandidateCampaignEvidence`。它们都是研究证据，不是交易信号、运行参数或资金授权。

## 3. 输入与输出接口

输入：

- 受版本控制且 SHA-256 校验通过的 v2 replay plan；
- 对应实验目录中的 manifest、candidate、development evidence 和 return series；
- CLI 显式传入的 bootstrap、walk-forward 与显著性阈值。

输出：

- 每个实验的 `development_return_series.json`；
- campaign 输出目录下的 `candidates/<candidate_id>.json`；
- campaign 输出目录下的 `campaign_evidence.json`。

输出使用不可覆盖写入。所有路径必须受指定 research artifact root 约束；输出不得含连接串、
密码、token、API key 或原始认证信息。

## 4. 数据库 Schema、表、索引与约束

本次不新增、不修改数据库表、索引、约束或 migration。Gold replay 数据仍通过现有只读查询
加载；campaign 只读取本地不可变实验证据。若未来将 campaign 注册到数据库，必须另行设计
唯一 campaign ID、artifact hash 和事务边界，本任务不得提前写入治理表。

## 5. 事务、一致性与并发

return series 必须在同一次实验内由用于计算 metrics 的同一因子值、标签值和成本参数生成。
series fingerprint 覆盖完整有序数值，campaign 校验 candidate、dataset fingerprint、协议和
实验 ID 一致后才计算。所有候选先完成只读验证和统计计算，再进行不可覆盖写入；部分失败
不得生成伪完整总表。并发运行撞到既有目标时失败，不允许 last-writer-wins。

## 6. 授权、认证与数据安全

CLI 不读取 `.env`，不需要交易所私有权限，不接触订单接口。数据库访问仍只发生在既有
development runner 中，并由调用环境显式注入连接。campaign 输入做敏感字段名拒绝检查；
任何输出都必须声明 `development_only` 和 `no_live_trading_authorization`。

## 7. 错误处理与幂等

缺计划、SHA 不匹配、路径越界、manifest 非成功、candidate 缺失、dataset 指纹不一致、
return series 损坏、样本不足、holdout 泄露或已有输出均失败关闭。计划中失败的试验仍计入
总 trial count，并以 p 值 1.0 保留在族中；但没有完整成功实验的条目不生成候选通过证据。
相同输入和配置产生稳定 hypothesis fingerprint，重复执行只能写到新的显式输出目录。

## 8. 状态转换与生命周期

单条计划状态为：

```text
PLANNED -> EXPERIMENT_UNAVAILABLE | EXPERIMENT_FAILED
PLANNED -> EVIDENCE_VALIDATED -> DUPLICATE_HYPOTHESIS
PLANNED -> EVIDENCE_VALIDATED -> REPRESENTATIVE -> STATISTICS_PASS | STATISTICS_FAIL
```

campaign 只有全部条目完成验证与计算后才能写 `COMPLETE`；任一结构性错误直接失败且不写
完成标记。统计通过仍是 development evidence，不能转换为 capital eligible。

## 9. 缓存与性能

候选规模当前为十级，收益序列在内存中有界计算。bootstrap 复用现有确定性实现并允许显式
配置 replication；每条代表候选只计算一次 bootstrap，族 p 值复用其结果。不得扫描 artifact
root 之外的目录，也不得加载 holdout return series。

## 10. 日志、监控与审计

CLI 标准输出只给出输出路径、SHA、计划数、唯一假设数、代表候选通过数和失败数，不打印
完整收益序列。campaign 记录输入引用、源 SHA、代码协议、统计参数、重复组、原始/校正 p 值、
原因码和生成时间；时间不参与 hypothesis fingerprint。

## 11. 测试策略

1. baseline 单元测试证明公开 return series 与 metrics 的收益口径同源；
2. real-data runner 测试证明 train/valid 被写入、指纹稳定、holdout 不暴露任何数值；
3. campaign 单元测试覆盖重复计划、失败试验计数、Holm 全族计算、路径越界、篡改、不可覆盖；
4. 运行最窄相关单测、Ruff、完整 unit；
5. WSL2 中使用 derivatives 模拟环境运行 development batch/campaign，严禁 live profile。

## 12. Migration、Rollback 与兼容

没有数据库 migration。新增 artifact 和 CLI，不删除旧文件；原 `run_factor_baseline` 与
`rdp_evaluate_candidate_statistics.py` 接口保持兼容。回滚时移除新增生成逻辑和 campaign CLI
即可，历史新 artifact 可作为研究证据保留，但旧代码会忽略其 output ref。

## 13. 配置与环境隔离

所有默认路径位于 `artifacts/research/research_factory`。development runner 只能使用
`real_factor_development`，campaign 无网络和数据库依赖。test/holdout 始终为
`sealed_not_evaluated`；simulation、staging 与 live 的证据不得混写。

## 14. 代码组织与依赖

收益算法保留在 `aats/data_platform/research_factory/benchmarks/baseline.py`，实验 artifact 构造
保留在 `real_data.py`，编排放在 `scripts/`，测试分别放在现有 research factory 和 scripts
单测目录。不引入第三方依赖，不复制统计公式，不修改交易执行模块。

## 15. 文档与运维手册

同步更新收益就绪 runbook、验收文档和文档索引，明确 campaign 命令、输出解释、失败原因和
证据边界。文档必须区分“代码能力”“本次实际运行结果”“仍未知/未完成”，不得把回测统计
写成真实盈利证明。

## 16. 部署与验收标准

验收要求：

- return series 由真实实验自动生成，且 valid 指标可从其净收益复算；
- artifact 明确只含 train/valid，holdout 无 metrics/returns/value 字段；
- 10 个计划全部计入 trial count，重复假设被分组且只有代表可参加选择；
- p 值来自同一 campaign 内的 deterministic block bootstrap，不再依赖人工填写；
- 每个代表候选同时经过 purged walk-forward、Holm 和 deflated Sharpe；
- 输出不可覆盖、无秘密、无数据库/参数/订单副作用；
- Ruff、完整 unit、最窄 WSL2 集成/运行验收通过，或对任何环境阻塞给出准确证据；
- live profile 继续失败关闭，结果不得声称资本资格或可盈利。

### 实施结果

本任务已按实现提交完成。WSL2 development campaign 实际计入 10 个计划、预先识别 4 个
唯一假设与 6 个重复计划；其中 3 个代表候选具备 return series，统计通过数为 0，
`capital_eligible=false`，holdout 保持 `sealed_not_evaluated`。当前结论与后续动作见
[`../code_review/profitability_gap_assessment_2026_08_25.md`](../code_review/profitability_gap_assessment_2026_08_25.md)。
