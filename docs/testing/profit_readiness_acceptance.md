# 收益可信度整改验收矩阵

> 文档状态：现行测试说明
> 最后核对：2026-08-27（当前静态起始 HEAD `9c4112c6`；2026-08-26 derivatives generation 仅作历史运行证据）
> 核对范围：当前静态 schema/API/测试验收条件与明确标日期的历史运行快照；不证明现场状态
> 边界：本文定义可执行验收，不把未运行的项目标记为通过。

| 层级 | 验收项 | 通过条件 | 失败/未知处理 |
| --- | --- | --- | --- |
| 静态 | Ruff | `ruff check aats/` 无错误 | 修复后重跑 |
| 单元 | 全量 unit | 全部通过，无 warning 契约回退 | 停止部署 |
| Schema | 102 张 ORM 表 + 7 张 Batch B SQL 治理表 + 1 张 migration ledger（当前物理总数 110）；18 个有序 Batch B stage，末项 `batch_b_19_historical_research_artifacts` | forward/rollback、ledger checksum、表、唯一键、CHECK 与 registry 一致 | 停止 RDP writer |
| 配置 | derivatives Compose | 公共采集器存在、不加载 live env、七个应用容器 required | 停止部署 |
| 配置 | canary | validator 通过、`deployable=false`、deploy 入口无注册 | 视为安全回退失败 |
| 研究 | 历史审计 | 所有旧候选 `capital_eligible=false` | 禁止引用旧结果 |
| 研究 | v2 dry-run | 源 SHA/协议通过，零 DB/holdout/参数写 | 修复计划或源漂移 |
| 研究 | 新假设预注册 | 运行前固定完整试验族、经济机制、失效条件、Factor DSL、窗口和三类成本 | 拒绝事后登记或删除失败计划 |
| 数据 | collector/eligibility | heartbeat、Silver 最新行和窗口门禁现场通过 | `UNKNOWN`/NO-GO |
| 数据 | coverage/provenance | 只读覆盖 artifact 不可覆盖，source/raw checksum/gap/bundle 可追溯且冲突失败关闭 | 停止导入与重建 |
| 数据 | archive-before-delete | 分区 Parquet、manifest、行数和 SHA-256 验证成功；任一分区异常时整次删除 0 行 | 停止 retention |
| 数据 | historical bundle | historical eligibility 不伪造 live heartbeat；同输入重建 fingerprint 确定；proxy/第三方边界正确 | 保持 ineligible/UNKNOWN |
| 数据 | Factor 输入完整性 | 每个引用字段在全窗及 train/valid/test 均不超过预注册缺失率；不静默填零 | 计算收益前失败关闭 |
| 研究 | 跨运行时 Factor 签名 | 同一 DSL 在 Windows/部署容器与支持的 Python 版本产生相同 signature | 停止登记，不能扩增 trial family |
| 统计 | 完整 campaign | 全计划计数、重复假设折叠，不使用 test，walk-forward/bootstrap/Holm/DSR 全通过 | candidate 不合格；禁止打开 holdout |
| 漏斗 | 模拟预算与风险 | 自然非零 target 不超过现场最严格 cap，且同 decision 全链可追踪 | `UNKNOWN`/修复尺度，不放宽风险 |
| 漏斗 | 不可覆盖证据 | 绑定健康 deployment；≥100 个成熟非零 target；无超 cap、尺度拒绝、阶段断链、拒绝后订单或孤儿 fill | `UNKNOWN`/`FAIL`，production/trading 固定 false |
| 执行健康 | 平仓冷静期 | 重启后 Fill 热缓存由 Postgres truth hydrate；失败回退 PG；平仓后配置窗口内不允许新增风险 | 任一 truth 对齐失败却继续信任缓存，或窗口内重入场即 NO-GO |
| 风险显示 | 净空仓强平距离 | 净数量为负时按 short 方向计算；Operator 显示与 exchange position direction 一致的正距离 | 状态降级并停止依赖该显示 |
| Guard 观测 | 跨进程 trial/derivatives signal | Gateway 从 Redis/NATS signal cache 读取 Execution 发布状态；进程分离时不得误报未配置 | `UNKNOWN`/NO-GO，检查订阅和 freshness |
| 成交 | L2 + paper calibration | 窗口无缺口，生命周期来源和误差门通过 | candidate 不合格 |
| OOS | one-time holdout | claim 先于读取、fingerprint 一致、第二次拒绝 | 保留失败访问 |
| 参数 | generation | 所有 role prepare/commit/readback 精确匹配 | FAILED/ROLLBACK_REQUIRED |
| 韧性 | fault matrix | 五场景六类检查均有真实 ref | `UNKNOWN`/NO-GO |
| 就绪 | readiness v1 | simulation facts 全 PASS；production/trading 仍 false | 不上线 |

## 当前现场验收快照（非持续状态证明）

下列结论只对应 2026-08-26 实现基线 `314adc6e8f17` 和 deployment generation
`314adc6e8f17-20260826T193656Z-763-10457`；它们不是持续状态证明：

- WSL2 规定路径 `~/aats-venv` 为 Python 3.12.14；依赖锁验证通过。Stage 18 隔离迁移与归档恢复
  集成共 `4 passed in 19.91s`；目标库真实 BBO 分区另恢复 1,064 行，SHA、行数与时间边界一致；
- 实现 `314adc6e` 与本轮文档工作区的完整单元回归为
  `4814 passed, 30 skipped, 94 subtests passed in 122.00s`；首次运行只因系统 `%TEMP%` 权限失败，
  改用项目内隔离 `--basetemp` 后完整通过；
- 标准部署 evidence 为
  `/root/aats/deploy/wsl2-dev/runtime/deployment-evidence/20260826T193838939451Z-derivatives-314adc6e8f17.json`。
  七个核心应用/采集容器最终均为 `healthy`；受控 DB outage 后 PostgreSQL 重启 1 次，其余核心容器
  重启计数为 0，Gateway 的 phase1 shadow loop 在约 7 秒内记录 recovered，`/healthz` 为 HTTP 200；
- v5 覆盖 artifact 为
  `/app/artifacts/data_governance/coverage/coverage_20260826T194622424432Z.json`，SHA-256
  `77ed0bcec772b2f5c73c8e396fad3f6ae85286fbe0f31297520f21e01c160ea8`。98 个 dataset 中
  `missing=35`、`observed=37`、`observed_with_quality_issues=24`、
  `present_unbounded_not_scanned=2`、`audit_failed=0`；
  这里的 98 是当时 deployment snapshot 中实际审计到的 dataset 数，不是当前 ORM 表数；
- 一日 OHLCV、funding、mark proxy、4,092,576 笔官方 trade 与 6,684,186 条官方 L2 事件已导入并形成
  `ELIGIBLE` bundle；L2/trade-flow Silver 重建指纹确定且重复执行幂等；Gold 15m/1H 为 96/24 行。
  这不证明历史 OI/强平或当时 AATS live capture；
- 完整 RDP `task_274d8e5f2470` / `run_7dd43c671b064959` 约 6 秒开始并完成 10/10 步骤。Phase 3
  `20260826_194312_bf9a4924`、Phase 4 `20260826_194320_fa86acfa` 与 decision round
  `20260826_194329_bf4d89f6` 绑定；
- 运行结论仍是 `blocked_by_attribution` / `not_ready_attribution_issue`。四个组合均为 `aligned=0`、
  `live_only=0`，5,398 条旧 live 事实缺 lineage；4 条参数升级建议和 4 条 pause 建议均为 draft，
  未 approve、release 或 apply；
- 本轮没有启动 live profile、提交真实订单、打开 holdout、读取账户凭证或应用参数。浏览器因服务重启
  被重定向到登录页，签名页面视觉复核需操作员重新登录。

因此，当前工程链路、一日官方数据、归档恢复和主要故障矩阵已经可以真实运行并失败关闭，但收益可信
仍是 NO-GO：30 日 source-aware 派生与签名 UI 门未通过，90 日容量不安全，精确归因为 0，账户执行事实
未获只读授权，campaign、L2/paper calibration、一次性 holdout 和前瞻模拟未完成。流程退出码 0 不能解释
为策略通过。

### 2026-08-25 上一代快照（历史证据）

下列结论只对应提交 `2c798eab` 和 generation `2c798eab13de-20260825T205326Z-1584-9530`：

- 当时完整单元回归为 `4596 passed, 30 skipped, 94 subtests passed`，标准部署结果为
  `simulation_stack_healthy`；
- 当时 2026-05-16 至 2026-05-28 的 confirmed Silver/closed Gold 各 1,152，三个候选成本后收益
  全部失败，holdout 保持封存，10 个唯一候选通过数为 0；
- 当时 `~/aats-venv` 缺失，WSL2 集成未通过。该环境缺口已在 2026-08-26 修复并完成隔离 PostgreSQL
  集成验证，因此不得再把它列为当前阻断；
- 当时签名 Operator UI 的 PnL/样本没有绑定合格候选，不构成收益证明。该结论仍然有效，但页面状态
  会漂移，必须在重新登录后重新取得。

## 较早现场验收快照（历史证据，不代表当前状态）

下列结论只对应 2026-08-25 19:12--20:07 UTC、最终部署基线 `1beba655` 的本地
`derivatives` 模拟栈；容器、账户、交易所和数据新鲜度会随时间变化，后续测试必须重新生成
证据，不得引用本节代替现场核验。

- 全量单元回归：`4577 passed, 30 skipped, 94 subtests passed`；Ruff 通过；
- 已部署 decision 容器在显式执行 managed-profile 注入后读取到 `derivatives`，现场最严格
  单步 cap 为 1,250；无 legs intent 的 10 × 0.25 缩放结果为 2.5，断言通过；
- 标准部署证据：
  `/root/aats/deploy/wsl2-dev/runtime/deployment-evidence/20260825T195837932361Z-derivatives-1beba655f321.json`；
- 七个必需应用容器均为 `running/healthy`、重启计数为 0。六个应用无
  `ERROR`/`CRITICAL`/未解析 traceback；execution 有 1 次私有 WebSocket ping timeout，约 5 秒
  后自动重连，后续账户刷新、成交同步和平仓均成功；
- 2026-08-25 18:30--18:45 UTC 微观结构窗口现场重算成功：BBO 756、books5 1323、
  trades 6296、OI 69、liquidations 0（允许稀疏），四类数据 lineage 使用同一 `ingest_run_id`；
- collector heartbeat SHA-256 为
  `fc38aa69b92f264fa83f788bcc65d8c86939040e2944734aa2c2c97d4d919ca9`；窗口资格证据
  fingerprint 为 `ec14f9b61b6d35a0065e13c8bd6a1061a69d170b2f81ab4e21b76fc4bd7b4429`，
  现场结果为 `eligible_for_research=true`、`reason_codes=[]`；
- development campaign 计入全部 10 个计划，预先识别 4 个唯一假设与 6 个重复计划；3 个
  有 return series 的代表候选全部为负收益且统计失败，`representative_pass_count=0`、
  `capital_eligible=false`、holdout=`sealed_not_evaluated`；
- 新 `profit_candidates_v3_20260825` 在结果前预注册 4 个唯一经济假设；全部真实 Gold
  development experiment 均因 train/valid 净收益或成本后 edge 为负而失败。完整 2,000 次
  bootstrap campaign 的代表通过数为 0、`capital_eligible=false`，holdout 保持封存；evidence
  SHA-256=`a67403ace4b6197005f161ce1b88aaf42f4231341afa00ab0f2d2966f84d968a`；
- 累计已产生 3 个自然新风险订单和 3 个平仓订单，共 28 个 fill。最强单链包含 allocation、target、policy、
  risk、plan、intent、order、fill 全阶段，risk 批准，1 个订单形成 11 个 partial fill，未发生
  超 cap 或尺度型拒绝；最终 deployment 窗样本只有 2/100，故仍为 `UNKNOWN`；
- 最强单链 artifact 为
  `/root/aats/deploy/wsl2-dev/runtime/execution-funnel-evidence/2a13eb3ba4d1-20260825T1931Z-v2.json`，
  SHA-256=`7de9b88872f6089e3b1bb3acce4a870189ba0ae100cd0835fece00eb8fae3b59`；
  最终 `1beba655` 部署的最新 artifact 为
  `/root/aats/deploy/wsl2-dev/runtime/execution-funnel-evidence/1beba655f321-20260825T2007Z.json`，
  SHA-256=`9fe99963d9eedf4cec90fce6fdf4f5565049dc3b62e5465c6423ad5f1da5b179`；该窗有 2 个成熟
  可执行 target、2 个订单和 13 个 fill，仅因 2/100 保持 `UNKNOWN`，
  两个 readiness 布尔值仍固定 false；
- 现场曾发现平仓后约 17 秒重新开空，违反 profile 的 300 秒冷静期。最终部署四个主进程均从
  Postgres 对齐 15 条 fill；decision 的 Redis 快照仅 11 条。最新自然决策成功恢复 19:51:41Z
  平仓锚点，并在约 444 秒后才开仓。下一次自然平仓约 2 秒后的上下文又报告 298.12 秒冷静期、
  active guard 与 target=0；因 baseline 同时未达到入场阈值，强竞争信号阻断样本仍待积累；
- 当时签名 Operator 页面显示模拟栈对账一致、当前阻断 0、活动委托 0、敞口 0、恢复资格为是；
  同页暴露真实资金报单路径未知、试盘守护未配置。后者的跨进程读路径已由 `2c798eab` 修复，
  前者及其他 live 前置项仍未通过，因此历史快照和当前修复都不构成实盘或盈利证明；
- 上一代 WARNING 主要是 dev HTTP/insecure-cookie 的模拟环境声明，以及 flat/0 target 的
  `normalize_delta` 跳过；后者已降为 DEBUG。最新部署日志还记录 system-status 429、stale feature
  拒绝及上述一次已恢复私有 WS timeout；flat/0 的 `normalize_delta` WARNING 匹配数为 0。

本快照证明数据窗口可研究、模拟服务可运行，并明确证明本轮候选没有正期望证据。它不证明
模拟成交可信，不证明参数 runtime ACK 已接入，也不解除任何 live profile 的 NO-GO。完整差距
见 [`../code_review/profitability_gap_assessment_2026_08_25.md`](../code_review/profitability_gap_assessment_2026_08_25.md)。

Windows 必跑：

```powershell
.venv\Scripts\python.exe -m ruff check aats/
.venv\Scripts\python.exe -m pytest tests/unit/ -x -q
.venv\Scripts\python.exe scripts\rdp_run_candidate_v2_batch.py --dry-run
.venv\Scripts\python.exe scripts\validate_future_canary_contract.py
```

WSL2 集成与模拟运行必须在提交后按唯一部署入口执行。`/healthz` 只证明 Gateway liveness；还需
核对容器、`/system/health`、`/system/recovery`、collector freshness、数据库最新快照和
evidence。没有真实隔离故障注入记录时，fault matrix 必须保持未通过。
