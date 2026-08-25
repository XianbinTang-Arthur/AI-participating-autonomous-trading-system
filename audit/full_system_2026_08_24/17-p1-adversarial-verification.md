# 17 P1 对抗性复核

> 复核日期：2026-08-24  
> 代码基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`（`main`）  
> 总体裁定：**REAL-MONEY PRODUCTION: NO-GO**  
> 工作性质：第二阶段只读复核；未修复代码，未修改应用配置、迁移、依赖或测试。

## 1. 复核原则与证据边界

本轮不是为首轮结论寻找佐证，而是逐项尝试推翻原 P1。每项均重新检查调用方、下游门禁、异常处理、补偿控制、配置条件、测试以及可导致资金损失的完整时序。裁定词含义如下：

- `CONFIRMED`：反证检查后，原 P1 严重度仍成立。
- `DOWNGRADED`：缺陷成立，但首轮对可达性或影响作了过强推断。
- `UPGRADED`：新增证据证明影响高于首轮。
- `INVALIDATED`：核心事实不成立。
- `REQUIRES RUNTIME VERIFICATION`：静态证据不足以作严重度裁定；不能解释为安全。

证据强度分层：

- **静态已验证**：由当前 checkout 的代码、Compose、部署脚本和测试直接证明。
- **隔离动态已验证**：使用替身组件做确定性故障注入，不连接交易所、不提交订单、不写生产数据库。
- **只读运行态已验证**：仅检查本机现有容器、HTTP 可达性和 PostgreSQL 容量/连接统计。
- **未知**：真实交易所、live 账户、宿主防火墙、远端网络、生产峰值、灾备恢复均未在本轮验证。

## 2. 总体结果

| 结果 | 数量 | Finding |
|---|---:|---|
| UPGRADED | 1 | `FS-002`：P1 → P0 |
| CONFIRMED | 5 | `FS-001`、`FS-003`、`FS-006`、`FS-007`、`FS-009` |
| DOWNGRADED | 3 | `FS-004`、`FS-005`、`FS-008`：P1 → P2 |
| INVALIDATED | 0 | 无 |
| REQUIRES RUNTIME VERIFICATION | 0 | 无单项以此作为最终状态；若干 P2 仍要求运行验证 |

以下按资金安全优先级排列，先给出 Kill Switch 裁定。

---

## 3. FS-002 — Kill Switch 传播失效与最终提交竞态

- **Original Finding ID**：`FS-002`
- **Final Status**：`UPGRADED`
- **Final Severity**：**P0**
- **Confidence**：高（静态全链路 + 两个隔离确定性复现；未向交易所发单）
- **Exact Code Path**：
  - 人工 halt API：`aats/api/routes.py:222-243`
  - 系统查询/网关调用：`aats/services/operator/reconciliation_system_queries.py:806-858`
  - 本地 halt 与跨进程发布：`aats/services/governance_engine/kill_switch.py:172-183,447-538`
  - 执行进程 NATS 更新：`aats/services/governance_engine/kill_switch.py:544-607`
  - Redis 仅启动水合：`aats/services/governance_engine/kill_switch.py:198-332`
  - command 前置门禁：`aats/bootstrap/config.py:4495-4512`
  - adapter 门禁与网络提交：`aats/services/execution_engine/okx_adapter.py:289-344,1107-1143`
  - OrderManager 本地门禁：`aats/services/execution_engine/order_manager.py:174-243,505-520`

### Evidence

1. 人工 halt API 调用的是 **Gateway 进程本地** `KillSwitch.halt_async()`；接口响应不等待 execution 进程确认，也没有返回已确认进程集合或传播 generation。
2. `halt_async()` 先设置本地内存状态，再尽力写 Redis 和发 NATS。两条传播路径的失败均只记录日志，没有让调用失败或进入“未确认”状态。
3. execution 进程只在启动时从 Redis 水合；运行中没有连续轮询。正常运行时的远端变更依赖 NATS handler 更新本地状态。因此 Gateway 的 Redis/NATS 发布同时失败后，execution 可无限期保留 `halted=False`，而 API 调用方得到正常返回。
4. 即使传播成功，OKX adapter 在 `submit()` 早期读取本地 kill 状态，之后等待异步最大尺寸检查，再直接 `place_order()`；网络提交紧前没有 generation/fence 或二次检查。
5. reduce-only/close 绕过 halt 是合理的降险控制；本 finding 只针对风险增加订单。
6. 隔离故障注入复现了传播失败：注入 Redis 与 NATS 同时失败后，handler 正常返回，Gateway `halted=True`，execution `halted=False`。
7. 隔离竞态复现了最终提交窗口：在异步最大尺寸门禁暂停期间激活 halt，恢复后仍发生一次 `place_order` 替身调用，最终状态为 `SUBMITTED`。

### Counterevidence Checked

- 决策、command processor、OrderManager 和 adapter 存在多层 kill 门禁，但它们都读取各进程本地状态；不能弥补跨进程副本未更新。
- `tests/integration/test_kill_switch_cross_process.py:152-177` 覆盖共享内存总线和可用 Redis 的成功路径，不是四进程真实 NATS，也没有覆盖 Redis/NATS 同时失败或 acknowledgement。
- 启动时 Redis 不可用存在 fail-safe 路径，但这不覆盖进程已运行后的 halt 发布故障。
- 稳定 client order id、模糊响应后的查询确认可降低 timeout 重复提交风险，但不能阻止一个在 halt 后新发出的风险增加订单。
- 未发现交易所侧 cancel-all、账户级冻结或受 API ack 约束的最终 fencing，可作为此路径的可靠补偿控制。

### Failure Timeline

```text
t0  execution 本地 kill state = RUNNING；风险增加 command 已排队或即将进入 adapter
t1  operator 通过 Gateway 触发 halt；Gateway 本地立即变为 HALTED
t2  Redis 写入失败，NATS 发布失败；异常被记录但 API 正常返回
t3  execution 未收到事件，也不会持续轮询 Redis，因此本地仍为 RUNNING
t4  order 通过 command/OrderManager/adapter 的本地门禁
t5  adapter 完成异步额度检查；期间即便 execution 随后收到 halt，提交前也无 fence/recheck
t6  client.place_order() 被调用，风险增加订单进入交易所
t7  不同 symbol 的执行可并行，影响不局限于理论上的单一订单
```

### Capital Impact

操作员在认为系统已停止后，系统仍可能继续建立新敞口；当 Redis/NATS 故障持续时，该状态没有内建收敛时限。若叠加行情剧烈波动、多个 symbol 并行 command 或 operator 因“halt 成功”停止外部干预，可能造成不受预期风控约束的连续敞口和重大资本损失。这符合 P0 的“紧急资金控制失效并可继续非预期交易”定义。

### Why Original Severity Was Correct or Incorrect

首轮识别的最终提交 TOCTOU 是正确的，但把最坏影响限定为“一笔订单”过于乐观。二审补全 API → 本地状态 → Redis/NATS → execution 副本链路后，证明 halt 可以被报告成功而执行进程完全不进入 halted 状态；因此由 P1 升为 P0。

### Required Verification Test

在完全隔离、交易所 client 为严格 spy 的四进程拓扑中：分别注入 Redis 故障、NATS 丢包/断连、两者同时故障、延迟/乱序事件和 execution 重启；并在 command claim、额度 await、网络提交前各点激活 halt。要求风险增加订单 outbound 调用数恒为 0、所有 execution worker 返回同一 halt generation 的确认、API 在缺确认时不得报告完成；同时验证 reduce-only 仍可执行。真实资金环境禁止作为首次验证场所。

---

## 4. FS-001 — RDP Profile rollback 报告终态但未回滚有效参数

- **Original Finding ID**：`FS-001`
- **Final Status**：`CONFIRMED`
- **Final Severity**：**P1**
- **Confidence**：高（静态全链路）
- **Exact Code Path**：
  - Profile rollback handler：`aats/api/rdp_profile_routes.py:538-571`
  - 独立参数 rollback endpoint：`aats/api/rdp_routes.py` 中 `/rdp/parameters/rollback`
  - active parameter 装载：`aats/bootstrap/config.py:5138-5143`
  - active set 读取：`aats/services/governance_engine/active_parameters.py:875-930`

### Evidence

1. `POST /profile-recommendations/{id}/rollback` 校验 token、actor 与双人审批后，只把 `governance.recommendations.status` 更新为 `rolled_back`。
2. 请求中的 `to_parameter_set_id` 不参与有效参数写回；响应同时返回顶层 `ok: true`、终态 `rolled_back` 和 `pending_live_rollback: true`。
3. 没有沿此 handler 写回 active parameter set、live payload、apply history 或 worker 内存，也没有 runtime readback。
4. 当前 active parameters 在 runtime bootstrap 阶段装载；未发现该 profile rollback 路径触发热加载或进程重建。
5. 另一个 `/rdp/parameters/rollback` endpoint 的存在不能修复本 endpoint 的成功语义；两个动作没有事务/saga 关联。

### Counterevidence Checked

- token、双人操作员和审计字段可以限制谁能调用，但不能让 rollback 生效。
- `pending_live_rollback:true` 暴露了未完成事实，但不能抵消 HTTP 2xx、`ok:true` 与数据库终态 `rolled_back` 对客户端/操作员造成的成功语义。
- 当前静态 UI 中未找到该 endpoint 的直接调用者，降低了内置 UI 的即时可达性；外部 API 仍可调用，且服务端契约本身不真实。
- 独立参数 rollback 路径有真实写入逻辑，但不会被 profile rollback 自动调用。

### Failure Timeline

```text
t0  live/runtime 正使用参数集 A
t1  operator 对推荐记录发起 rollback，目标参数集为 B
t2  handler 只把推荐记录标为 rolled_back，并返回 ok=true
t3  active set、live payload 和 worker 内存仍为 A
t4  operator/自动化按“已回滚”判断风险已解除
t5  策略继续用 A 生成决策，偏离操作员预期
```

### Capital Impact

错误参数可在操作员误以为已撤销后继续影响风险与策略决策，延长异常敞口窗口。调用需要授权且当前内置 UI 未发现调用方，故不足以单独升级为 P0；但任何真实资金上线都不能接受回滚成功语义与有效状态分离。

### Why Original Severity Was Correct or Incorrect

原 P1 正确。二审找到权限与明确 pending 字段等补偿信息，却没有找到有效状态改变、运行时传播或读回验证；核心失败路径完整保留。

### Required Verification Test

在隔离数据库与假 worker 中建立 A→B apply 历史，调用 profile rollback，逐项断言 recommendation、active set、live payload、apply history、worker runtime 参数和 API operation state 一致；在写回、发布、worker ack 任一点故障时必须保持 pending/failed，不能报告 rolled_back。

---

## 5. FS-003 — 同根 K 线前视与同收盘价成交

- **Original Finding ID**：`FS-003`
- **Final Status**：`CONFIRMED`
- **Final Severity**：**P1**
- **Confidence**：高（静态时间语义 + 现有单元测试契约）
- **Exact Code Path**：
  - 当前 bar 入历史并打分：`aats/data_platform/replay/adapters/independent_adapter.py:91-103,199-251`
  - 同循环决策后成交：`aats/data_platform/replay/backtest/harness.py:228-248,296-320`
  - IOC/post-only 成交模型：`aats/data_platform/replay/backtest/fill_simulator.py:205-228`
  - OKX candle `confirm` 解析与丢失：`aats/data_platform/normalizers/okx_normalizer.py:44-75,363-376`

### Evidence

replay adapter 先把当前 bar 加入 history，再使用该 bar 的 open、high、low、close、volume 计算信号；harness 随即在同一循环把订单交给 fill simulator。IOC 按该 bar close 加减固定 1 bp，并按 100% 数量成交。live 行情中，最终 high/low/close/volume 只有 candle 完成时才确定；normalizer 虽解析 OKX `confirm`，转换后的 market kline 没有保留可供下游强制“仅闭合 candle”判断的字段。

**数值例子：**15 分钟 bar 为 `open=100, high=111, low=99, final close=110`。replay 在知道完整 `+10%` bar、最终 high/low/volume 后产生买入，并以 `110 × 1.0001 = 110.011` 成交。live 只有在 bar 结束后才能确认这些最终值；若下一可交易报价为 112，则真实可达价格相对回测差约 `112 / 110.011 - 1 = 1.8089%`。这不是 1 bp 参数误差，而是观察时点与成交时点倒置。

### Counterevidence Checked

- 若策略显式只使用上一根闭合 candle 并在下一可交易事件成交，可避免前视；当前 replay 路径并非如此。
- 固定滑点、手续费和 fill policy 只能调成本，不能修复“知道最终 bar 后回到同一 close 成交”的因果错误。
- live feed 可能发送 evolving/unfinished candle；这使当前语义更不等价，而非补偿控制。
- 没有发现该回测结果直接自动晋升 live 的链路，因此资本损失需要“人或治理流程采用错误证据”这一环节；这支持维持 P1而非 P0。

### Failure Timeline

```text
t0  15m candle 开始，live 只知道 open 与演化中的 OHLCV
t1  replay 已拿到最终 high/low/close/volume，并据此生成动作
t2  replay 仍以同一 candle 的 final close ±1bp 成交
t3  live 到 candle close 才得到等价信息
t4  live 的最早可交易事件价格已变化，可能跳空或流动性消失
t5  回测收益/风险统计被系统性抬高，错误证据进入上线判断
```

### Capital Impact

策略可能因虚假的收益、回撤或成交能力证据被分配真实资本。影响不是确定单笔损失，而是整个候选评价基础被污染，可导致长期错误选模和规模配置。

### Why Original Severity Was Correct or Incorrect

原 P1 正确。二审重构了精确观察/决策/成交时序，并找到 candle `confirm` 未进入下游契约；没有补偿机制把成交推迟到下一可交易事件。

### Required Verification Test

建立带 `open_time`、`close_time`、`confirm`、decision timestamp、submit timestamp 和下一 quote 的 golden replay；断言任何使用 candle 最终值的信号都不能在该 candle 的 final close 成交。对未闭合 candle、跳空、延迟、部分成交、无流动性和下一 bar open 做确定性对照，并将旧绩效证据按执行模型版本失效。

---

## 6. FS-004 — Research Factory 的 test 使用边界

- **Original Finding ID**：`FS-004`
- **Final Status**：`DOWNGRADED`
- **Final Severity**：**P2**
- **Confidence**：中高（当前代码路径明确；历史人工试验行为需审计产物验证）
- **Exact Code Path**：
  - 60/20/20 分段：`aats/data_platform/research_factory/real_data.py:121-123`
  - test 指标与 gate：`aats/data_platform/research_factory/real_data.py:491-552`
  - baseline 计算：`aats/data_platform/research_factory/benchmarks/baseline.py:19-80`
  - 行数证据：`aats/data_platform/research_factory/evidence.py:281-295`

### Evidence

runner 构造 train/valid/test，但当前候选评价只对 `test_rows` 计算 factor、future return、metrics 和 deterministic gate，随后生成 candidate/recommendation；train/valid 主要进入行数/证据检查，而非训练或稳定性判断。这是数据治理缺陷，`test` 名称与实际“候选评价集”角色不一致，也没有封存的最终 OOS 或 purged walk-forward。

### Counterevidence Checked

- 该具体函数没有训练模型、超参数搜索、feature selection、ensemble、early stopping 或在多个候选间自动排名；首轮把“在 test 上直接选模”表述得过强。
- baseline 消费预先给定的 factor/label；dataset 对象只需存在，并未从 train 拟合参数后再以 test 选择模型。
- 找到 recommendation、人工 design、dry-run 和 observation gate，没有找到由该 test 指标自动发布到 live 的直接路径。
- research allocation policy 在当前静态调用图中未找到应用入口；不能据其存在推断自动资金分配。
- 仍无法从代码排除操作员反复修改 factor/threshold 后重跑并观察同一 test；历史 artifact/registry 未做运行态审计。

### Failure Timeline

```text
t0  系统固定切分 60/20/20，但 train/valid 不参与稳定性或选择
t1  人工提出 factor/threshold，runner 在命名为 test 的同一段输出指标与 gate
t2  人工查看结果并修改方案，再次运行（是否发生需查历史 artifact）
t3  若重复发生，该 test 实际成为 validation
t4  recommendation 仍缺少独立封存 OOS 证据，人工上线判断可能过拟合
```

### Capital Impact

若团队重复查看同一 test 并据此迭代，最终绩效会有选择偏差；但当前代码没有证明自动搜索或自动 live 晋升，资本影响需要额外人工行为。因此是重要研究治理风险，但不独立构成 P1。

### Why Original Severity Was Correct or Incorrect

原始事实“train/valid 未参与，test 直接产生指标与 recommendation”正确；原严重度对自动选模和直接资金路径推断过强。降为 P2，但不能把 `test` 结果视为最终 OOS。

### Required Verification Test

只读审计 experiment/candidate/recommendation registry、proposal lineage、factor/threshold 变更和同一 dataset window 的查看次数；随后用封存 OOS 与 purged walk-forward 重算。若证实自动/高频适配同一 test，或该指标可无独立验证直接进入 live，严重度应重新升级。

---

## 7. FS-005 — Gateway 绑定、TLS 与登录传输

- **Original Finding ID**：`FS-005`
- **Final Status**：`DOWNGRADED`
- **Final Severity**：**P2**
- **Confidence**：高（代码/Compose + 当前模拟运行态）；生产网络边界为 UNKNOWN
- **Exact Code Path**：
  - Base Compose Gateway：`deploy/wsl2-dev/docker-compose.aats.yml:106-123`
  - live TLS 证书生成：`scripts/deploy.sh:208-238`
  - uvicorn SSL 参数：`scripts/compose_entrypoint.py:61-83`
  - live 启动安全门禁：`aats/bootstrap/config.py:1535-1570`
  - session cookie：`aats/config/settings.py:732-743`
  - HTTP auth transport guard：`aats/api/auth_routes.py:132-145,1148-1192`

### Evidence

Base Compose 仍令 Gateway 监听 `0.0.0.0`，宿主端口映射未限定 `127.0.0.1`。本轮只读运行态再次观察到当前模拟 derivatives 容器对 `0.0.0.0` 和 `[::]` 发布 8001，HTTP health 可达。这证明暴露面配置事实成立。

### Counterevidence Checked

- live deploy 会生成 TLS 证书并通过 entrypoint 注入 uvicorn SSL 参数；不是“生产默认必然明文 HTTP”。
- exchange/live 启动配置要求 auth，限制不安全 write access，并要求 Secure cookie（开发模拟例外）。
- auth transport guard 会阻止不安全 HTTP 登录；cookie 具有 Secure/HttpOnly/SameSite 控制。
- 当前观测的是 **simulated derivatives** overlay，不是 live，不能用其 HTTP 200 证明生产登录明文暴露。
- 未找到反向代理；自签证书 SAN 仅覆盖 localhost/127.0.0.1，与远端主机名访问可能不匹配。
- 宿主防火墙、VPN、路由/NAT 和局域网可达性未验证。仅凭 `0.0.0.0` 不能证明互联网可达或凭证已泄露。

### Failure Timeline

```text
t0  live Gateway 映射到所有宿主接口
t1  若宿主防火墙/VPN/路由没有限制，远端可到达控制面端口
t2  TLS 可能因自签与主机名不匹配被绕过/错误信任，或操作路径退回开发配置
t3  攻击者获得更大的认证探测、DoS、会话攻击面
t4  只有再叠加认证缺陷/凭证失陷，才形成直接控制或资金影响
```

### Capital Impact

暴露控制面扩大攻击与凭证风险半径，但当前代码有 live TLS、认证与 cookie 门禁，且没有证据证明生产主机公网可达。因此它是必须验证和收敛的纵深防御问题，不足以仅凭开发运行态保持 P1。

### Why Original Severity Was Correct or Incorrect

首轮正确识别 all-interface bind，却把当前模拟 HTTP 状态过度外推到生产。生产路径具备明确 TLS/auth 补偿控制，故降为 P2；生产网络与证书信任未验证，不能降为“安全”。

### Required Verification Test

在真实目标网络但不连接交易所的部署副本中，从本机、局域网、VPN 外和非授权网络分别探测端口；验证 HTTPS 强制、证书 SAN/信任、HTTP 登录拒绝、Secure/HttpOnly/SameSite cookie、Host 校验、认证限流和防火墙规则。保存端口绑定与防火墙只读证据，不读取/展示凭证。

---

## 8. FS-006 — 关键业务 task 死亡而容器保持 healthy

> Phase 3D 状态说明：本节保存 Phase 2 修复前裁定与复现。当前未提交工作区已让
> 显式关键 task 的 exception/cancel/提前返回触发 heartbeat 停止、daemon 非零
> 退出或 FastAPI health `503`。Phase 3K 又让七条固定周期关键循环的永久 await/
> 连续无成功周期在 deadline 后分类为 `stalled`；事件驱动任务、整体 event-loop
> stall、真实依赖和容器 restart 仍未验证。当前状态与新证据见 `24`、`31`，不要
> 把下文原始代码路径当作修复后现状。

- **Original Finding ID**：`FS-006`
- **Final Status**：`CONFIRMED`
- **Final Severity**：**P1**
- **Confidence**：高（静态全链路 + 隔离确定性复现）
- **Exact Code Path**：
  - runtime task 创建：`aats/bootstrap/config.py:561-712`
  - process lifecycle：`aats/bootstrap/process_lifecycle.py:377-504`
  - Compose heartbeat health：`deploy/wsl2-dev/docker-compose.aats.yml:161-216`
  - Gateway healthz：`apps/api_gateway/main.py:257-268`

### Evidence

runtime 将 account WS、reconciliation、account refresh、execution sync、command flow 等关键循环创建为后台 tasks，没有统一 critical supervisor 或 done callback。`run_process()` 另起独立 heartbeat 后只等待 OS stop signal；业务 task 异常不会令进程退出，也不会停止 heartbeat。Compose health 只检查 heartbeat 文件新鲜度；Gateway `/healthz` 主要证明 lifespan 存活。

隔离复现中，注入的 critical business task 已结束并携带异常，heartbeat 文件仍存在且 process 仍在等待；外部停止后进程甚至正常退出。这证明 orchestrator 无法仅由当前 health contract 发现业务死亡。

### Counterevidence Checked

- Docker restart policy 仅在进程退出后生效；此处进程不退出。
- shutdown 阶段会 await tasks，但发现异常太晚，且不是运行期监督。
- 日志可能记录 task 异常，但日志不是 fail-closed、readiness 或自动 restart。
- 某些业务组件有自己的重试循环；这不能覆盖 task 自身未捕获异常或永久退出。
- 未发现 Compose health 调用组件级 last-success、lag 或 command consumer 状态。

### Failure Timeline

```text
t0  execution/decision 进程启动业务 tasks 与独立 heartbeat
t1  account WS、reconciliation 或 command consumer 因未捕获异常退出
t2  task 异常无人监督，主 coroutine 继续等待 OS signal
t3  heartbeat 独立刷新，Compose 持续报告 healthy
t4  deploy/operator 依据绿色容器误判 trading-ready
t5  下单、成交处理、对账或风险快照的一部分静默停止
```

### Capital Impact

可造成隐藏的死交易组件、无法及时降险、漏处理成交/状态漂移或错误的 readiness 判断。具体组件死亡的直接影响不同，但“关键业务已死而系统宣称健康”在真实资金系统中是硬阻断项。

### Why Original Severity Was Correct or Incorrect

原 P1 正确。二审通过故障注入复现了 business dead + heartbeat alive + process waiting 的完整状态，未找到 supervisor 或 readiness 补偿控制。

### Required Verification Test

对每个标记为 critical 的 task 注入未捕获异常、永久 hang 和依赖断连；断言系统在有界时间内 halt 并 readiness=false，或进程非零退出触发 restart。健康检查必须校验 task identity、last success、lag、restart count 和依赖状态，不能只看心跳文件。

---

## 9. FS-007 — 部署 readiness 与一致回滚不足

- **Original Finding ID**：`FS-007`
- **Final Status**：`CONFIRMED`
- **Final Severity**：**P1**
- **Confidence**：高（脚本/Compose 静态全路径；未执行部署）
- **Exact Code Path**：`scripts/deploy.sh:44,125-138,397-413,426-507,551-567`；`deploy/wsl2-dev/docker-compose.aats.derivatives-live.yml`

### Evidence

唯一部署入口默认 profile 为 `derivatives-live`。流程先 down 旧栈，再 build/up；没有保留并验证可用旧应用/旧 schema 的一致回滚单元。app up 非零仅警告并继续，最终成功条件主要是 Gateway `/healthz` 和 required container health。required app list 不含 derivatives-live 的 liquidations/microstructure collectors。

脚本没有形成可审计 trading-readiness packet：未核对 schema identity/compatibility、交易所连通性与模式、account/market freshness、critical worker last-success、recovery/reconciliation、execution 进程 kill state/ack、活动参数 identity、command/outbox backlog、所有 live-only daemon，也没有验证 app 与 schema 可一致回退。`bash -n` 只证明语法有效，不证明部署正确。

### Counterevidence Checked

- live startup 对 profile、auth、cookie 等有 fail-fast 门禁。
- root schema 有 migration ledger/checksum 和数值列校验；这些是局部补偿，不构成整体 schema identity/readiness。
- TLS 生成与五个主要服务 health 检查降低部分错误上线概率。
- container health 可能捕获进程级崩溃，却受 `FS-006` 的假健康限制。
- 没有发现部署后的受控 exchange read-only probe、对账 clean 证明或 risk-control readback。

### Failure Timeline

```text
t0  operator 未显式指定 profile，脚本选择 derivatives-live
t1  旧栈先被 down，随后 build/up 部分失败
t2  app up 错误只产生 warning，脚本继续
t3  Gateway 与 required containers 通过浅层 health；遗漏 daemon/业务 task/状态仍异常
t4  脚本报告完成，operator 认为可交易
t5  系统以 schema/行情/账户/风控未知或部分服务缺失状态接入 live
t6  若需回退，没有经过验证的 app+schema 一致回滚路径
```

### Capital Impact

可把部分故障、未知状态或错误 profile 带入真实资金环境，并在失败后造成长时间停机或不一致恢复。它不是某一个 bug，而是上线决策缺少可证明的安全门禁。

### Why Original Severity Was Correct or Incorrect

原 P1 正确。二审找到了若干 startup hardening，但均不足以证明 trading-ready，也没有覆盖故障后的 app/schema 一致回滚。

### Required Verification Test

在隔离克隆环境演练成功部署、build 失败、app up 部分失败、schema 不兼容、collector 缺失、stale market/account、dirty recovery、kill state 不一致和回滚失败。每个异常必须使部署非零且不得给出成功结论；成功产物需包含 commit/image/schema/profile/parameter identity 和所有关键 worker/风险状态的读回证据。

---

## 10. FS-008 — PostgreSQL 连接预算

- **Original Finding ID**：`FS-008`
- **Final Status**：`DOWNGRADED`
- **Final Severity**：**P2**
- **Confidence**：中高（静态上限与当前只读采样可靠；生产峰值未测）
- **Exact Code Path**：
  - 主池：`aats/storage/session.py:209-258`
  - 各进程构建 storage：`aats/bootstrap/config.py:5103-5112`
  - RDP：`aats/data_platform/db.py:18-27`
  - live query：`aats/data_platform/runtime/live_query_adapter.py:99-105`
  - live facts：`aats/data_platform/live_facts/db.py:59-65`
  - live session：`aats/data_platform/runtime/live_session.py:81-95`
  - governance：`aats/services/governance_engine/_db_util.py:118-123`

### Evidence

静态最大连接预算如下；这是允许上限之和，不是同步实际需求：

| 来源 | 进程/实例假设 | 每实例上限 | 理论连接 |
|---|---:|---:|---:|
| 四个主 runtime storage | 4 | 15 + 45 | 240 |
| RDP research engine | 1 | 5 + 10 | 15 |
| live query | 1 | 3 + 5 | 8 |
| live facts | 1 | 3 + 5 | 8 |
| live session RW + RO | 1 | 5 + 4 | 9 |
| governance | 1 | 2 + 3 | 5 |
| 两个 live collector 的独立 RDP pool | 2 | 5 + 10 | 30 |
| execution orderbook read pool | 1 | 1 + 1 | 2 |
| **稳态理论合计** |  |  | **317** |
| 四进程 active-parameter transient pool | 4 | 1 | **+4，至 321** |

迁移/admin 连接还可能叠加。当前 PostgreSQL 只读值为 `max_connections=200`、`superuser_reserved_connections=3`，普通连接可用上限约 197。只读采样为 40 个 activity（active 1、idle 34、其他/无状态 5）；当前是 simulated derivatives，未运行两个 live collector，不能代表生产峰值。

可信生产峰值只能作带假设估计，而非测量事实：

| 峰值来源 | 假设并发连接 |
|---|---:|
| 四主进程基础池 | 60 |
| Gateway 高并发 overflow | +45 |
| execution burst | +10 |
| RDP/live/governance 活跃连接 | +20 至 +30 |
| 两个 collector | +2 至 +10 |
| admin/migration/recovery 余量 | +5 |
| **估计区间** | **约 142 至 160** |

该区间低于普通连接上限 197，但只有约 37 至 55 的名义余量，且没有负载、慢查询、故障恢复或连接泄漏实测。

### Counterevidence Checked

- 首轮 `4 × 60 > 200` 把各池硬上限相加，证明“无全局预算”，但不能证明四个进程会同时打满 overflow。
- 当前运行值 40 明显低于容量上限，是反证的一部分；但它是非 live 的单点采样。
- QueuePool overflow 按需创建，并非启动即占满。
- 不同 pool 可能连接不同数据库，但仍共享同一 PostgreSQL 实例的 `max_connections`。
- 未发现全局 semaphore、PgBouncer 或按服务配额，可确保总连接低于数据库预算。

### Failure Timeline

```text
t0  正常负载约占用基础连接
t1  Gateway fan-out、慢查询、execution 对账与 RDP/collector 同时升高并发
t2  各进程独立扩张 overflow，不知道全局剩余额度
t3  普通连接接近 197，恢复/admin 请求与新业务连接开始超时或被拒绝
t4  控制面、对账和 execution 同时退化，健康与降险能力受影响
```

### Capital Impact

连接耗尽可拖慢或阻断控制、执行、成交处理和对账，但从当前配置无法证明可信峰值必然超过 197；需要较高并发、慢查询或故障叠加。因此降为 P2 容量/可用性风险，仍需作为 live 容量门禁验证。

### Why Original Severity Was Correct or Incorrect

原 finding 正确指出理论上限不受全局预算约束；原 P1 将“理论可达上限超过 200”近似为“可信生产峰值超过 200”，证据不足。加入按负载角色估算和运行采样后，降为 P2。

### Required Verification Test

在生产等价、无真实交易所写入的隔离栈中启用全部 live daemon，压测 Gateway、execution、对账、RDP、collector，并注入慢查询、DB 短断和 worker 重启。记录每服务 active/idle/overflow、pool wait、timeout、rejected、PostgreSQL 内存和为恢复/admin 保留的余量；峰值与故障恢复期间均需有明确容量阈值。

### Phase 3U Current Workspace Overlay

原始证据和 Phase 2 降级结论保留为历史快照。当前未提交工作区已新增
`aats/storage/connection_budget.py`，把四进程主 pool ceiling 从 4×60 改为
gateway/market/decision/execution 的 32/8/10/16，并集中定义 RDP、collector、governance、
live session 和 orderbook 配额。14 个声明 topology component 合计 150；按当前 Compose
200/3 计算，普通容量 197、名义余量 47。

标准库 verifier 对 13 个 application `create_engine` 调用建立 AST inventory，禁止未归类
engine、裸 pool 数字、未批准 pool root、短命持久池和 Compose/CI 漂移。该变化消除了“各
模块独立硬编码且总量无自动校验”的一部分根因。

但 150 不是数据库全局 runtime cap：governance transient、并行 NullPool CLI/replay、
migration/recovery/admin、仓库外进程和 topology 实例漂移仍可能叠加；目标负载、慢查询、
故障重连、pool wait/timeout、告警和 `work_mem` 联合内存均未验证。因此当前状态更新为
`PARTIALLY REMEDIATED / DECLARED TOPOLOGY BUDGETED / TARGET LOAD & TRANSIENT PATHS OPEN`，
P2 和 required verification test 不变。证据见
[41-fs-008-database-connection-budget.md](41-fs-008-database-connection-budget.md)。

---

## 11. FS-009 — 多套 schema 管理机制产生同 revision 漂移

- **Original Finding ID**：`FS-009`
- **Final Status**：`CONFIRMED`
- **Final Severity**：**P1**
- **Confidence**：高（静态机制清单与启动异常路径）
- **Exact Code Path**：
  - root `create_all` + versioned migration：`aats/storage/session.py:261-355`
  - 每进程 startup schema：`aats/bootstrap/config.py:1728-1768`
  - RDP `create_all`：`aats/data_platform/db.py:53-63`
  - RDP schema/bootstrap DDL：`aats/data_platform/rdp_models.py:1882-1899`
  - Batch B 手工 DDL：`migrations/_batch_b.py:42-...`
  - RDP daemon fail-fast：`scripts/rdp_task_daemon.py:516-525`
  - Gateway 吞并 RDP migration error：`apps/api_gateway/main.py:115-127`

### Evidence

当前至少存在以下 schema 形成机制：

| 机制 | 入口 | 能力/边界 |
|---|---|---|
| root ORM `create_all` | `session.py` / runtime startup | 建缺失表，不等价于版本化 ALTER/constraint/view |
| root versioned migrations | `session.py` | 有 revision/checksum/advisory lock，但与 RDP 体系分离 |
| RDP ORM `create_all` | `data_platform/db.py`, `rdp_models.py` | 建七个 schema/表，并执行少量 bespoke ALTER helper |
| Batch B 手工 SQL/Python DDL | `migrations/_batch_b.py` | 含 ALTER、VIEW、CHECK、精度扩展等，不由 RDP `run_migrations()` 完整执行 |
| daemon startup | `rdp_task_daemon.py` | 运行 RDP create_all，失败可阻断 daemon |
| Gateway startup | `apps/api_gateway/main.py` | RDP migration 异常被捕获并继续启动 |
| 测试初始化 | 多处 fixture/create_all | 可形成仅满足 ORM 的测试 schema，不能证明生产 schema 等价 |
| deploy | `scripts/deploy.sh` + app startup | 未有独立、单一、可回退 schema migration job |

同一个 Git revision 下，“新库仅运行 ORM create_all”和“历史库执行过 Batch B”可能具有不同的 views、checks、precision、columns/constraints。root validation 主要检查有限数值列，不能证明全 schema identity。Gateway 吞 RDP migration 异常也证明部分服务可以在 RDP schema 未就绪时继续绿色。

### Counterevidence Checked

- root migration ledger/checksum/advisory lock 是有效补偿控制，但只覆盖 root migration 集，不统一 RDP Batch B。
- RDP daemon 对自己的 `run_migrations()` fail-fast；Compose required/health 可捕获该 daemon 完全起不来。但 `run_migrations()` 本身仍不保证 Batch B 全量 DDL 已应用。
- `create_all` 幂等地创建缺表，适合测试/初始化便利，却不会把既有列、约束、view 修正为目标 revision。
- 未发现部署前比较表、列、类型、精度、constraint、index、view 与目标 manifest 的完整校验。

### Failure Timeline

```text
t0  环境 A 从空库执行 ORM create_all；环境 B 历史上另执行 Batch B
t1  两者都运行相同 commit，表名大体存在，浅层 startup/health 通过
t2  A 缺少某 ALTER/CHECK/VIEW/精度变化，或 Gateway 吞掉 RDP 初始化异常
t3  研究、治理或 active parameter 路径在特定数据上失败或接受非法状态
t4  部署没有 schema manifest 识别差异，也没有一致 rollback
t5  不完整/错误治理数据影响策略批准、运行参数或恢复判断
```

### Capital Impact

schema 漂移可破坏治理约束、活动参数、研究证据或状态查询，并可能在表“存在”且部分服务健康时潜伏。其资金影响通常需叠加特定数据/功能路径，故维持 P1 而非 P0。

### Why Original Severity Was Correct or Incorrect

原 P1 正确。二审盘点了所有主要 schema 机制和 daemon/Gateway 的不同失败姿态；现有 ledger 与 daemon health 未能消除同 revision 不同 schema 的事实。

### Required Verification Test

从空库、当前生产克隆、缺少某 Batch B 步骤的库和部分失败库分别前滚至相同 revision，导出并比较 schema manifest（schema/table/column/type/nullability/default/precision/index/constraint/view/function）。演练失败、重试与 app+schema 回滚；任何不一致、吞错或部分 ready 都必须阻断部署。

---

## 12. 复核结论

二审没有为真实资金上线提供放行依据。相反，`FS-002` 已从单一进程内 TOCTOU 扩展为“控制面报告 halt 成功，但 execution 可能完全未停”的跨进程 P0。三个降级项只表示首轮严重度证据被修正：

- `FS-004` 仍不能把当前 `test` 当作封存 OOS；
- `FS-005` 仍需在目标网络证明生产暴露与 TLS 边界；
- `FS-008` 仍需通过全 live 拓扑的容量/故障压测。

### Phase 3V Current Workspace Overlay

原始证据与 Phase 2 降级裁定保持历史有效。当前 real-data v2 runner 已改为 train/valid
分别评价并要求双门通过，valid 形成 candidate metrics；test 只参与输入质量/来源检查，
生成内容 seal 和 `sealed_not_evaluated` 状态，不进入 factor、label、绩效 metrics 或
selection gate；execution summary
必须精确绑定 valid，不能覆盖 test。57 项 focused
隔离回归覆盖 test timestamps 不进入 evaluator、train fail/valid pass 不生成 candidate、
holdout-only 内容变化只改变 seal，以及 candidate/recommendation lineage。

这不把 test 升级为已通过的封存 OOS，也不能证明 v1 artifact/人工研究历史未污染。
最终一次性 OOS、访问账本、purged walk-forward、多重检验、production gate 与独立复核
仍 OPEN。当前状态和关闭标准见
[42-fs-004-research-selection-holdout.md](42-fs-004-research-selection-holdout.md)。

这些降级不抵消 `FS-001/002/003/006/007/009` 的硬阻断结论，也不代表未测试的 live 状态安全。

## 13. Phase 3E FS-009 后续整改边界

本文第 11 节保留 Phase 2 的修复前全路径证据。Phase 3E 当前工作区已将该路径收紧为：部署期一次性 root+RDP schema job；root/RDP exact version+checksum ledger；RDP 13 个 canonical Batch B stage 的 advisory lock、predecessor、同事务 DDL+ledger 与 rollback-suffix contract；managed 应用 validate-only；Gateway 在任何 build/readiness/background side effect 前失败关闭。修复中还识别并纠正了 one-shot job 不能复用非法 `rdp-daemon` process role 的兼容问题。

16 项 focused、220 项相关回归和 4,186 项全量 unit 通过。真 PostgreSQL integration 及相关独立 SQL integration 共 7 项只收集/跳过，未连接数据库。由于空库、历史克隆、缺 stage、部分失败库的完整 manifest 全等与 app+schema rollback 仍未演练，当前裁定为 `PARTIALLY REMEDIATED / CLONE MANIFEST & ROLLBACK OPEN`，P1 hard blocker/G6 仍未放行。见 `25-fs-009-schema-single-truth-remediation.md`。

该实施也局部改善 FS-007：镜像构建现在位于 down 之前，schema job 失败位于 app up 之前且由 `set -e` 中止。默认 live profile、app up 非零后继续、缺完整 trading-readiness packet、两个 live-only collector 未纳入必需门、无旧 app/schema/parameter 一致回滚等主要路径未改，所以 FS-007 仍是 P1 OPEN。

## 14. Phase 3F FS-007 后续整改边界

本文第 9 节保留 Phase 2 的修复前全路径证据，Phase 3E 末尾也保留当时状态。Phase 3F 当前工作区已取消部署默认 profile，只允许显式 `spot`/`derivatives` 模拟 profile；三个 live profile 在任何 WSL/Docker/DB 副作用前硬失败且无 override。down、infrastructure up 与 app up 非零不再被吞，future live required-container contract 已补 liquidations/microstructure collector。新增的模拟 evidence 记录 commit/image/container 身份，但明确 `production_ready=false`、`trading_ready=false` 和所有运行态 unknown。

FS-007 独立 11 项、首轮 deploy focused 34 项与扩大后的 73 项 deploy/process/startup/FS-009 相关回归通过；最终全量 unit 为 4,197 passed、30 skipped、1,666 warnings、85 subtests passed。没有执行 WSL2/Docker/数据库/交易所，也没有读取真实账户状态。完整 trading-readiness packet、critical hang/lag、账户/行情新鲜度、Kill Switch generation/ack、活动参数、recovery/reconciliation、网络/容量、克隆部署故障矩阵和 app+schema+parameter 一致 rollback 仍未完成。

因此原 P1 finding 不关闭，当前裁定更新为 `RISK CONTAINED / LIVE DISABLED / READINESS & CONSISTENT ROLLBACK OPEN`。G5 从原始 FAIL 更新为 `PARTIAL / 未放行`，只表示标准入口的意外 live 风险已被隔离，不表示 production ready。见 [26-fs-007-deployment-fail-closed-remediation.md](26-fs-007-deployment-fail-closed-remediation.md)。

## 15. Phase 3G FS-005 后续整改边界

本文第 7 节保留 Phase 2 修复前证据与 P2 降级理由。Phase 3G 当前工作区已把 Gateway 宿主端口从 all-interface mapping 固定为 `127.0.0.1`，保持 container 内 listener 仅供 Docker network；本地 `start_api.py` 只接受模拟 profile 与 loopback host。模拟 deployment evidence 新增实际 Docker published binding 读取，缺失、格式错误、空/all-interface 或非 loopback HostIp 均失败。76 项 related 与 4,219 项全量 unit 通过。

没有重建或 inspect 任何容器，也没有做目标网络探测。因此现有 HostIp、Windows/WSL 防火墙、LAN/VPN/NAT、公网可达性、TLS 证书 SAN/信任、HTTP 强制、Host/auth/cookie/限流仍为 UNKNOWN。当前裁定为 `CODE REMEDIATED / TARGET NETWORK VERIFICATION OPEN`；FS-005 保持 P2，G7 不放行。见 [27-fs-005-gateway-loopback-containment.md](27-fs-005-gateway-loopback-containment.md)。

## 16. Phase 3H FS-020 后续整改边界

本文与 `09-security-review.md` 保留 Phase 1/2 缺失安全头的原始证据。Phase 3H 当前工作区已在 Gateway 最外层 user middleware 实施固定本机 Host allowlist，非法/不信任 Host 路由前 400；对 HTML、JSON、HTTPException/认证失败和 Host 400 统一覆盖严格 CSP、frame/nosniff/referrer/permissions/COOP/CORP，HSTS 仅在实际 HTTPS scope 输出。当前 UI 无 inline script/style，不需 `unsafe-inline`/`unsafe-eval`。

初版实施曾因内部 `::1` 与 HTTP `[::1]` 归一缺陷导致 21 failed/18 passed，首次修正后 40 项 focused 通过；后续复核又拒绝了多重尾点与非 IPv6 方括号 Host，最终为 44 focused 和 4,252 full unit passed、30 skipped、1,666 warnings、85 subtests passed。全量的两次 Windows 临时目录基础设施失败也已保留在 `28`。

没有运行真实 TLS terminator/proxy/browser，也没有证明框架最外层未捕获 500 响应带头。因此当前裁定为 `CODE & ASGI REMEDIATED / TARGET TLS-BROWSER VERIFICATION OPEN`；FS-020 保持 P2，G7 不放行。见 [28-fs-020-browser-security-headers-remediation.md](28-fs-020-browser-security-headers-remediation.md)。

## 17. Phase 3I FS-019 后续整改边界

本文保留 Phase 1/2 对 auth DoS 的原始代码证据。Phase 3I 当前工作区已将
`POST /auth/login` 的同步 Operator repository、PBKDF2、账户失败/成功状态和审计写入
完整移入 cancel-safe 有界 worker；每 app/loop 默认最大并发 4、排队 1 秒。请求取消后
capacity 只在真实 worker 完成时释放。worker 创建前还有每进程 60 秒 global/client/
identity 60/20/10 限流，client 只取 ASGI socket；不存在/禁用/损坏 hash 走 dummy KDF，
输入和 hash iteration 有上界。

初版 focused `3 failed, 61 passed` 暴露 QueryService 构造仍在 event loop，修正后 64
通过；最终 131 related、4,273 full unit 与 Ruff 通过。没有运行真实 PostgreSQL、多个
Gateway worker、trusted proxy/Redis 集中限流、慢 DB/连接耗尽、目标 p95/p99、
event-loop lag 或告警故障注入。

因此当前裁定为 `CODE REMEDIATED / DISTRIBUTED RATE-LIMIT & LOAD VERIFICATION OPEN`；
FS-019 保持 P2，G7 不放行。见
[29-fs-019-operator-login-async-isolation.md](29-fs-019-operator-login-async-isolation.md)。

## 18. Phase 3J FS-016 后续整改边界

本文与 `05-architecture-review.md` 保留 Phase 1/2 对 INTEREST + peer readiness fail-open 的原始证据。Phase 3J 当前工作区已将 gateway/market/decision/execution 的 hybrid/nats barrier 改为 generation-scoped strict gate；缺 generation/hot-state、Redis announce/poll 异常和 peer timeout 都在 publisher 前失败。标准模拟 deploy 在 sync 后/build 前生成同一非秘密 generation，Compose 必填注入，ready key/payload 和模拟 evidence 同时绑定。

首轮聚焦回归 113 项通过；加固底层不变量与 evidence 后，129 related、4,286 full unit、Ruff、shell 语法和 YAML 静态解析通过。没有运行 WSL2/Docker/Redis/NATS，也没有执行 peer 延迟、断连、新部署/重启和 INTEREST 消息计数矩阵。

因此当前裁定为 `CODE REMEDIATED / TARGET NATS STARTUP-RESTART VERIFICATION OPEN`；FS-016 保持 P2，G5 不放行。见 [30-fs-016-nats-peer-readiness-remediation.md](30-fs-016-nats-peer-readiness-remediation.md)。
