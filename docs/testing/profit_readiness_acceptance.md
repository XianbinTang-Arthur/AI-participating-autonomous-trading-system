# 收益可信度整改验收矩阵

> 文档状态：现行测试说明
> 最后核对：2026-08-25（实现基线 `a658164134101f62617865160105ef35d57328f9`）
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
| 统计 | walk-forward/多重检验 | 不使用 test，独立门全部通过 | candidate 不合格 |
| 成交 | L2 + paper calibration | 窗口无缺口，生命周期来源和误差门通过 | candidate 不合格 |
| OOS | one-time holdout | claim 先于读取、fingerprint 一致、第二次拒绝 | 保留失败访问 |
| 参数 | generation | 所有 role prepare/commit/readback 精确匹配 | FAILED/ROLLBACK_REQUIRED |
| 韧性 | fault matrix | 五场景六类检查均有真实 ref | `UNKNOWN`/NO-GO |
| 就绪 | readiness v1 | simulation facts 全 PASS；production/trading 仍 false | 不上线 |

## 现场验收快照（非持续状态证明）

下列结论只对应 2026-08-25 17:17--17:18 UTC、实现基线 `a6581641` 的本地
`derivatives` 模拟栈；容器、账户、交易所和数据新鲜度会随时间变化，后续测试必须重新生成
证据，不得引用本节代替现场核验。

- 全量单元回归：`4540 passed, 30 skipped, 94 subtests passed`；Ruff 通过；
- 标准部署证据：
  `/root/aats/deploy/wsl2-dev/runtime/deployment-evidence/20260825T171727161865Z-derivatives-a65816413410.json`；
- 七个必需应用容器均为 `running/healthy`、重启计数为 0，最近 15 分钟无
  `ERROR`/`CRITICAL`/未解析 traceback；
- 2026-08-25 17:00 UTC 微观结构窗口在新聚合代码下重算成功：BBO 782、books5 1387、
  trades 20375、OI 80、liquidations 15，四类数据 lineage 使用同一 `ingest_run_id`；
- collector heartbeat SHA-256 为
  `daef817b8b2bd226020912ab2dc29796ac433cf6e23ff4291154cf6c995c3771`；窗口资格证据
  SHA-256 为 `05b8227a285886c82598e78b4d14b5e2d52c4caae7b46d3d990e596857b0a745`，
  现场结果为 `eligible_for_research=true`、`reason_codes=[]`；
- 签名 Operator 页面显示模拟栈对账一致、当前阻断 0、活动委托 0、敞口 0、恢复资格为是；
  同页仍明确暴露真实资金报单路径未知、试盘守护未配置，因此不构成实盘或盈利证明；
- 系统仍有 OKX `system/status` 端点一次 `50011` 限频并执行 300 秒退避；数据 WebSocket
  collector 持续写入且未报错。该告警不阻断模拟栈，但上线评审必须重新观察其频率。

本快照只证明一条数据窗口通过研究资格门禁。它不证明候选策略具备正期望，不证明
参数 runtime ACK 已接入，也不解除任何 live profile 的 NO-GO。

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
