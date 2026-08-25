# 收益证据与模拟交易就绪运行手册

> 文档状态：现行操作说明
> 最后核对：2026-08-25（实现基线 `52cd026a98b073ff2d25693c38f8fe5f643688f9`）
> 适用范围：`derivatives` 本地模拟栈、RDP 研究库、Research Factory 研究产物
> 禁止范围：真实资金、live profile、真实订单、手工绕过部署入口

本手册把“容器启动”“采到数据”“候选通过”“模拟成交可信”和“允许实盘”拆成不同结论。
任何一步缺失、失败、过期、`UNKNOWN` 或 `DEGRADED`，都不能向后推导。格式 v1 最多产生
`simulation_ready=true`，`production_ready` 与 `trading_ready` 固定为 `false`。

## 1. 当前能力与硬边界

| 能力 | 当前状态 | 可得出的结论 |
| --- | --- | --- |
| 公共 BBO/books5/trades/OI/强平采集 | 已纳入标准 derivatives Compose | 部署后可验证公共数据新鲜度；静态代码不证明现场已有数据 |
| 微观结构资格 | 已实现 15 分钟窗口门禁 | 连续频道满足样本、完整性和 lineage 后才可用于研究 |
| 历史候选资金资格 | 已完成确定性审计 | 旧 `benchmark_segment=test` 等产物全部不可作为资金证据 |
| v2 复跑 | 已生成计划并提供两阶段批处理 | development 不读 holdout；完整阶段强制要求 L2 成本摘要 |
| Campaign 统计门禁 | 已自动串联实验 return series、全试验计数、重复假设、walk-forward、bootstrap、Holm、deflated Sharpe | 本次 3 个可评估代表候选全部失败，不具备资本资格 |
| 模拟执行预算 | 已修复方向 intent 只缩审计预算、不缩 qty 的错误，并按最严格现有 cap 限制单步目标 | 确定性测试通过；部署后尚未出现自然非零信号，运行验收仍是 `UNKNOWN` |
| 一次性 holdout | 已实现 DB 唯一账本和先占用后读取协议 | 失败也消耗访问；不允许事后补登记已看过的 test 指标 |
| L2/event 回放 | 已实现 top-5、共享深度、partial/no-fill、post-only 队列近似 | 盘口研究证据，不等于交易所撮合真值 |
| 模拟生命周期校准 | 已实现 order/command/transition/fill 对齐 | 只接受 `paper_local`，不读取 live 凭证 |
| 参数 generation | 已实现状态机与 schema | runtime worker 尚未接入 ACK，apply/rollback API 继续返回 501 |
| 故障矩阵 | 已实现固定场景、证据 schema 与聚合 | 尚无经批准的隔离故障注入器；空模板必然失败 |
| future canary | 已实现不可部署的最小权限契约 | 未注册 deploy profile，不构成上线许可 |

## 2. 标准模拟部署与公共采集

Windows 工作区代码必须先提交；随后只使用标准入口：

```bash
bash scripts/deploy.sh --profile derivatives --skip-commit
```

应用必需集合为 gateway、market、decision、execution、rdp-daemon、liquidations-daemon、
microstructure-collector。两个公共采集器只连接 OKX 公共频道，只写 `aats_research`，不加载
`.env.derivatives.live`，也不订阅 execution command。

部署成功后还要核对 `/healthz`、`/system/health`、`/system/recovery`、deployment evidence
中的七个应用容器和 collector heartbeat、四类 Silver 数据最新时间。强平为稀疏频道：零事件
可以正常，但 collector 必须新鲜。`/healthz` 不能替代其余证据。

## 3. 微观结构数据资格

运行环境只注入 `RDP_DATABASE_URL`；脚本不读取 `.env`：

```bash
python scripts/write_collector_freshness_evidence.py \
  --output artifacts/research/microstructure_eligibility/collector_<timestamp>.json

python scripts/rdp_validate_microstructure_window.py \
  --symbol BTC-USDT-SWAP \
  --window-start 2026-08-25T00:00:00+00:00 \
  --collector-evidence artifacts/research/microstructure_eligibility/collector_<timestamp>.json \
  --output artifacts/research/microstructure_eligibility/<window>.json
```

连续频道的缺样本、必填值空、fatal quality flag、混合 lineage 都失败。强平窗口内零事件可通过，
但 heartbeat 必须在资格判定时重新计算且小于 60 秒；脚本不会信任旧 packet 中遗留的
`fresh=true`。省略 `--window-start` 时，“最新”Silver 窗口结束时间还必须在 30 分钟内；显式
指定历史窗口才关闭当前时效限制。输出不可覆盖并包含确定性 fingerprint。

## 4. 历史候选与 v2 复跑

```powershell
.venv\Scripts\python.exe scripts\rdp_audit_and_plan_candidate_v2.py
.venv\Scripts\python.exe scripts\rdp_run_candidate_v2_batch.py --dry-run
```

第一条命令保留原 artifact，在追加 registry 中记录 `capital_eligible=false`，并按源 candidate/
spec SHA-256 生成稳定计划。dry-run 校验全部计划，且不访问数据库、holdout 或参数表。

受控环境中的两阶段运行：

```bash
python scripts/rdp_run_candidate_v2_batch.py --phase development
python scripts/rdp_run_candidate_v2_batch.py \
  --phase evidence-complete \
  --execution-summary-root artifacts/research/research_factory/l2_cost_by_plan
```

`development` 不访问 sealed test，也不产生资金资格；`evidence-complete` 要求每个 plan 对应的
`l2_event_replay_v1`、`benchmark_segment=valid` 成本摘要。源 SHA 漂移、缺摘要、非 L2 模型或
timeframe 不一致都会停止。两个阶段都不会写 active parameters 或提交订单。

## 5. 统计、L2 与模拟校准

统计输入只能使用 development/OOS fold 收益，并包含真实试验族与 trial count。完整候选族应优先
使用 campaign 命令，从对应实验的不可变 return-series artifact 自动推导 p 值：

```bash
python scripts/rdp_evaluate_candidate_campaign.py \
  --output-root artifacts/research/research_factory/campaigns/<campaign_id> \
  --replications 2000 \
  --seed 7
```

输出目录不可预先存在。命令必须在所有 development 实验结束后运行；没有 return series 的计划
仍以失败试验计入，不得从 plan root 删除。输出中的 `representative_pass_count=0`、任一代表候选
失败或 `capital_eligible=false` 都意味着停止后续 L2/holdout 流程。本次现场结果见
[`../code_review/profitability_gap_assessment_2026_08_25.md`](../code_review/profitability_gap_assessment_2026_08_25.md)。

旧的单候选 CLI 保留兼容，只能用于诊断已经构造好的输入，不能替代完整 campaign：

```bash
python scripts/rdp_evaluate_candidate_statistics.py \
  --input artifacts/research/research_factory/statistics_inputs/<candidate>.json \
  --output artifacts/research/research_factory/experiments/<id>/statistical_evidence.json
```

L2 request manifest 必须绑定 `plan_id`、Gold dataset fingerprint、
`benchmark_segment=valid`、timeframe 和具体订单；生成的成本摘要会保留 `plan_id`，批处理会同时
核对候选、symbol 与 timeframe，防止跨候选串用。eligibility manifest 必须覆盖订单等待区间内
每个 15 分钟窗口且无间隙：

```bash
python scripts/rdp_run_l2_execution_replay.py \
  --request-manifest <requests.json> \
  --eligibility-manifest <eligibility_manifest.json> \
  --output artifacts/research/l2_execution/<id>.json \
  --cost-summary-output artifacts/research/research_factory/l2_cost_by_plan/<plan_id>.json

python scripts/rdp_calibrate_l2_against_paper.py \
  --l2-evidence artifacts/research/l2_execution/<id>.json \
  --output artifacts/research/execution_calibration/<id>.json
```

校准只接受只读连接和 `source_system=paper_local`。状态链错误、submit 未 ACK、fill 数量/状态
不一致、来源错误或误差超限均失败。

### 5.1 模拟执行漏斗

每次部署后必须按同一个 `decision_id` 串联以下事实：

```text
portfolio allocation -> position target -> policy -> risk
  -> execution plan -> order intent -> order state -> fill
```

空仓新增风险的名义目标不得超过当前 profile 所有正值额度中的最小值。当前 derivatives 模拟
配置下该值为 1,250，但运行检查必须读取现场配置，不能永久写死该数字。flat/0 target 只证明
系统选择观望；它不能作为预算缩放、订单或成交样本。风险拒绝必须保留原因，严禁为凑成交数
放宽上限。默认校准门为：至少 20 个匹配订单、生命周期有效率 100%、fill ratio MAE ≤ 0.20、
均价误差均值 ≤ 10 bps、费用误差均值 ≤ 1 bps、command-to-terminal p95 ≤ 5 秒。

## 6. 一次性 holdout

账本唯一键是 `(candidate_id, holdout_content_fingerprint)`。候选专用 evaluator 必须先校验
非 holdout 证据并提交 `access_started`，然后才能读取 sealed test；实际 test fingerprint
必须与 seal 一致。终态只能为 `evaluated_pass`、`evaluated_fail` 或 `access_failed`，第二次读取
必须拒绝。

仓库不提供“从已计算的 test 指标补登记”的命令，因为这会绕过先占用后读取边界。第一次执行
崩溃同样消耗访问，不能以换 actor 的方式复读。

## 7. 参数生效

Batch B stage 16 创建 operation 和 runtime ACK 表。每个预期 role 必须对同一 generation 和
payload SHA-256 完成 prepare；execution authority 开始 commit 后，还需 commit 与 readback，
读回 parameter set ID 必须等于目标。stale generation、payload mismatch、拒绝、超时或部分
readback 均不能成功。

当前 worker 未接入 ACK/内存读回，所以 apply/rollback 继续无写入返回 501，不得直接调用历史
profile saga，也不能把 `active_parameter_sets` 行解释为进程已生效。readiness 中的
`parameter_activation_readback` 必须保持 `UNKNOWN`，直至 worker 集成和真实重启演练完成。

## 8. 故障矩阵

固定场景为 Redis 断连、NATS 断连、execution restart、stale generation、activation TTL
过期。每个场景必须证明 baseline 健康、故障发生、新增风险被阻断、没有非预期订单、恢复核对和
清理完成。空模板预期失败：

```bash
python scripts/write_fault_matrix_evidence.py \
  --manifest <completed_observations.json> \
  --output deploy/wsl2-dev/runtime/fault-matrix-evidence/<generation>.json
```

当前没有允许在共享本地栈任意停 Redis/NATS 的第二入口。真实注入必须先提供独立命名、独立
volume、可验证 cleanup 的隔离 harness；不能用人工描述填成 PASS。

## 9. Trading readiness 与 canary

从 `configs/templates/trading_readiness_manifest.example.json` 开始，只填真实 evidence ref：

```bash
python scripts/write_trading_readiness_evidence.py \
  --manifest <completed_readiness_manifest.json> \
  --output deploy/wsl2-dev/runtime/trading-readiness-evidence/<generation>.json
```

`simulation_ready=true` 要求 15 类证据全部 PASS 且未过期。格式 v1 即使全部通过，
`production_ready=false`、`trading_ready=false`。未来 canary 契约检查：

```powershell
.venv\Scripts\python.exe scripts\validate_future_canary_contract.py
```

契约固定单一 BTC 永续、逐仓、1x、单笔 25 USDT、总敞口 50 USDT、日损失 5 USDT、无提现/
划转权限、双人签和手工恢复，且 `deployable=false`。这些数字只是未来最大上限，不是当前建议
入金金额，也不代表可部署。

## 10. 停止条件

数据 lineage 混合、collector 陈旧、候选使用 test 选型、统计 trial count 不可信、L2 窗口
不完整、校准来源不是 paper、holdout 已访问、worker readback 缺失、故障证据靠人工断言、
reconciliation 非 normal、Kill Switch 代次/许可不一致或 live profile 被意外注册时，立即停止。
保留错误证据，不修改为 PASS，不使用 override。

当前项目到真实收益的逐层差距、完成顺序和 NO-GO 结论以
[`../code_review/profitability_gap_assessment_2026_08_25.md`](../code_review/profitability_gap_assessment_2026_08_25.md)
为准；该评估仍不能替代每次操作前的现场检查。
