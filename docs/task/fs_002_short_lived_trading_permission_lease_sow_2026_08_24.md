# FS-002 全分区短时交易许可租约设计及实施范围

> 文档状态：Phase 3L 已实施 / 设计与验收边界冻结  
> 最后核对：2026-08-24（起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 实施起点：`codex/fs-002-kill-switch-p0` 未提交 Phase 3A–3K 工作区  
> 安全边界：仅代码、纯内存和静态验证；不读取 `.env.*`，不连接真实账户、交易所、Redis/NATS/Postgres、Docker 或 WSL2  
> 目标裁定：**PARTIALLY REMEDIATED / TARGET PARTITION-EXPIRY VERIFICATION OPEN**  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**
> 实施证据：[`audit/full_system_2026_08_24/32-fs-002-short-lived-trading-permission-lease.md`](../../audit/full_system_2026_08_24/32-fs-002-short-lived-trading-permission-lease.md)

## 1. 业务目标与边界

Phase 3A 已保证 Gateway 在 halt 未获 execution 同 generation acknowledgement 时不谎报 enforced，并让 execution 最终提交读取 Redis generation/state。但当 Gateway 无法向 Redis 写 halt、NATS command/state 也完全失败，而 execution 仍能读取旧 RUNNING 记录时，旧许可没有时间上界。本阶段把风险增加权限改为控制面短期续租：旧 RUNNING generation 只有同时存在同 generation 的未过期 Redis permission key 才可越过最终提交边界；控制面失联后租约自动过期。

本阶段不声称解决网络层物理分区、未来多 execution membership/全员 ack、真实 OKX reduceOnly 语义或目标容器告警。租约只收紧风险增加订单，不阻断经账户事实验证的 reduce-only/cancel 降险动作。

## 2. 当前行为与根因

`risk_increasing_submission_guard()` 当前校验本地状态和 30 天 TTL 的 `aats:hot:system:kill_switch` RUNNING generation。该 key 是恢复权威，不是短期在线许可；Gateway 双传输故障时无法改变旧记录，execution 可继续读取并提交。根因是持久恢复状态与在线交易授权复用同一长寿命事实。

## 3. 模块职责与领域模型

| 模块 | 职责 |
|---|---|
| Gateway/monolith `KillSwitch` | 读取权威 RUNNING 状态并续租；HALTING/HALTED/DEGRADED 时停止续租并尽力撤销本 generation key |
| execution `KillSwitch` | 绝不续租；最终 fence 同时读取长期权威状态和短期 permission lease |
| Redis | 用服务器 TTL 自动删除许可；持久 kill-switch state 与短期 permission key 分离 |
| `ApplicationRuntime` | readiness 后启动控制面 lease task；作为 service-owned critical task 监督；shutdown 时停止 |
| Gateway health / daemon | lease task 异常复用 FS-006 critical failure 路径 |

permission payload 只含 `generation`、`issued_by` 和非权威诊断时间；安全性依赖 Redis TTL 与 generation key，不依赖跨主机墙钟判断。

## 4. 输入与输出接口

新增 key：`aats:hot:system:kill_switch_permission:<generation>`。固定 TTL 15 秒、续租间隔 5 秒。`start_trading_permission_lease()` 仅在 `gateway`/`monolith` 且 authority-loss fail-closed 的 runtime 启动；返回 service-owned task。`risk_increasing_submission_guard()` 在权威 RUNNING 校验后读取当前 generation key，缺失、非法、错 generation 或读取失败均抛固定 `KillSwitchSubmissionBlocked` reason。

公共 HTTP/API shape 不变。snapshot 增加 lease owner/running/last-success 等非秘密诊断字段。

## 5. 数据库 Schema、表、索引与约束

无 PostgreSQL schema、表、索引、约束或 migration。Redis 新 key 是瞬态许可，不写 event store，不作为重启恢复真源。旧版本不会读取它；因此应用升级前仍保持 live 禁用，不允许滚动混跑新旧 execution。

## 6. 事务、一致性与并发

最终 fence 继续跨越唯一不可逆 `place_order` 调用。租约读取在同一 fence 内、长期 authority generation 校验之后完成。续租前同时要求本地 RUNNING/resume-authorized 和 Redis 长期 authority 完全匹配；halt 先本地 HALTING，使并发 renew 不再延长权限。Redis TTL 提供过期线性化，不用进程墙钟决定许可有效性。

## 7. 授权、认证与数据安全

只有现有 admin/operator resume 路径能产生新的 RUNNING generation；租约不能创建或切换 generation。execution、market、decision 不具备续租权。payload/log 不含凭证、账户、订单、token 或 `.env.*` 内容。

## 8. 错误处理与幂等

- authority read/set 失败：不续租；旧 key 最迟 TTL 到期；
- lease read 失败/缺失/非法：风险增加提交立即失败关闭并锁存 DEGRADED；
- 重复续租同 generation：幂等刷新 TTL；
- generation 改变：旧 generation key 即使暂存也不能授权新 intent；
- halt：停止续租并 best-effort 删除当前 generation key，删除失败仍由 TTL 收敛；
- resume：长期 authority 先持久化；许可由控制面续租建立，在此之前继续拒单；
- shutdown/crash：不在 shutdown 续租，Redis 自动过期。

## 9. 状态转换与生命周期

```text
RUNNING authority + gateway renew task
  -> permission key TTL=15s, refresh every 5s
  -> execution final fence requires authority + same-generation key

halt / gateway crash / renew partition
  -> no further refresh
  -> best-effort delete or Redis TTL expiry
  -> execution risk-increasing submit blocked
```

HALTED/DEGRADED 不创建许可；restart 后即使长期 authority 为 RUNNING，也必须等控制面重新建立 lease 才能风险增加。

## 10. 缓存与性能

每次风险增加真实提交多一次本地 Redis GET；Gateway 每 5 秒一次 authority GET + permission SET。当前单 execution、低频订单架构可接受。不能缓存 lease 到 execution 本地越过 Redis TTL，因为这会重新打开分区窗口。

## 11. 日志、监控与审计

记录 lease renewed、renew failed、revoked、expired/missing、generation mismatch 与 task failure；只含 role/generation/error type/TTL。连续续租失败不得写成功事件。operator/readiness 后续可读取 snapshot，但本阶段不把代码字段冒充目标告警已送达。

## 12. 测试策略

纯内存确定性覆盖：正常续租允许提交；没有 lease 拒绝；错 generation 拒绝；lease TTL 到期后旧 RUNNING authority 仍拒绝；Gateway 双传输 halt 失败后停止续租并在 TTL 后阻断；execution 不能启动续租；halt 撤销；resume 在新 lease 前拒绝、续租后允许；renew authority mismatch 不刷新；task stop 不续租；reduce-only 保持可用。复跑 FS-002、KillSwitch、adapter、lifecycle、Gateway health、Ruff 和全量 unit。

## 13. 迁移、回滚与兼容

无 DB migration。该协议对旧 execution 不兼容：新 Gateway 不能证明旧 execution 会校验 lease，因此 live 保持硬禁用，未来只能整批同 revision 部署。回滚会恢复无限期 RUNNING 权限，不是安全生产回滚；隔离环境若需回滚必须保持 halt/live disabled。

## 14. 配置与环境隔离

TTL/renew interval 先作为代码安全常量，不开放环境覆盖，防止被配置成无限长。InMemory store 单测使用相同 TTL 语义。paper/backtest 或 authority-loss 非严格 runtime 不要求 lease，避免把研究环境误标为 live authority failure。

## 15. 代码组织与依赖

预计修改 KillSwitch lease 生命周期与最终 guard、ApplicationRuntime 启停/critical 注册、FS-002 测试、现行文档与追加审计。不新增第三方依赖，不改交易 API，不触碰 OrderState 三层持久化。

## 16. 文档、运维手册、部署与验收标准

验收要求：控制面失联后许可在 15 秒上界内消失；旧 RUNNING 记录不能单独授权；execution 永不自续；halt/delete 失败仍由 TTL 收敛；新旧 generation 隔离；focused/related/Ruff/full unit 通过。未执行真实 Redis/NATS/Compose 分区和时间上界故障注入前，FS-002 只能标 `PARTIALLY REMEDIATED / TARGET PARTITION-EXPIRY VERIFICATION OPEN`，G1 保持未放行，真实资金继续 **NO-GO**。

实施结果：上述代码与纯内存验收已完成；四进程代理 resume 也已收紧为 Gateway
成功激活同 generation permission 后才返回。28 项 FS-002 focused、225 项扩大相关和
`4312 passed, 30 skipped, 1666 warnings, 85 subtests passed` 的全量 unit 通过。
真实 Redis/NATS/Compose 目标分区与时间界仍未执行，因此目标裁定和 NO-GO 不变。
