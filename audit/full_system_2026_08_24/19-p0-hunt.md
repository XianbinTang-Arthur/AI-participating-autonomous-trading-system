# 19 相邻 P0 组合狩猎

> Phase 3M 更新：本文件 FS-001 可达链冻结的是修复前证据。当前 profile apply 与
> rollback 都已改为授权后无写入 `501`，不再形成新的虚假终态；但真实
> execution-owned activation/reverse saga、worker readback 与历史 applied 数据对账
> 仍未完成，所以 FS-001 保持 P1 HARD BLOCKER。现行证据见 `22`、`33`。

> 日期：2026-08-24；基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`。  
> 目标：在原 P1 周边寻找能导致持续非预期交易、重复订单、不可降险、错误仓位真相或灾难性资本暴露的组合路径。  
> 结论：确认一个 P0，即升级后的 `FS-002`；没有另建重复 finding。其他组合未达到 P0 证明标准，但保留明确未知项。

## 1. P0 判定门槛

本轮仅在以下条件同时成立时确认 P0：

1. 当前代码存在可达的完整因果路径，而非仅有两个风险名词并列；
2. 现有门禁/恢复/交易所 identity 不能在损失前可靠收敛；
3. 结果可形成持续或大范围非预期市场暴露、重复订单、无法紧急止损，或同等级资金灾难；
4. 不以“操作员连续违反明确流程”作为唯一触发条件；
5. 静态证据或隔离替身可证明关键状态变化，不要求拿真实资金复现。

## 2. 已确认 P0：Kill Switch race + 传播失败 + queued/new work

> Phase 3L 更新：本节冻结修复前 P0 证明。Phase 3A 已增加 execution
> generation acknowledgement 与最终 submission fence；Phase 3L 又以同 generation
> 15 秒 Redis permission 收紧双传播故障后的旧 RUNNING 权限。真实 Redis/NATS
> 四进程目标时界和独立复核仍 OPEN，故 P0 不降级、不关闭。见 `21`、`32`。

- **对应 finding**：`FS-002`（不新建编号）
- **结论**：P0 / `UPGRADED`
- **核心组合**：Gateway 本地 halt 成功语义 + Redis/NATS best-effort 且无 execution ack + execution 无持续 Redis 轮询 + adapter 最终提交前无 fence/recheck。
- **确定性复现结果**：
  - 发布故障：handler 正常返回；Gateway halted；execution 仍未 halted。
  - adapter 竞态：在异步额度门禁期间激活 halt；随后仍产生一次 `place_order` spy 调用并进入 `SUBMITTED`。
- **为何是 P0**：紧急停止是资本安全最后防线。此路径可令操作员认为已停，但执行进程仍接受 queued/new 风险增加命令，且不同 symbol 可并行；故障持续时间没有内建上限。
- **边界**：未向真实交易所发单；这不降低结构性 P0 的严重度。详见 [17-p1-adversarial-verification.md](17-p1-adversarial-verification.md#3-fs-002--kill-switch-传播失效与最终提交竞态)。

## 3. 组合逐项审查

### 3.1 Kill switch race + retry behavior

**假设攻击链：** halt 竞态允许首次提交，随后 transport timeout 触发重试，造成重复风险订单。

**查到的控制：**

- command/order 使用稳定 identity 与 idempotency key；
- OKX adapter 对模糊 transport 结果先以 client order id 查询确认；
- startup recovery 对 `CLAIMED/SENT/PENDING` 等不确定状态倾向 halt 并要求对账，而不是直接重发；
- reduce-only 有意保持可用。

**裁定：** 未额外证明“重复订单 P0”。`FS-002` 本身已是 P0，因为不需要 retry 叠加即可令 halt 失效。需要在隔离 exchange stub 中覆盖 timeout-before-accept、accept-before-timeout、query-not-found 延迟和进程硬崩溃窗口；当前不能把未证明重复解释为不可能。

### 3.2 Duplicate submission + timeout ambiguity

**怀疑路径：** `CLAIMED` command 超时后被重新领取并创建新 venue order。

**反证：** command recovery/startup recovery 对不确定执行状态采取 halt/人工交易所对账；adapter 以稳定 `clOrdId` 查询模糊结果。首轮 `FS-013` 已因此撤回，二审未发现绕过该恢复姿态的新调用方。

**裁定：** 未确认 P0，亦不恢复 `FS-013`。剩余未知是“进程在交易所接单后、持久化前硬崩溃且查询暂时不可见”的真实 venue 行为，应做故障注入，但不能凭假设新增 P0。

### 3.3 Partial fill + restart

**怀疑路径：** 部分成交已发生，进程重启后本地数量回退，重复补单或错误对冲。

**反证/控制：** 成交与订单有幂等 identity，启动恢复和 reconciliation 会检查不确定状态，并可触发 halt；账本/组合链路以持久事实和交易所对账收敛，而不是只依赖内存。

**裁定：** 未确认 P0。当前审计没有做“每个持久化边界硬崩溃”的 end-to-end fault matrix，故仓位、fee sign、部分成交累计和重复事件仍需隔离验证。若发现 restart 自动补发而不先对账，应立即重评为 P0。

### 3.4 Rollback illusion + live strategy execution

**可达链：** `FS-001` 的 profile rollback 返回 `ok:true/rolled_back`，而 runtime 继续使用旧参数；策略继续执行。

**补偿与条件：** 调用需要 token/双人操作员；响应包含 `pending_live_rollback:true`；当前内置 UI 未发现直接调用者；还需要操作员/外部 client 忽略 pending 字段并继续授权 live。

**裁定：** 保持 `FS-001` P1，不升级 P0。它有明确资本损失路径，但持续非预期交易依赖额外的人机误判，且不像 Kill Switch 那样破坏紧急停止最后防线。

### 3.5 Schema drift + failed migration

**可达链：** ORM `create_all`、root migration 与手工 RDP Batch B 可生成不同 schema；Gateway 可吞 RDP migration 异常并继续。

**补偿：** root ledger/checksum/advisory lock；RDP daemon 对自身初始化 fail-fast；Compose 会检查该 daemon 健康。缺口是 daemon 的 `run_migrations()` 仍不等价于完整 Batch B，且没有全 schema manifest。

**裁定：** `FS-009` 保持 P1。未证明 schema 漂移可直接绕过交易执行硬风控或自动形成灾难性敞口；通常还需特定缺失约束/数据和治理路径叠加。若活动风控参数的关键 constraint/column 可缺失而 execution 仍 ready，应重新评估 P0。

> Phase 3E 更新：上述可达链是修复前快照。当前工作区已用显式
> root+RDP job、Batch B ledger/checksum/transaction/order 契约和 Gateway 启动前
> validate-only 阻断 create_all/manual/吞错组合路径。但真 PostgreSQL 克隆 manifest
> 与 app+schema rollback 未证，所以仅更新为 `PARTIALLY REMEDIATED / OPEN`，
> 不改变 P1 严重度也不改变 NO-GO。见 `25`。

### 3.6 Stale market data + risk-increasing order

**搜索重点：** stale quote/candle 是否可继续驱动新开仓，adapter 是否仅做形式检查而没有 freshness。

**查到的控制：** decision/risk 路径存在行情 freshness/staleness guard，adapter 还有 reference/slippage 与 instrument/size 检查；未知状态原则上可阻断风险增加动作。

**缺口：** 本轮没有在完整四进程拓扑中冻结 market consumer、保持 heartbeat、再观察 queued command 与 adapter 的端到端结果；不同数据产品的 freshness contract 也未逐一做动态证明。

**裁定：** 未确认新 P0。它与 `FS-006` 假健康组合是 P1 邻近风险；必须在隔离拓扑做 stale/partition/clock-skew 测试。任何“stale 且仍能新开仓”的实证都应至少升级为 P1，并结合规模/持续性判断 P0。

### 3.7 Heartbeat false positive + dead order consumer

> Phase 3D 更新：下述 `task 结束但 heartbeat 继续` 原始路径已由显式 critical
> supervisor 阻断；Phase 3K 又为七条固定周期关键循环加入成功进度 deadline。
> 事件驱动任务、整体 event-loop stall、真实依赖与容器行为仍未验证，故 P1 finding
> 保持 OPEN 而非关闭。详见 `24`、`31`。

**可达链：** command consumer task 异常退出，独立 heartbeat 继续，容器保持 healthy。

**资本方向分析：** dead consumer 通常停止消费新下单命令，单独看不会持续新增订单；但它会隐藏无法执行降险命令、成交/对账 task 死亡或恢复状态过时。不同 critical task 的后果不能混为一谈。

**裁定：** `FS-006` 保持 P1，不升级 P0。若故障的是 fill/reconciliation/position truth consumer，并且另一路仍可提交新单，则可能构成 P0 组合；当前隔离复现证明“死而绿”，没有证明该特定双路径同时可达。

### 3.8 Test contamination + automated promotion to live

**搜索结果：** Research Factory 在命名为 test 的分段直接形成 metrics/gate/recommendation，但当前具体路径没有训练/超参搜索/自动排名，也未找到 recommendation 自动发布到 live 或自动资金分配的调用链。

**裁定：** 不构成 P0；`FS-004` 降为 P2。若运行 artifact 证明大量自适应同一 test，严重度可上调；若再发现无人工/独立 OOS 门禁的自动 live promotion，则应单独重审资本影响。

## 4. 未确认但必须保留的高风险未知

| 未知项 | 为什么当前不能下安全结论 | 最小安全验证 |
|---|---|---|
| 交易所接单后响应丢失 + 进程硬崩溃 | 替身测试不能完全代表 OKX 查询可见性/幂等窗口 | demo/严格模拟 venue，稳定 clOrdId，分阶段断电，禁止真实资金 |
| partial fill 跨事务边界恢复 | 未覆盖 fill/order/ledger/lot/portfolio 每个落库点的崩溃矩阵 | 隔离 DB + 可脚本化 exchange stub + 重放/对账不变量 |
| stale market + 旧 queued command | 静态 freshness guard 未证明所有进程/数据类型/时钟偏差下生效 | 四进程冻结/分区/clock-skew 故障注入 |
| execution task 部分死亡而 submission 仍活跃 | `FS-006` 证明可死而绿，但未穷举 task 依赖组合 | 按 criticality 逐 task kill，观察 admission、halt 与 readiness |
| live host 网络边界 | 当前只观察 simulated 栈，宿主防火墙/VPN 未审 | 目标网络只读端口与 TLS/认证测试 |

## 5. P0 狩猎结论

本轮不能声称“系统只有一个 P0”。准确结论是：**在已审范围和当前基线，确认一个 P0（`FS-002`），其余指定组合没有达到 P0 的证据门槛；未执行的交易所、故障恢复和生产网络验证仍是 UNKNOWN。**

`FS-002` 已完成 Phase 3A/3L 设计、代码与隔离修复验证，但独立复核和真实 Redis/NATS 四进程目标分区时界仍未完成；在这些证据关闭前不得进入真实资金上线流程，也不得用人工操作规范替代 execution acknowledgement、最终提交 fencing 与短时 permission。
