# 收益可信度整改验收矩阵

> 文档状态：现行测试说明
> 最后核对：2026-08-25（实施基线 `c5124eb8dc91246dbf0319d1b6b0fc9e662a3a5b` + 本任务工作区）
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
