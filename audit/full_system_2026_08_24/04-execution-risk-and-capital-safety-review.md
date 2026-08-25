# 04 执行、风控与资金安全审查

> Phase 3M 状态边界：FS-001 的 profile apply/rollback 错误成功入口均已失败关闭，
> 但真实双向运行时变更、worker readback 与历史对账仍未实现；FS-001/G2 继续 OPEN。
> 详见 `22`、`33`、`20`。

## FS-002 — kill switch 在最终网络提交前存在 TOCTOU 竞态

> 后续状态：本节是 Phase 1/2 修复前 finding。Phase 3A generation/ack/final fence
> 见 `21`；Phase 3L generation-scoped 15 秒短时交易许可见 `32`。真实 Redis/NATS
> 四进程分区、目标 TTL 时界与独立复核仍 OPEN，当前状态不是 CLOSED。

- 严重度：P1；置信度：高；类别：capital safety / concurrency
- 状态：VERIFIED（静态时序）；未向交易所复现
- 位置：`aats/services/execution_engine/okx_adapter.py:289-344,1107-1111`
- 证据：`_submission_gate_error()` 在 311 行读取 kill switch；随后执行语义检查、异步 `_max_size_gate_error()` 和滑点检查；344 行直接调用 `client.place_order()`，没有最终复查。reduce-only/close 有意允许通过，风险增加动作才受影响。
- Phase 2 现行裁定：**P0 / UPGRADED**。二审新增证明 halt API 不等待 execution ack，Redis/NATS 双传播失败可在 API 正常返回后让 execution 无限期保留旧状态，并以隔离替身复现最终提交竞态。首轮证据保留；完整链路见 `17-p1-adversarial-verification.md`。
- 触发：风险增加订单通过首次检查后，在异步额度/网络前置阶段内 kill switch 被 halt。
- 后果：暂停后仍可能有一笔增加敞口的订单到达交易所。
- 调用方/测试复核：OrderManager、command processor 和 adapter 有多层初始检查；现有测试验证“检查时已经 halt”会阻断，没有覆盖检查后切换。
- 建议：在 outbound call 紧前复查，并以 monotonic halt generation/fencing token 贯穿 command；halt 后旧 generation 的风险增加命令不可提交。保留降低敞口动作的明确例外和测试。

## 订单状态与模糊提交

已审链路的主要顺序是：持久化 order/command → claim → adapter submit → 记录 SENT/ack/order state → fills/outbox/portfolio。进程可能在交易所接受订单后、数据库记录 SENT 前崩溃，形成 `CLAIMED` 模糊状态。当前恢复实现会 halt 并要求交易所对账，而不是自动重复提交；这是正确的 fail-closed 控制。

仍需验证：

- `clOrdId` 在所有入口、重试、父子单和恢复路径是否全局稳定且交易所侧唯一；
- REST timeout、HTTP 5xx、WS 重连和迟到 fill 的组合故障注入；
- 部分成交后撤单、反向成交、reduce-only 被交易所拒绝、position mode 漂移；
- 真实账户模式、杠杆、保证金币种与 instrument metadata 新鲜度。

## 风控层级

| 层 | 已确认控制 | 主要剩余风险 |
|---|---|---|
| Strategy/permission | family、rollout、budget、sleeve permission | 参数来源漂移、回放/生产差异 |
| Policy/RiskEngine | kill switch、health、notional、position、loss/guard | 跨进程缓存新鲜度、原子性 |
| Command flow | 持久化、claim、恢复扫描 | 某些 profile 的 effective flag 需 runtime truth 证明 |
| OrderManager | intent/leg 检查、obligation、对账 | 并发目标、迟到事件、退出路径 |
| OKX adapter | 环境、dry-run、submit enable、symbol、open orders、notional、max size、slippage | 最终 gate 竞态；交易所响应模糊 |
| Recovery | pending/CLAIMED/SENT 检测、halt、人工 review | 部署 health 不验证 recovery clean |

## 组合、成交与账本

- Portfolio fill handler 使用串行锁、checkpoint/restore、fill outcome 去重，并在数据库事务中原子写 snapshot/outcome/outbox。
- Execution outbox 将 order/fill 事实与待发布事件保存在同一事务；fill ID/唯一约束用于幂等。
- 启动可从 fills/ledger/snapshot 重建组合并生成 recovery status。
- fee 在已审 UI/组合路径中区分成本与 rebate；未发现直接符号反转，但未覆盖每一条资产/合约分支。

## 上线必须满足的不变量

1. halt 之后不得有旧 generation 的风险增加 submit；降低敞口动作必须被明确识别。
2. 每个 venue order 在本地只有一个稳定 identity；模糊结果先对账、后决策。
3. order、fill、ledger、lot、portfolio 与 reconciliation 的数量/方向/费用可闭环。
4. 任何 stale/unknown account、instrument、price、position mode、recovery 状态都阻断开仓。
5. RDP/策略参数回滚只有在运行值读回验证后才可报告成功。

当前 `FS-001`、`FS-002`、`FS-003` 和部署 readiness 缺口意味着这些不变量尚未被系统级证明。
