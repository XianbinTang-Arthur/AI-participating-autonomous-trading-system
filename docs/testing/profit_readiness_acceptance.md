# 收益可信度整改验收矩阵

> 文档状态：现行测试说明
> 最后核对：2026-08-25（实现 `2c798eab13dedd6c65287d64ae46499d98492ce2`；模拟部署 generation `2c798eab13de-20260825T205326Z-1584-9530`）
> 边界：本文定义可执行验收，不把未运行的项目标记为通过。

| 层级 | 验收项 | 通过条件 | 失败/未知处理 |
| --- | --- | --- | --- |
| 静态 | Ruff | `ruff check aats/` 无错误 | 修复后重跑 |
| 单元 | 全量 unit | 全部通过，无 warning 契约回退 | 停止部署 |
| Schema | Batch B stage 16 | forward/rollback、表、唯一键、CHECK 与 registry 一致 | 停止 RDP writer |
| 配置 | derivatives Compose | 公共采集器存在、不加载 live env、七个应用容器 required | 停止部署 |
| 配置 | canary | validator 通过、`deployable=false`、deploy 入口无注册 | 视为安全回退失败 |
| 研究 | 历史审计 | 所有旧候选 `capital_eligible=false` | 禁止引用旧结果 |
| 研究 | v2 dry-run | 源 SHA/协议通过，零 DB/holdout/参数写 | 修复计划或源漂移 |
| 研究 | 新假设预注册 | 运行前固定完整试验族、经济机制、失效条件、Factor DSL、窗口和三类成本 | 拒绝事后登记或删除失败计划 |
| 数据 | collector/eligibility | heartbeat、Silver 最新行和窗口门禁现场通过 | `UNKNOWN`/NO-GO |
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

下列结论只对应 2026-08-25 最终提交和 deployment generation
`2c798eab13de-20260825T205326Z-1584-9530`；它们不是持续状态证明：

- Ruff 全量通过；最终完整单元回归为 `4596 passed, 30 skipped, 94 subtests passed`；
- 标准部署证据为
  `/root/aats/deploy/wsl2-dev/runtime/deployment-evidence/20260825T205451196702Z-derivatives-2c798eab13de.json`，
  结果 `simulation_stack_healthy`，七个必需应用容器及基础设施 healthy，production/trading 均为 false；
- 最终部署窗内应用日志没有匹配到新的 error、exception、timeout 或 reconnect；`/healthz` 为 200，
  但仍只代表 Gateway liveness；
- 2026-05-16 至 2026-05-28 半开窗口的 confirmed Silver 与 closed Gold 均为 1,152，时间缺口和
  funding 缺失为 0。每个所需微观结构字段为 1,150/1,152 非空，缺失率 0.173611% ≤ 1%；
- 微观结构 campaign 登记证据 SHA-256 为
  `a38afb4618b372d88b9c5cea8e9a9ef58cfe875ecbb3e3d125a3637039586019`，统计证据 SHA-256 为
  `ca311e020b3843905b1c6b289bc6d42daafc6825f0e16aac436c4e4e2537bab5`；三个候选 train/valid
  成本后收益全部失败，holdout 保持封存。累计三个阶段的 10 个唯一候选通过数仍为 0；
- 签名 Operator UI 只读复核显示 trial guard 为“监控中”，最近强平距离为正的 3,081.29%，没有
  硬阻断；页面中的 7 个已关闭模拟样本和 24 小时模拟 PnL 未绑定合格候选，不构成收益证明；
- WSL2 集成 pytest 已尝试，但规定路径 `~/aats-venv/bin/python` 不存在，因此该项是环境缺口、
  未通过也未伪报成功。数据重建、campaign 和部署运行证据不能替代缺失的 pytest 环境。

本快照只证明最终代码、数据研究链和模拟栈达到上述有限结论；候选经济性仍明确失败，参数
readback、隔离故障矩阵、候选绑定的 L2/paper forward 和 live 入口仍未通过。

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
