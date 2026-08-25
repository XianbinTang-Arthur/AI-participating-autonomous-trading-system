# 收益可信度整改验收矩阵

> 文档状态：现行测试说明
> 最后核对：2026-08-25（实现与模拟部署基线 `6749ea8a515fc84f8ab8b38de5790c8f5c0fc17c`）
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
| 数据 | collector/eligibility | heartbeat、Silver 最新行和窗口门禁现场通过 | `UNKNOWN`/NO-GO |
| 统计 | 完整 campaign | 全计划计数、重复假设折叠，不使用 test，walk-forward/bootstrap/Holm/DSR 全通过 | candidate 不合格；禁止打开 holdout |
| 漏斗 | 模拟预算与风险 | 自然非零 target 不超过现场最严格 cap，且同 decision 全链可追踪 | `UNKNOWN`/修复尺度，不放宽风险 |
| 漏斗 | 不可覆盖证据 | 绑定健康 deployment；≥100 个成熟非零 target；无超 cap、尺度拒绝、阶段断链、拒绝后订单或孤儿 fill | `UNKNOWN`/`FAIL`，production/trading 固定 false |
| 成交 | L2 + paper calibration | 窗口无缺口，生命周期来源和误差门通过 | candidate 不合格 |
| OOS | one-time holdout | claim 先于读取、fingerprint 一致、第二次拒绝 | 保留失败访问 |
| 参数 | generation | 所有 role prepare/commit/readback 精确匹配 | FAILED/ROLLBACK_REQUIRED |
| 韧性 | fault matrix | 五场景六类检查均有真实 ref | `UNKNOWN`/NO-GO |
| 就绪 | readiness v1 | simulation facts 全 PASS；production/trading 仍 false | 不上线 |

## 现场验收快照（非持续状态证明）

下列结论只对应 2026-08-25 18:30--18:51 UTC、实现基线 `6749ea8a` 的本地
`derivatives` 模拟栈；容器、账户、交易所和数据新鲜度会随时间变化，后续测试必须重新生成
证据，不得引用本节代替现场核验。

- 全量单元回归：`4559 passed, 30 skipped, 94 subtests passed`；Ruff 通过；
- 已部署 decision 容器在显式执行 managed-profile 注入后读取到 `derivatives`，现场最严格
  单步 cap 为 1,250；无 legs intent 的 10 × 0.25 缩放结果为 2.5，断言通过；
- 标准部署证据：
  `/root/aats/deploy/wsl2-dev/runtime/deployment-evidence/20260825T184345080927Z-derivatives-6749ea8a515f.json`；
- 七个必需应用容器均为 `running/healthy`、重启计数为 0，最近 15 分钟无
  `ERROR`/`CRITICAL`/未解析 traceback；
- 2026-08-25 18:30--18:45 UTC 微观结构窗口现场重算成功：BBO 756、books5 1323、
  trades 6296、OI 69、liquidations 0（允许稀疏），四类数据 lineage 使用同一 `ingest_run_id`；
- collector heartbeat SHA-256 为
  `fc38aa69b92f264fa83f788bcc65d8c86939040e2944734aa2c2c97d4d919ca9`；窗口资格证据
  fingerprint 为 `ec14f9b61b6d35a0065e13c8bd6a1061a69d170b2f81ab4e21b76fc4bd7b4429`，
  现场结果为 `eligible_for_research=true`、`reason_codes=[]`；
- development campaign 计入全部 10 个计划，预先识别 4 个唯一假设与 6 个重复计划；3 个
  有 return series 的代表候选全部为负收益且统计失败，`representative_pass_count=0`、
  `capital_eligible=false`、holdout=`sealed_not_evaluated`；
- 预算修复前两代部署观察分别产生 25 组和 6 组 target/risk，均被 risk 批准但均为 flat/0，
  未产生 execution plan、order intent、order 或 fill；这证明这些窗口没有风险阻断，但自然
  非零信号下的预算修复仍为 `UNKNOWN`；
- 新漏斗 CLI 的当前现场 artifact 为
  `/root/aats/deploy/wsl2-dev/runtime/execution-funnel-evidence/6749ea8a515f-20260825T1850Z.json`，
  SHA-256=`9aa131ff7a54ce0f027037900c62673e2934ce007281b006ddff5e6a4199f0c4`；
  它绑定当前 deployment 并覆盖 8 个自然 flat/0 决策周期，结果为 `UNKNOWN`，成熟非零目标、
  订单、成交均为 0，两个 readiness 布尔值固定 false；
- 签名 Operator 页面显示模拟栈对账一致、当前阻断 0、活动委托 0、敞口 0、恢复资格为是；
  同页仍明确暴露真实资金报单路径未知、试盘守护未配置，因此不构成实盘或盈利证明；
- 上一代 WARNING 主要是 dev HTTP/insecure-cookie 的模拟环境声明，以及 flat/0 target 的
  `normalize_delta` 跳过；后者已降为 DEBUG。最新部署日志复核中七个应用 error 匹配数为 0，
  flat/0 的 `normalize_delta` WARNING 匹配数也为 0。

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
