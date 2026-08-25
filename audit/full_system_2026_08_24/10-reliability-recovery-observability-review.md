# 10 可靠性、恢复与可观测性审查

> Phase 3D 更新：下文关于“business task 退出但独立 heartbeat 仍绿”是 Phase 1/2
> 原始快照。当前工作区已让显式 critical task 的 exception/cancel/提前返回触发
> daemon 非零退出，FastAPI health 返回 `503`。永久 hang、last-success/lag、依赖
> 断连与真实容器 restart 仍未闭环；详见 `24`。

> Phase 3E 更新：`deploy.sh` 已改为先 build 新镜像再 down，并在 app up 前运行
> 显式 root+RDP schema job；Gateway 不再吞 RDP schema 错误。默认 live、app up
> 非零继续、完整 readiness、collector 与 app+schema rollback 仍 OPEN；详见 `25`/`20`。

> Phase 3F 更新：标准部署现在无默认 profile，只允许显式模拟 profile；全部 live
> profile 在副作用前硬禁用且无 override。down/infra/app up 失败均非零，future live
> required list 已含两个 collector，模拟 evidence 明确不是 trading-ready。完整 readiness
> 与 app+schema+parameter 一致回滚仍 OPEN；详见 `26`/`20`。

> Phase 3J 更新：FS-016 的启动 barrier 已对四主进程 NATS/hybrid 路径失败关闭并绑定部署 generation。下表第 3 项保留 Phase 1/2 原始故障快照；目标 Redis/NATS/Compose 故障矩阵仍 OPEN，详见 `30`。

> Phase 3K 更新：FS-006 已为账户刷新、执行同步、对账、execution outbox/command、Phase 1 shadow 与 trial guard 七条固定周期关键循环加入成功进度 deadline；永久 await 或连续无成功周期会分类为 `stalled` 并触发 nonzero/503。事件驱动任务、整体 event-loop stall 与目标运行验证仍 OPEN，详见 `31`。

## FS-007 — 部署流程可在错误状态下报告成功，且没有安全回滚

- 严重度：P1；置信度：高；类别：deployment / readiness / recovery
- 状态：原始 finding VERIFIED；Phase 3F 风险已隔离，readiness/一致回滚仍 OPEN
- 位置：`scripts/deploy.sh:44,125-138,397-411,460-507`；live overlay 的 liquidations/microstructure services
- 证据：Phase 3F 后无 profile 和全部 live profile 都会在副作用前失败；`--yes` 不能覆盖。build 在 down 前，down/infra/app up 非零均停止，future live required list 已补 liquidations 与 microstructure collector；模拟 evidence 记录 commit/image/container identity，并明确 `production_ready=false`、`trading_ready=false`。仍不检查 recovery clean、reconciliation、account/market freshness、Kill Switch generation/ack、active parameters、exchange mode 或 command backlog，也没有已验证的旧 image + schema + parameter 一致回滚。
- 剩余触发：若未来重新开放 live，业务 task hang/lag、状态 stale、恢复/对账 unsafe、schema/app 不兼容或回滚失败仍可能阻断安全切换。
- 后果：当前标准入口不会进入 live；但这不是任何既有外部栈已停机或未来 live 可安全部署的证明。提前解禁会重新暴露“浅层健康假绿”和不一致恢复风险。
- 建议：保持 live 硬禁用，直到统一 trading-readiness packet、版本化旧 image 和 app+schema+parameter 一致 rollback 在隔离克隆环境通过故障矩阵并经独立复核。

## 15 个关键失败场景

| # | 场景 | 当前控制 | 审计判断/缺口 |
|---:|---|---|---|
| 1 | Market WS 停止但进程存活 | REST fallback、task-exit supervisor、部分 freshness health | task 结束会非零退出；事件驱动 hang/freshness/断连重试与目标运行仍 OPEN |
| 2 | Private account WS 停止 | REST refresh、snapshot freshness | 未做受控断流；UNKNOWN |
| 3 | Redis 启动期不可用 | generation-scoped strict barrier；announce/poll/timeout 失败 | 代码/隔离测试已 fail-closed；真 Redis/NATS/Compose 未验，P2 OPEN |
| 4 | NATS partition/consumer missing | durable consumers、命令 LIMITS、部分 DB/outbox | topic 级恢复证明不完整 |
| 5 | PostgreSQL 连接耗尽 | pool timeout、pre-ping、idle tx timeout | 总预算超过 200；P1 |
| 6 | Gateway 失败 | 其他主进程独立运行 | operator 失明；是否自动 halt 未证明 |
| 7 | Execution 业务 task 退出或固定周期无成功进度 | 显式 critical registry + 七条周期任务 deadline + 非零/503 | 代码/内存路径已修复；事件驱动任务、整体 loop stall、真实容器 restart/告警未验证 |
| 8 | Venue 接单后进程崩溃 | CLAIMED/SENT 扫描、halt、人工对账 | 正确 fail-closed；需故障注入 |
| 9 | REST timeout/5xx 结果模糊 | client order id、reconciliation | 全分支幂等未封板 |
| 10 | Kill switch 在提交窗口切换 | 多层初始检查 | 最终 TOCTOU；P1 |
| 11 | Outbox publisher 停止 | DB pending outbox + task-exit/progress supervisor | 退出、永久 await 与连续 flush 失败可有界检测；真实 backlog/依赖/告警仍未验证 |
| 12 | RDP schema 漂移 | 显式 deploy job + ledger/checksum + startup validate-only | 吞错 ready 已阻断；真 PG manifest/rollback 仍 OPEN |
| 13 | 参数回滚 | token、双人、status 记录 | profile live payload 未回滚；P1 |
| 14 | 部署构建失败 | 先 build 后 down + shell set -e | 旧应用不再因 build 失败被预先停止；其他部署失败仍无一致回滚 |
| 15 | 时钟偏移/naive timestamp | 多处 UTC/aware validation | 部分 DB naive 值被当 UTC；现场偏移 UNKNOWN |

## 恢复机制

已确认的强控制：

- startup recovery 检测 pending/CLAIMED/SENT、缺 command、truth pending、未知 exchange write 等状态并阻断自动提交；
- portfolio 可由持久化 fills/snapshot/ledger 重建；
- reconciliation 比较本地与交易所并把模糊状态提升为人工 review/halt；
- kill switch 关闭过程不自动 resume，Redis 状态保留。

尚未证明：冷备恢复点、备份可读性、PostgreSQL volume 损坏、Redis/NATS volume 丢失、整机重启顺序、DNS/证书过期、交易所维护窗口、operator 失联时的自动安全姿态。

## 可观测性

系统包含 structured logging、Prometheus、Grafana、Loki、Promtail、Jaeger/OTel，NATS publish/receive span、多个业务 metrics 和 health snapshot。问题在于“有指标”不等于“部署 gate 使用指标”：当前成功判定没有消费 recovery、reconciliation、task freshness、outbox backlog 等关键事实。

建议把每个 critical loop 的 generation、last_success、last_error、lag、queue depth、source as_of、restart_count 汇总成单一 machine-readable readiness；告警必须做送达和人工响应演练。

Phase 3D 新增 task completion 监督；Phase 3K 再为七条固定周期资金关键循环增加
成功进度 deadline。19 项 Phase 3K focused、118 项扩大相关和最终 4,296 项全量
unit 已通过，但没有运行 WSL2 四进程、Docker health/restart、真实依赖断连、
事件驱动 task freshness/queue lag 或整体 event-loop stall 故障注入。当前裁定为
`PARTIALLY REMEDIATED / EVENT-DRIVEN AND TARGET RUNTIME VERIFICATION OPEN`。

Phase 3J 又收口了启动 provisioning 这一层：同代次 peer 未全部就绪时 publisher 不启动，但 ready key 不是持续 liveness/freshness lease。目标环境的启动/重启/断连、task hang/lag 与告警仍必须分别验证。
