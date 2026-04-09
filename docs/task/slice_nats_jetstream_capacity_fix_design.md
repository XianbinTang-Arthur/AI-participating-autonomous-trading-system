# Slice: NATS JetStream 容量根因修复设计

**状态**：待批准
**作者**：Claude (Opus 4.6)
**日期**：2026-04-08
**前置 slice**：`slice_docker_compose_hardening_fix_design.md`（已完成，4 commits）
**关联未提交工作**：Slice 6.5 `ensure_stream` 幂等 upsert 修复（1 处 + 4 单测，作为本 slice commit 1 前置提交）

## 1. 背景与病根

### 1.1 病状

4 进程 docker-compose 拓扑启动 derivatives profile 真跑后，`aats-market` 容器稳定喷下列日志：

```
CRITICAL [aats.market_gateway] okx_market_message_error
  consecutive_errors=920
  error=nats: ServiceUnavailableError: code=503 err_code=10023
  description='insufficient resources'
```

market gateway 心跳仍在跑（heartbeat 文件正常），但向 NATS 发布 `aats.market.snapshots` 全部失败。决策链路因 market 数据断流而饿死，订单永远触发不了。这是 **阻塞用户原始诉求「观察系统是否能正确下单」的根堵点**。

### 1.2 根因链条（5 层）

| 层 | 证据 | 结论 |
|---|---|---|
| **L1 Server 限额** | `deploy/wsl2-dev/nats/nats-server.conf` L20 `max_file_store: 1GB` | 单节点 JetStream 文件存储的硬上限 1 GB |
| **L2 Stream 容量策略缺失** | `aats/bus/nats_bus.py:367` `StreamConfig(..., retention=LIMITS, discard=OLD, max_age=7d)` 只设时间维度 | `max_bytes = max_msgs = -1`（无限），stream 自身不设容量兜底，carry 给 L1 server 硬上限才会爆 |
| **L3 数据来源** | `DEFAULT_CRITICAL_TOPICS` 包含 `market.snapshots` / `feature.snapshots`（高频 tick 推送），每秒级写入 | 7 天保留期内很容易吃满 1 GB |
| **L4 遗留堆积** | 运行时 `stream_info`: 451,979 条消息 / ~1 GB（平均每条 ~2.4 KB） | 多次历史观察运行的累积噪音已经把 stream 写到 server 上限边缘 |
| **L5 故障表现** | NATS server log: `JetStream resource limits exceeded for server` + client `err_code=10023` | publish 永久失败 → event bus 停摆 → 决策链路饿死 |

### 1.3 根因总结

**只设 `max_age` 等于把所有容量控制都压到 server 级硬限额**。L1 是"强刹车"（碰到就整个 NATS 停服），L2 是"软刹车"（碰到 stream `discard=OLD` 会自动丢最老的消息） —— 当前配置缺失了软刹车层，一次意外的高频写入或长期观察运行就会把软刹车跳过直接撞强刹车。

同时 `ensure_stream` 当前比较维度只看 `subjects`（Slice 6.5 修复时引入），不看容量参数 —— 即使本 slice 把 max_bytes 加进 `NatsBusConfig`，stream 已存在且 subjects 没变时会走 unchanged 分支，新容量设置永远应用不到现存 stream。这是一个**必须同时修的次级隐患**。

## 2. 目标与非目标

### 2.1 目标（本 slice 必做）

- **T1**：消除 "insufficient resources" 启动阻塞，让 4 进程拓扑可以稳定连续跑数周不炸。
- **T2**：stream 容量"软刹车"先于 server 硬上限触发，碰到容量顶时 `discard=OLD` 自动丢最老的消息。
- **T3**：容量配置可观测（启动期日志打 stream_bytes / max_bytes / pct_used；runbook 有 jsz 水位抓取步骤）。
- **T4**：`ensure_stream` 容量参数能被应用到现存 stream（不只是新建 stream）。
- **T5**：遗留 1 GB 历史数据的清理路径写清楚，有幂等可回滚的 migration 步骤。
- **T6**：改动面严格受限、各工作包独立可回滚。

### 2.2 目标补充（用户反馈后扩入）

- **T7**：**分层 stream 归属**：把高频观察/派生 topic（`market.snapshots` / `feature.snapshots`）拆到独立短保留 stream `AATS_EVENTS_MARKET`，与长保留 `AATS_EVENTS` critical stream 隔离，彻底解决"高频挤占长保留窗口"的问题（之前设计接受的 3.1 天 tradeoff 被用户拒绝）。

### 2.3 非目标（故意不做，避免 scope creep）

- **N2**：**不**引入 NATS 集群化 / 副本。单节点 dev 足够；生产阶段再谈。
- **N3**：**不**改 `DEFAULT_CRITICAL_TOPICS` 是否进 JetStream 的顶层归类（关键 vs 观察者路由）—— 本 slice 只在 critical 内部再做二次分层（长保留 vs 短保留），不动 observer 那半。
- **N4**：**不**在本 slice 修复 `market.snapshots` 的高频写入本身（可能有降采样/压缩优化空间）；容量兜底 + discard=OLD 先打通底盘，性能调优留下一轮。
- **N5**：**不**做 consumer ack lag 管理（durable consumer 没 ack 导致 stream 不能回收）—— 当前没证据显示 ack lag 是主因，待真跑继续观察。
- **N6**：**不**把其他高频 observer-like topic（`portfolio.snapshots` / `account.baselines` / `portfolio.balance_deltas`）也拆到 MARKET stream —— 这些承载资金状态，需要 7 天合规回放，保持在 long-retention stream。本 slice 只做 `market.snapshots` + `feature.snapshots` 两个明确的纯观察/派生项拆分，严格受控避免归属漂移。
- **N7**：**不**改动现有集成测试/单测的 `bus.start(topics=...)` / `bus.ensure_stream(topics=...)` 调用姿势（除 1 行 T2 例外）；通过 `NatsEventBus.ensure_stream` legacy shim 保留向后兼容，发 DeprecationWarning 提示未来再迁移。

### 2.4 用户决策汇总

本 slice 的关键设计决策来自用户多轮交互反馈，记录在此便于审查：

| # | 问题 | 用户决策 | 落地位置 |
|---|---|---|---|
| D1 | Slice 6.5 `ensure_stream` 未提交 diff 是否作为本 slice commit 1 前置？ | **B**（整合为本 slice commit 1） | §5 / §6 |
| D2 | server `max_file_store` 升到多少？ | **γ (8 GB)** | §4.1 / §8.1 |
| D3 | MARKET 是否拆独立 stream？ | **独立 stream**（拒绝 3.1 天 tradeoff 方案） | §2.2 T7 / §4 分层设计 |
| D4 | dev 环境是否可 purge 老 AATS_EVENTS？ | **接受 purge**（所有环境分库分表） | §9.2 / §11.5 |
| D5 | StreamSpec 是否加 `num_replicas` / `duplicate_window` / `deny_purge` 三个扩展字段？ | **你来决定** → 加，用安全默认值 | §7.1 |
| D6 | Legacy shim 是发 DeprecationWarning 还是硬 raise？ | **你来决定** → DeprecationWarning（不硬 raise） | §7.4 / §7.5 |
| D7 | 是否先调研所有 caller 再定设计？ | **查清楚再设计** | §7.5a (11 个 caller 全列) |
| D8 | T6 诡异状态（MARKET 存在但 EVENTS 不存在）是 warn 恢复还是 raise？ | **要**（raise） | §9.2 / §9.4 |
| D9 | MARKET vs EVENTS 的 `max_msg_size` 是否对称？ | **保持对称**（都是 4 MB） | §4.3.2 / §4.4 / §7.2 / I-11 |

**用户的核心诉求**："这些决策都你来做 关键是保证系统稳定能正常工作"

本设计通过以下方式实现稳定保证：
1. **不变量 I-1~I-11** 全部有单测 / 运行时 / 文档三层保护
2. **两条路径独立**（runtime 新 API + 测试 legacy shim）避免耦合风险
3. **T6 raise** 暴露诡异状态而不是掩盖
4. **对称 max_msg_size** 避免运维认知偏差
5. **Migration 脚本幂等** 可重跑可回滚
6. **9.5 真跑烟测 8 步** 覆盖 pre-check / dry-run / apply / restart / 启动日志 / 15 分钟观察 / 水位 / 下单链路

## 3. 不变量

本 slice 改动必须始终满足以下不变量：

| ID | 内容 | 验证手段 |
|---|---|---|
| **I-1** | **所有 stream** 的 `max_bytes` 之和严格小于 server 级 `max_file_store`（留 ≥15% 索引/overhead 预留） | unit test 断言 `sum(spec.max_bytes for spec in specs) < server_max_file_store * 0.85` |
| **I-2** | `ensure_streams` 在 stream 已存在时，容量参数差异也会触发 `update_stream`（不只是 subjects 差异） | 新增 unit test `test_ensure_streams_updates_when_max_bytes_differs` |
| **I-3** | `StreamSpec` 所有容量字段都有合理默认值；env var 可覆盖；非法值（负数 / 0）在构造期 raise | 新增 unit test 3 条 |
| **I-4** | 每个 stream 的容量策略都用 `discard=OLD` + `max_bytes` 双保险；**任何 stream 都不依赖** server 硬上限抗压 | diff 审查 + 真跑 stream_info 打印 |
| **I-5** | Slice 6.5 `ensure_stream` upsert 三分支行为（created / unchanged / updated）向后兼容；收尾事件 `nats_jetstream_stream_ensured` 保留（每个 stream 各 emit 一次） | 已有 4 个 Slice 6.5 单测全绿 + 本 slice 新增单测扩展 |
| **I-6** | 遗留数据 migration 脚本幂等：跑 N 次效果等价于跑 1 次，且对多 stream 都适用 | 单测 + 真跑验证 |
| **I-7** | NATS server 配置改动独立可回滚（改回 1 GB 之后 stream_max_bytes 降档一档仍可工作） | runbook 回滚步骤 |
| **I-8** | **分层归属完整性**：每个 `DEFAULT_CRITICAL_TOPICS` 里的 topic 必须归属且仅归属一个 `StreamSpec.topics`；两个 StreamSpec 的 topics 集合互斥；两者并集 == `DEFAULT_CRITICAL_TOPICS` | unit test `test_stream_specs_cover_all_critical_topics_exactly_once` |
| **I-9** | **MARKET stream 归属窄约束**：`AATS_EVENTS_MARKET` 只包含 `MARKET_SNAPSHOTS` + `FEATURE_SNAPSHOTS` 两个 topic；任何其他归属变更必须在本文件 §4 + §7 同步修改并经 review | unit test `test_market_stream_spec_only_contains_market_and_feature_snapshots` |
| **I-10** | **Legacy shim 不污染 runtime**：`NatsEventBus.ensure_stream(topics=...)` legacy 路径永远只操作**一次性** StreamSpec，**不**读/写 `self._config.streams`；runtime 路径（`_construct_event_bus` → `_start_event_bus` → `start()` → `ensure_streams()`）永远不经过 legacy shim | grep 审查 + runtime 启动日志不含 DeprecationWarning |
| **I-11** | **对称 max_msg_size**：`DEFAULT_AATS_EVENTS_MARKET_SPEC.max_msg_size == DEFAULT_AATS_EVENTS_SPEC.max_msg_size == 4_194_304`（4 MB，对齐 server `max_payload`） | unit test `test_default_stream_specs_have_symmetric_max_msg_size` |

## 4. 容量参数设计（分层 2 stream）

### 4.1 计算依据

用户批准：**server `max_file_store = 8 GB`**。本 slice 把原来的单 `AATS_EVENTS` stream 拆成 2 个 stream：

- **`AATS_EVENTS`**（长保留）：7 天 retention，承载决策/执行/风险/对账/审计等 critical 事件
- **`AATS_EVENTS_MARKET`**（短保留）：1 天 retention，承载 `market.snapshots` + `feature.snapshots` 两个高频观察/派生 topic

预留空间 = 8 GB − (AATS_EVENTS max_bytes + AATS_EVENTS_MARKET max_bytes) = **2 GB（25%）**，用于：
- JetStream per-stream index 文件（每 stream 一份）
- durable consumer 的 pending delivery state
- 临时磁盘 write buffer
- 未来可能的第三个 stream 扩展空间

比 NATS 官方推荐的 ~10% 宽松很多，预留充足。

### 4.2 Stream 归属规则

```
AATS_EVENTS_MARKET (1 day, 2 GB)
├── market.snapshots          ← 最高频 topic（OKX tick 推送）
└── feature.snapshots         ← 次高频（market 派生）

AATS_EVENTS (7 day, 4 GB)
├── 其他所有 DEFAULT_CRITICAL_TOPICS 里的 topic
│   包括但不限于：
│   ├── 决策链路：decision_contexts, ai_decision_briefs, ai_shadow_decisions, ...
│   ├── 执行链路：order_intents, order_updates, fill_events, obligation_updates, ...
│   ├── 资金状态：account.baselines, portfolio.snapshots, portfolio.balance_deltas
│   ├── 风险/对账：risk_decisions, reconciliation_reports, replay_validations
│   ├── 审计/operator：audit_records, operator_actions, kill_switch_state
│   └── strategy profile：profile_recommendations, profile_activations, ...
```

**归属规则的来源**：
```python
# aats/bus/nats_bus.py 新增常量

# 高频观察/派生 topic → 独立短保留 stream
DEFAULT_MARKET_STREAM_TOPICS: frozenset[str] = frozenset({
    _topics.MARKET_SNAPSHOTS,
    _topics.FEATURE_SNAPSHOTS,
})

# 其他 critical topic → 长保留 stream
# 派生自 DEFAULT_CRITICAL_TOPICS 减掉 DEFAULT_MARKET_STREAM_TOPICS
DEFAULT_CRITICAL_EVENTS_TOPICS: frozenset[str] = \
    DEFAULT_CRITICAL_TOPICS - DEFAULT_MARKET_STREAM_TOPICS
```

### 4.3 参数表（两个 stream 各一套）

#### 4.3.1 Server 级

| 字段 | 值 | 依据 |
|---|---|---|
| `nats-server.conf: max_file_store` | `8GB` | 用户选 γ；WSL2 磁盘充裕 |
| `nats-server.conf: max_memory_store` | `256MB` | 不变（本 slice 不动 memory-backed stream） |
| `nats-server.conf: max_payload` | `4MB` | 不变 |

#### 4.3.2 `AATS_EVENTS_MARKET` stream（高频短保留）

| 字段 | 值 | 依据 |
|---|---|---|
| `name` | `AATS_EVENTS_MARKET` | |
| `subjects` | `["aats.market.snapshots", "aats.features.snapshots"]` | 仅 2 个高频 subject |
| `retention` | `LIMITS` | |
| `storage` | `FILE` | 保留 1 天但要持久化（决策重启后继续消费） |
| `discard` | `OLD` | 满了丢最老 |
| `max_age` | `86400`（1 天） | 短保留 —— market 数据实时性压倒回放价值 |
| `max_bytes` | `2_147_483_648`（2 GB = 2 × 1024³） | 基于 1 day × 10 tick/s × 2.4 KB ≈ 2 GB/day，留 ~1x margin 吃突发 |
| `max_msgs` | `5_000_000`（500 万条） | 兜底保险丝，折合 ~400 B/msg，防 cardinality 爆炸 |
| `max_msg_size` | `4_194_304`（4 MB） | **用户决策：与 EVENTS / server `max_payload` 对称**（避免两个 stream 行为漂移，max_msg_size 只是兜底保险丝不影响实际 tick size 10 KB 级） |
| `num_replicas` | `1` | dev 单节点；生产升级时单独 slice 改 |
| `duplicate_window_seconds` | `120` | NATS 默认值，dedup 窗口够长抗重试风暴 |

#### 4.3.3 `AATS_EVENTS` stream（长保留 critical）

| 字段 | 值 | 依据 |
|---|---|---|
| `name` | `AATS_EVENTS` | 保持原名，向后兼容现有 consumer durable name |
| `subjects` | `DEFAULT_CRITICAL_EVENTS_TOPICS` 映射到 subject（~38 个） | `DEFAULT_CRITICAL_TOPICS` 减去 market/feature snapshots |
| `retention` | `LIMITS` | |
| `storage` | `FILE` | |
| `discard` | `OLD` | |
| `max_age` | `604800`（7 天，**不变**） | 合规 / 回放窗口 |
| `max_bytes` | `4_294_967_296`（4 GB = 4 × 1024³） | critical 事件相对低频，7 天 4 GB 预估 ~1 条/分钟均值（实际按数据量调整） |
| `max_msgs` | `5_000_000`（500 万条） | 和 MARKET 对齐方便运维理解 |
| `max_msg_size` | `4_194_304`（4 MB） | 与 server `max_payload` 对齐 |
| `num_replicas` | `1` | dev 单节点 |
| `duplicate_window_seconds` | `120` | NATS 默认，dedup 窗口够长抗重试风暴 |

**容量合计**：
```
AATS_EVENTS_MARKET    2 GB
AATS_EVENTS           4 GB
─────────────────────────
合计 stream max       6 GB  < server 8 GB  ✓ （headroom 2 GB = 25%）
```

### 4.4 对称/不对称设计决策解释

**为什么 MARKET stream 是 `FILE` 而不是 `MEMORY` 存储？**
初读看起来 1 天短保留 + 高频数据很像 memory stream 的场景。但：
- decision engine 重启后必须能从 market snapshot 消费 durable consumer 的 ack 位置继续消费，memory stream 重启丢数据会让 consumer 看到空 stream（或跳到最新）
- 1 天 × 2 GB 对 8 GB 磁盘来说负担很小
- FILE storage 和 MEMORY storage 的 throughput 差距在 dev 量级上可忽略（OKX tick 10/s）

**为什么两个 stream 的 `max_msg_size` 都是 4 MB？（用户决策：保持对称）**
- 用户明确要求"保持对称"：两个 stream 的 per-msg 兜底保险丝设同值避免运维/调试时被不对称吓到
- 4 MB 对齐 server `max_payload: 4MB`，即"单条消息的物理上限就是 server 级硬限"
- 对 MARKET 来说"超过 1 MB 早 fail 抓 schema 漂移" 的原始理由其实不需要 stream 级介入 —— 单条 market snapshot schema 测试 + mypy 限制已经足够；真要到 1 MB 级 market 消息意味着 NATS publish 链路有更严重的问题（批量化、payload 污染），那会是另外一个 slice 的话题
- 实际 market tick 消息在 KB 级（2-5 KB），离 4 MB 差 1000x 余量，对称不会引入真实风险

**为什么两个 stream 的 max_msgs 都设 500 万？**
保险丝性质，防 subject cardinality 或 high fan-in 打爆 index。数字本身不重要，够大就行；对齐数字让运维看 jsz 输出时一眼对得上。

**为什么两个 stream 的 num_replicas 都是 1？**
当前是 dev 单节点 NATS。如果未来升级生产做 cluster，再加一个独立 slice 统一改 `DEFAULT_STREAM_SPECS` 里的 `num_replicas`（单 env var override 即可切生产）。dev 不做副本避免额外磁盘占用。

**为什么 duplicate_window_seconds = 120 秒？**
NATS 默认值，足够抗：OKX publish retry（3x exponential backoff ≈ 30 秒）+ decision/execution 进程重启后的 resumption window（~60 秒）。再长会吃内存（dedup table 按时间窗口保存所有 Msg-Id），再短会 dedup 漏重。

### 4.5 env var override

所有容量字段走环境变量可覆盖，env_prefix `AATS_NATS_`（不再用 `STREAM_` 前缀，因为现在是 per-stream）：

#### `AATS_EVENTS_MARKET` 相关
| env var | StreamSpec 字段 |
|---|---|
| `AATS_NATS_MARKET_MAX_BYTES` | `AATS_EVENTS_MARKET.max_bytes` |
| `AATS_NATS_MARKET_MAX_MSGS` | `AATS_EVENTS_MARKET.max_msgs` |
| `AATS_NATS_MARKET_MAX_MSG_SIZE` | `AATS_EVENTS_MARKET.max_msg_size` |
| `AATS_NATS_MARKET_MAX_AGE_SECONDS` | `AATS_EVENTS_MARKET.max_age` |

#### `AATS_EVENTS` 相关
| env var | StreamSpec 字段 |
|---|---|
| `AATS_NATS_EVENTS_MAX_BYTES` | `AATS_EVENTS.max_bytes` |
| `AATS_NATS_EVENTS_MAX_MSGS` | `AATS_EVENTS.max_msgs` |
| `AATS_NATS_EVENTS_MAX_MSG_SIZE` | `AATS_EVENTS.max_msg_size` |
| `AATS_NATS_EVENTS_MAX_AGE_SECONDS` | `AATS_EVENTS.max_age_seconds`（映射旧字段） |

**向后兼容**：`stream_max_age_seconds` 旧字段保留在 `NatsBusConfig`，作为 `AATS_EVENTS.max_age` 的默认值兜底；旧的 `AATS_NATS_STREAM_MAX_AGE_SECONDS` env var 不再识别（需要在 migration notes 里明确）。

## 5. 工作包拆分

共 **5 commits**（含前置 + 4 个真正的改动）：

| # | Commit 类型 | 标题 | 范围 | 依赖 |
|---|---|---|---|---|
| 1 | `feat(nats-bus)` | ensure_stream 幂等 upsert 三分支（Slice 6.5 收尾） | `aats/bus/nats_bus.py` ensure_stream 一处 + `tests/unit/test_nats_bus_skeleton.py` 190 行 | 无 |
| 2 | `docs(slice-nats-capacity)` | NATS JetStream 容量+分层 stream 根因修复 slice 设计 | `docs/task/slice_nats_jetstream_capacity_fix_design.md`（本文） | 无 |
| 3 | `feat(nats-bus)` | **StreamSpec 抽象 + 分层 stream + 容量参数 + 容量感知 upsert** | `StreamSpec` 数据类 + 2 个默认 spec 常量 + `NatsBusConfig.streams` 改成列表 + `ensure_stream` → `ensure_streams` + 容量比较维度扩展 + env var 加载 + 18 新单测 | 1 |
| 4 | `chore(deploy)` | nats-server.conf max_file_store 8GB + runbook | `deploy/wsl2-dev/nats/nats-server.conf` + `deploy/wsl2-dev/RUNBOOK.md` §9.10 | 无（独立） |
| 5 | `feat(deploy)` | 多 stream `scripts/nats_stream_migrate.py` + 真跑验证 | 新脚本（支持两 stream） + runbook 补全 + `_probe_check_stream.py` 扩展为 2 stream | 3 + 4 |

**为什么这个顺序？**
- 1（Slice 6.5 前置）必须先落地，否则 3 会碰到 "stream 已存在 subjects 相同" 就 unchanged 分支直接 return，新容量配置永远应用不到
- 2 是本设计文档，放在 1 之后给 reviewer 按顺序看
- 3 是代码主干改动：StreamSpec 抽象 + 分层 stream 是核心改动，容量参数是附带改动，两者不分离避免中间态不可测
- 4 是 server 配置，独立可测（改 conf + NATS 容器重启即可）
- 5 是 migration + 真跑收尾；要把老的单 `AATS_EVENTS` stream 迁移成 2 个新 stream，必须在 3+4 之后做

**独立回滚**：
- 回滚 1 → 回退到 HEAD 的 `add_stream` 一行实现，但 Slice 6.5 OBLIGATION_UPDATES topic 会再次无法启动（已知故障）
- 回滚 3 → 回退到单 stream + 无容量参数（Slice 6.5 收尾后的状态）；**注意**：如果已经跑过工作包 5 的 migration 创建了 `AATS_EVENTS_MARKET`，回滚时必须手动 `delete_stream AATS_EVENTS_MARKET` + `AATS_EVENTS` subjects 合并回全集（runbook 有步骤）
- 回滚 4 → 改回 max_file_store 1GB，**必须同时降**档各 stream `max_bytes` 的 env var override 让总和 < 800MB 否则不变量 I-1 违反
- 回滚 5 → migration 脚本是一次性操作，回滚只需要 `git revert` 脚本文件（已执行的 purge 不能 un-purge，但 dev 环境分库分表无影响）

## 6. 工作包 1: feat(nats-bus) ensure_stream 幂等 upsert（前置）

### 6.1 审阅结论

- HEAD 的 `ensure_stream` 是 1 行 `await self._js.add_stream(config=config)`，Slice 6.5 新增 OBLIGATION_UPDATES topic 会让所有已有 stream 的容器启动失败 `err_code=10058`
- working tree 版本是完整的 `stream_info` 探测 + 三分支 upsert：
  - NotFoundError → `add_stream` + `created` 日志
  - subjects set 相等 → noop + `unchanged` 日志
  - subjects set 不等 → `update_stream` + `updated` 日志（含 added/removed diff）
  - 任一路径最终都 emit 统一 `nats_jetstream_stream_ensured` 事件保持向后兼容
- 测试质量：4 个新单测覆盖三分支 + 对称下线 case（subjects 多出的情形），pytest.importorskip 保护 CI 没装 nats-py 时跳过
- raw diff 1550 行是 Python formatter 重排噪音；`diff -u0 --ignore-all-space` 真实只有 5 个 hunk，全部在 `ensure_stream` 一个方法里
- **隐患** (由工作包 3 修)：比较维度只看 `subjects`，不看容量参数 —— 工作包 3 必须扩展这个比较

### 6.2 执行动作

```bash
git add aats/bus/nats_bus.py tests/unit/test_nats_bus_skeleton.py
git commit -F .git/COMMIT_MSG_1.txt
```

commit message 要点：
- Slice 6.5 引入 OBLIGATION_UPDATES topic 导致 `add_stream(config=...)` 非幂等问题的完整描述
- 三分支 upsert 的语义（created / unchanged / updated）
- 4 个新单测矩阵
- 向后兼容保证：`nats_jetstream_stream_ensured` 事件保留
- 遗留隐患（容量比较）由工作包 3 处理

### 6.3 验证

- 跑 `pytest tests/unit/test_nats_bus_skeleton.py -xvs` 全绿（5 个测试含原 max_age 回归）
- 无 linting 错误（black + ruff）

## 7. 工作包 3: feat(nats-bus) StreamSpec 抽象 + 分层 stream + 容量参数

### 7.1 StreamSpec 数据类

新增顶层数据类 `StreamSpec`，封装单个 JetStream stream 的完整定义：

```python
# aats/bus/nats_bus.py

try:
    from nats.js.api import (  # type: ignore[import-not-found]
        DiscardPolicy,
        RetentionPolicy,
        StorageType,
    )
    _NATS_API_AVAILABLE = True
except ImportError:
    _NATS_API_AVAILABLE = False


@dataclass(slots=True, frozen=True)
class StreamSpec:
    """NATS JetStream stream 的完整声明式定义。

    一个 StreamSpec 对应一个 ``js.add_stream`` / ``update_stream`` 调用的配置。
    本类是 nats-py ``StreamConfig`` 的薄封装 + 本项目的归属/容量约束。

    设计依据：slice_nats_jetstream_capacity_fix_design.md §4 + §7
    """

    # ── 标识 ────────────────────────────────────────────────────
    name: str                     # JetStream stream 名（如 "AATS_EVENTS"）
    topics: frozenset[str]        # 该 stream 承载的 EventBus topic 名（不带 aats. 前缀）

    # ── 容量策略（全部必填，没有默认值逼迫 caller 显式决策） ──
    max_age_seconds: float        # 消息保留时间上限（秒）
    max_bytes: int                # 总字节上限
    max_msgs: int                 # 总消息数上限
    max_msg_size: int             # 单条消息字节上限

    # ── 行为策略（有默认值，稳定字段） ─────────────────────────
    storage: str = "file"         # "file" / "memory"；传给 nats-py 时再转 StorageType
    retention: str = "limits"     # 固定 "limits"（本项目不用 workqueue/interest）
    discard: str = "old"          # 固定 "old"

    # ── 副本 / dedup / 运维（新增：带安全默认值） ────────────
    # 设计依据：§4.4 对称/不对称设计决策解释
    num_replicas: int = 1                     # dev 单节点；生产升级另起 slice
    duplicate_window_seconds: float = 120.0   # nats 默认；抗 publish 重试 + 进程重启 dedup 窗口
    deny_purge: bool = False                  # dev 允许 migration 脚本 purge；生产环境可 True

    def __post_init__(self) -> None:
        if not self.name or not self.name.isupper() or not self.name.replace("_", "").isalnum():
            raise ValueError(
                f"stream name must be SCREAMING_SNAKE_CASE, got {self.name!r}"
            )
        if not self.topics:
            raise ValueError(f"stream {self.name} must have at least one topic")
        if self.max_age_seconds <= 0:
            raise ValueError(f"stream {self.name} max_age_seconds must be positive, got {self.max_age_seconds}")
        if self.max_bytes <= 0:
            raise ValueError(f"stream {self.name} max_bytes must be positive, got {self.max_bytes}")
        if self.max_msgs <= 0:
            raise ValueError(f"stream {self.name} max_msgs must be positive, got {self.max_msgs}")
        if self.max_msg_size <= 0:
            raise ValueError(f"stream {self.name} max_msg_size must be positive, got {self.max_msg_size}")
        if self.storage not in ("file", "memory"):
            raise ValueError(f"stream {self.name} storage must be 'file' or 'memory', got {self.storage!r}")
        if self.num_replicas < 1:
            raise ValueError(f"stream {self.name} num_replicas must be >= 1, got {self.num_replicas}")
        if self.duplicate_window_seconds < 0:
            raise ValueError(f"stream {self.name} duplicate_window_seconds must be >= 0, got {self.duplicate_window_seconds}")

    def to_nats_stream_config(self, subject_prefix: str) -> "StreamConfig":  # noqa: F821
        """转成 nats-py StreamConfig 对象，调用 ensure_streams 时用。"""
        from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

        subjects = [f"{subject_prefix}{topic}" for topic in sorted(self.topics)]
        return StreamConfig(
            name=self.name,
            subjects=subjects,
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE if self.storage == "file" else StorageType.MEMORY,
            discard=DiscardPolicy.OLD,
            max_age=self.max_age_seconds,
            max_bytes=self.max_bytes,
            max_msgs=self.max_msgs,
            max_msg_size=self.max_msg_size,
            num_replicas=self.num_replicas,
            duplicate_window=self.duplicate_window_seconds,
            deny_purge=self.deny_purge,
        )
```

### 7.2 两个默认 StreamSpec 常量

```python
# aats/bus/nats_bus.py

# 高频观察/派生 topic → 独立短保留 stream
DEFAULT_MARKET_STREAM_TOPICS: frozenset[str] = frozenset({
    _topics.MARKET_SNAPSHOTS,
    _topics.FEATURE_SNAPSHOTS,
})

# 其他 critical topic → 长保留 stream
# 派生自 DEFAULT_CRITICAL_TOPICS 减掉 DEFAULT_MARKET_STREAM_TOPICS
DEFAULT_CRITICAL_EVENTS_TOPICS: frozenset[str] = \
    DEFAULT_CRITICAL_TOPICS - DEFAULT_MARKET_STREAM_TOPICS


# 默认的两个 stream spec（两者对称设计：见 §4.4）
DEFAULT_AATS_EVENTS_MARKET_SPEC = StreamSpec(
    name="AATS_EVENTS_MARKET",
    topics=DEFAULT_MARKET_STREAM_TOPICS,
    max_age_seconds=86_400,              # 1 天
    max_bytes=2_147_483_648,             # 2 GB
    max_msgs=5_000_000,                  # 500 万条
    max_msg_size=4_194_304,              # 4 MB（与 EVENTS 对称）
    # num_replicas / duplicate_window_seconds / deny_purge 走默认值
)

DEFAULT_AATS_EVENTS_SPEC = StreamSpec(
    name="AATS_EVENTS",
    topics=DEFAULT_CRITICAL_EVENTS_TOPICS,
    max_age_seconds=604_800,             # 7 天
    max_bytes=4_294_967_296,             # 4 GB
    max_msgs=5_000_000,                  # 500 万条
    max_msg_size=4_194_304,              # 4 MB
)

DEFAULT_STREAM_SPECS: tuple[StreamSpec, ...] = (
    DEFAULT_AATS_EVENTS_MARKET_SPEC,
    DEFAULT_AATS_EVENTS_SPEC,
)
```

### 7.3 NatsBusConfig 改动（含 legacy 测试路径 shim）

**设计约束**（来自 §7.5a caller 调研）：
1. 所有 runtime 代码（`_construct_event_bus` + `HybridEventBus.start`）转到"无 topics 参数"的新 API
2. 所有测试（6 个集成测试 start(topics=...) + 6 个单测 ensure_stream(topics=...)）**不改动任何一个 caller**，通过 legacy 字段 shim 继续工作（仅发 DeprecationWarning，不 raise）
3. 新老路径独立：runtime 走 `streams` 字段；legacy 走 `stream_name` + `stream_max_age_seconds` 字段 + 测试级默认容量

```python
@dataclass(slots=True)
class NatsBusConfig:
    # ── 已有字段保持 ────────────────────────────────────
    servers: tuple[str, ...] = ("nats://127.0.0.1:4222",)
    name: str = "aats"
    subject_prefix: str = "aats."
    # ... ack_wait_seconds / connect_timeout_seconds / max_reconnect_attempts 等保持 ...

    # ── Slice nats-capacity 新增（新 runtime 路径） ────
    #
    # 分层 stream 定义。默认 2 个：AATS_EVENTS_MARKET（1d, 2GB）和
    # AATS_EVENTS（7d, 4GB）。env var override 在 bootstrap 层处理。
    #
    # runtime 代码路径（_construct_event_bus + HybridEventBus.start）只看这个
    # 字段，通过 ensure_streams() 无 topics 参数完成 upsert。
    #
    # 设计依据：slice_nats_jetstream_capacity_fix_design.md §4 + §7.5a
    streams: tuple[StreamSpec, ...] = DEFAULT_STREAM_SPECS

    # ── legacy 字段（测试 shim + 回退兼容，不删除） ───
    #
    # 历史上 NatsBusConfig 用 stream_name + stream_max_age_seconds 两个扁平字段
    # 配合 ensure_stream(topics=...) / start(topics=...) 工作。集成测试 7 个
    # call site 依赖这个 API，本 slice 不改测试代码，而是在 NatsEventBus.
    # ensure_stream(topics) legacy shim 里按需从这俩字段 + 测试级默认容量
    # 构造一个**一次性**的 StreamSpec，直接调 _ensure_single_stream。
    #
    # 测试 caller 清单见 §7.5a 表格。runtime caller 都不再读这俩字段，
    # 只有 NatsEventBus.ensure_stream(topics) legacy shim 会读。
    stream_name: str = "AATS_EVENTS"         # legacy only，不影响 streams 字段
    stream_max_age_seconds: float | None = None  # legacy only，None = legacy shim 用 default 7 天

    def __post_init__(self) -> None:
        # 校验 streams 非空且 topic 归属互斥 + 完整
        if not self.streams:
            raise ValueError("NatsBusConfig.streams must not be empty")

        all_topics: set[str] = set()
        for spec in self.streams:
            dupes = all_topics & set(spec.topics)
            if dupes:
                raise ValueError(
                    f"stream {spec.name} has topics {sorted(dupes)} already "
                    f"claimed by another stream; topics must be exclusive"
                )
            all_topics |= set(spec.topics)

        # 注意：legacy stream_max_age_seconds 不再影响 self.streams
        # （避免"test 设 3600 秒 → 生产 AATS_EVENTS 变 1 小时 retention"的
        #  危险耦合）。测试 shim 在 ensure_stream(topics) legacy 路径里单独
        #  读这个字段，只对 **legacy 一次性 StreamSpec** 生效。
    
    def spec_for_topic(self, topic: str) -> StreamSpec:
        """返回承载该 topic 的 StreamSpec；找不到 raise。"""
        for spec in self.streams:
            if topic in spec.topics:
                return spec
        raise ValueError(
            f"topic {topic!r} not claimed by any stream in "
            f"{[s.name for s in self.streams]}"
        )
```

### 7.4 ensure_stream → ensure_streams 重构（+ legacy shim 保留）

**旧签名**（Slice 6.5 收尾版本）：
```python
async def ensure_stream(self, topics: list[str]) -> None:
```

**新 runtime 签名**：
```python
async def ensure_streams(self) -> None:
    """对 self._config.streams 里每个 spec 走 upsert 流程。

    每个 stream 独立走 Slice 6.5 的三分支 upsert（created / unchanged / updated）
    并且容量比较维度从 subjects 扩展到 subjects + 所有容量参数。
    """
    # ... 见 7.4.2 ...
```

**Legacy shim**（测试和回退兼容，**不修改任何测试代码**）：
```python
async def ensure_stream(self, topics: list[str] | None = None) -> None:
    """Deprecated legacy shim. 构造一次性 StreamSpec 走单 stream upsert。

    保留原因：§7.5a 调研显示 6 个集成测试 + 4 个单测的 7 个 call site 依赖
    ``bus.ensure_stream(topics=[...])`` / ``bus.start(topics=[...])`` 语义，本
    slice 不改测试代码；这个 shim 从 ``NatsBusConfig.stream_name`` + 测试级
    默认容量构造一个**一次性** StreamSpec，直接调 ``_ensure_single_stream``。

    注意：这个 shim **不**读 ``self._config.streams``。测试环境的 streams 字段
    仍然是 default 的 (AATS_EVENTS_MARKET, AATS_EVENTS) tuple，但测试从来不调
    ``ensure_streams()``，所以默认 tuple 里的 spec 对测试环境是死代码。

    Runtime 路径（_construct_event_bus + HybridEventBus.start）走新的
    ``ensure_streams()`` 无参 API，**永远不会**走到这个 legacy shim。
    """
    import warnings
    warnings.warn(
        "NatsEventBus.ensure_stream(topics=...) is deprecated; new runtime code "
        "should call ensure_streams() (no args) which reads StreamSpec list from "
        "NatsBusConfig.streams. Legacy test code may continue to use this path.",
        DeprecationWarning,
        stacklevel=2,
    )
    if self._js is None:
        raise RuntimeError("NatsEventBus.ensure_stream called before connect()")
    if topics is None:
        topics = []

    # 从 legacy 字段 + 测试级默认容量构造一次性 StreamSpec
    legacy_spec = StreamSpec(
        name=self._config.stream_name,
        topics=frozenset(topics) if topics else frozenset({"_legacy_placeholder"}),
        max_age_seconds=(
            self._config.stream_max_age_seconds
            if self._config.stream_max_age_seconds is not None
            else 7 * 24 * 60 * 60
        ),
        # 测试级默认容量：小但非零。真跑时测试会验证 publish/subscribe 行为，
        # 不验证 stream_info 的容量字段；走 128 MB / 10K msg / 4 MB msg size。
        max_bytes=134_217_728,       # 128 MB
        max_msgs=10_000,             # 1 万条（集成测试消息量很小）
        max_msg_size=4_194_304,      # 4 MB
    )
    # StreamSpec 的 topics 非空校验：如果 caller 传空 list，上面用了占位符，
    # 需要在这里纠正回空 topics 的语义错误
    if "_legacy_placeholder" in legacy_spec.topics:
        raise ValueError(
            "ensure_stream(topics=[]) is not supported; "
            "legacy shim requires at least one topic"
        )

    await self._ensure_single_stream(legacy_spec)
```

### 7.4.1 为什么保留 `topics` 参数 legacy shim？

原 API 设计前提是"caller 告诉 bus 需要哪些 topic 持久化"。新设计下这个 API 有两个问题：
1. Caller 从哪里拿到这个 topic 列表？历史上是写死的，容易漂移
2. 多 stream 之后，topic 归属已经由 `StreamSpec.topics` 定义，caller 再传 topic 列表反而引入歧义

**但是**：调研发现 6 个集成测试 + 4 个单测共 7 个 call site 依赖 legacy API（§7.5a 表格）。本 slice 坚持"不改测试代码"原则（I-5 的推论：测试不改避免引入回归），因此：

- **runtime 代码全部迁移到新 API**：`HybridEventBus.start()` → `critical_start()`（无 topics）；`_construct_event_bus` + `_start_event_bus` → 新签名
- **测试代码一律保留原样**：legacy shim 仍能工作，发 DeprecationWarning 提示未来再迁移
- **新代码禁止走 legacy shim**：通过 code review + deprecation warning 强制

新设计下"spec 是唯一事实来源"的不变量只对 runtime 代码生效；测试环境允许用 ad-hoc 一次性 StreamSpec 验证单 stream 行为。

### 7.4.2 ensure_streams 实现（每个 stream 独立 upsert）

```python
async def ensure_streams(self) -> None:
    """声明本 bus 的所有 JetStream stream（幂等 upsert，容量感知）。
    
    对 self._config.streams 里每个 StreamSpec 走：
    1. stream_info 探测
    2. 如果不存在 → add_stream → 'created'
    3. 如果存在且 subjects+容量都一致 → noop → 'unchanged'
    4. 如果存在但 subjects 或容量参数任一不一致 → update_stream → 'updated'
    5. 每个 stream 最终 emit 一次 'nats_jetstream_stream_ensured' 收尾事件
       （向后兼容 Slice 6.1 / 6.3 的 runbook 断言）
    """
    if self._js is None:
        raise RuntimeError("NatsEventBus.ensure_streams called before connect()")
    
    from nats.js.errors import NotFoundError
    
    for spec in self._config.streams:
        await self._ensure_single_stream(spec)

async def _ensure_single_stream(self, spec: StreamSpec) -> None:
    from nats.js.errors import NotFoundError
    
    config = spec.to_nats_stream_config(self._config.subject_prefix)
    
    # ── Step 1: stream_info 探测 ─────────────────────────
    existing_cfg = None
    try:
        info = await self._js.stream_info(spec.name)
        existing_cfg = info.config
    except NotFoundError:
        existing_cfg = None
    
    if existing_cfg is None:
        # Step 2a: 创建
        await self._js.add_stream(config=config)
        log_event(
            self.logger,
            "nats_jetstream_stream_created",
            stream=spec.name,
            subject_count=len(config.subjects),
            max_bytes=spec.max_bytes,
            max_msgs=spec.max_msgs,
            max_msg_size=spec.max_msg_size,
            max_age_seconds=spec.max_age_seconds,
        )
    else:
        # Step 2b: 比较 subjects + 容量参数
        existing_subjects_set = set(existing_cfg.subjects or [])
        new_subjects_set = set(config.subjects)
        
        capacity_drift = (
            existing_cfg.max_bytes != spec.max_bytes
            or existing_cfg.max_msgs != spec.max_msgs
            or existing_cfg.max_msg_size != spec.max_msg_size
            or existing_cfg.max_age != spec.max_age_seconds
        )
        
        if existing_subjects_set == new_subjects_set and not capacity_drift:
            log_event(
                self.logger,
                "nats_jetstream_stream_unchanged",
                stream=spec.name,
                subject_count=len(config.subjects),
            )
        else:
            added = sorted(new_subjects_set - existing_subjects_set)
            removed = sorted(existing_subjects_set - new_subjects_set)
            await self._js.update_stream(config=config)
            log_event(
                self.logger,
                "nats_jetstream_stream_updated",
                stream=spec.name,
                subject_count_before=len(existing_subjects_set),
                subject_count_after=len(new_subjects_set),
                subjects_added=added,
                subjects_removed=removed,
                capacity_drift=capacity_drift,
                max_bytes=spec.max_bytes,
                max_msgs=spec.max_msgs,
                max_msg_size=spec.max_msg_size,
                max_age_seconds=spec.max_age_seconds,
            )
    
    # Step 3: 统一收尾日志（向后兼容 Slice 6.1/6.3 runbook 断言）
    log_event(
        self.logger,
        "nats_jetstream_stream_ensured",
        stream=spec.name,
        subjects=sorted(config.subjects),
        max_bytes=spec.max_bytes,
        max_msgs=spec.max_msgs,
        max_msg_size=spec.max_msg_size,
        max_age_seconds=spec.max_age_seconds,
    )
```

### 7.5 start() 调用点改动（兼容 legacy topics 参数）

现有的 `start(topics=[...])` 逻辑：
```python
async def start(self, *, topics: list[str] | None = None) -> None:
    await self.connect()
    if topics:
        await self.ensure_stream(topics=topics)
```

改成：
```python
async def start(self, *, topics: list[str] | None = None) -> None:
    """启动 bus：connect + ensure_streams / legacy ensure_stream。

    新 runtime 路径：``bus.start()`` 无参 → ``connect() + ensure_streams()``
    Legacy 测试路径：``bus.start(topics=[...])`` → ``connect() + ensure_stream(topics)``

    两条路径互不干扰：新 runtime 走 NatsBusConfig.streams 字段；
    legacy 测试走 NatsBusConfig.stream_name + stream_max_age_seconds 字段
    生成一次性 StreamSpec。
    """
    await self.connect()
    if topics is not None:
        # Legacy 路径：发 deprecation warning 但继续工作
        await self.ensure_stream(topics=topics)
    else:
        # 新 runtime 路径
        await self.ensure_streams()
```

**runtime caller 的改动**：
1. `aats/bus/nats_bus.py:750` `HybridEventBus.start()` 当前调 `critical_start(topics=critical_topics)`；改成 `critical_start()` 不传 topics
2. `aats/bootstrap/config.py:2986` `_start_event_bus(bus)` 当前调 `start_method()` 已经无 topics 参数，**不需要改**（但要确认 NatsEventBus 单独存在时也能正确走到 `ensure_streams`）

### 7.5a Caller 调研结果与设计调整（§7.5a 新增，回应用户"3. 请你查清楚再设计"）

本节详细列出所有 `ensure_stream(topics=...)` / `start(topics=...)` / `NatsBusConfig(stream_name=...)` 的 call site，按"runtime vs 测试"分类，并说明每个 call site 在本 slice 后的处理方式。

#### 7.5a.1 Runtime call site（必须改）

| # | 文件:行 | 上下文 | 本 slice 改动 |
|---|---|---|---|
| R1 | `aats/bootstrap/config.py:2941` | `_construct_event_bus()` 构造 `NatsBusConfig(servers, stream_name, stream_max_age_seconds)` | 改成 `NatsBusConfig(servers=..., streams=_build_nats_streams_from_env(DEFAULT_STREAM_SPECS))`；**保留** stream_name + stream_max_age_seconds 字段（用 legacy 默认），但不再传给 constructor（因为 runtime 路径不用） |
| R2 | `aats/bootstrap/config.py:2986` | `_start_event_bus(bus)` 调 `await start_method()` 无 topics 参数 | **不改**。已经是新路径。此前因为 `start(topics=None)` 在旧代码里会跳过 `ensure_stream`，实际上 pre-slice 的 monolith+nats 路径压根没创建 stream（pre-existing 潜在 bug）；新 `start()` 无参会走 `ensure_streams()`，反而修复了这个遗漏 |
| R3 | `aats/bus/nats_bus.py:750` | `HybridEventBus.start()` 调 `critical_start(topics=critical_topics)` | 改成 `await critical_start()` 不传 topics；注意这里 critical_topics 来自 `HybridBusRouting.critical_topics`，是 hybrid 路由层的元数据，不再用于决定 stream 创建，只用于 `publish_envelope` 的 route_for 决策 |

#### 7.5a.2 Legacy 测试 call site（**不改**，依赖 legacy shim）

| # | 文件:行 | 场景 | Legacy shim 处理 |
|---|---|---|---|
| T1 | `tests/integration/test_nats_event_bus_roundtrip.py:152+162` | 单 NatsEventBus round-trip，`NatsBusConfig(stream_name="AATS_RT_SINGLE")` + `bus.start(topics=[_topics.AI_DECISION_BRIEFS])` | legacy shim 构造一次性 StreamSpec(name="AATS_RT_SINGLE", topics={AI_DECISION_BRIEFS}, 测试级默认容量) |
| T2 | `tests/integration/test_nats_event_bus_roundtrip.py:237+264` | HybridBus 内部 NatsEventBus 共享 stream，`stream_name="AATS_RT_HYBRID"` + `verifier.start(topics=None)` | `topics=None` 走新 ensure_streams 路径；但 verifier 的 streams 字段是 default (MARKET+EVENTS) —— 这会**非预期**地创建 AATS_EVENTS + AATS_EVENTS_MARKET 两个 stream 污染测试环境。**解决方案**：legacy shim 仅在 `topics is not None` 时生效；`topics=None` 的老 caller 改调 `ensure_stream(topics=[])` 明确走空路径 —— 但这会触发 "at least one topic" 校验。**最终决定**：T2 这个 call site **需要改一行**，把 `start(topics=None)` 改成 **直接调 `connect()`**（不走 start/ensure 任一路径），因为 verifier 本来就是独立 stream 订阅者，stream 已经由 hybrid 创建。这是**唯一**一个允许测试代码改动的例外。|
| T3 | `tests/integration/test_nats_event_bus_roundtrip.py:306-340` | Hybrid observer verifier，`stream_name="AATS_RT_OBSERVER_VERIFIER"` + `verifier.start(topics=[_topics.HEALTH_SNAPSHOTS])` | legacy shim 构造一次性 StreamSpec(name="AATS_RT_OBSERVER_VERIFIER", topics={HEALTH_SNAPSHOTS}, 测试级默认容量)。HEALTH_SNAPSHOTS 是 observer topic，不在任何 default StreamSpec 里，但 legacy shim 不检查这个约束，允许任意 topic 走 ad-hoc stream |
| T4 | `tests/integration/test_nats_event_bus_roundtrip.py:386+396` | Hybrid cross-process publisher 子进程，`stream_name="AATS_RT_HYBRID_XP"` + `bus.start(topics=[AI_DECISION_BRIEFS])` | 同 T1 |
| T5 | `tests/integration/test_nats_event_bus_roundtrip.py:452+462` | Parallel isolation subscriber，`stream_name="AATS_RT_PARALLEL"` + `subscriber.start(topics=[AI_DECISION_BRIEFS])` | 同 T1 |
| T6 | `tests/unit/test_nats_bus_skeleton.py:326` | `test_nats_bus_ensure_stream_before_connect_raises`：`bus.ensure_stream(topics=["decisions"])` 在 connect 前调 | legacy shim 先检查 `self._js is None` raise RuntimeError —— 语义完全保留 |
| T7 | `tests/unit/test_nats_bus_skeleton.py:431` | `test_ensure_stream_passes_max_age_in_seconds_not_nanoseconds`：`bus.ensure_stream(topics=["decisions"])` 验证 max_age 字段 | legacy shim 用 `self._config.stream_max_age_seconds` 构造一次性 StreamSpec，通过 `_ensure_single_stream` 的 FakeJetStream 断言 max_age=3600 |
| T8 | `tests/unit/test_nats_bus_skeleton.py:480` | `test_ensure_stream_creates_when_stream_missing`：`bus.ensure_stream(topics=["decisions", "execution.order_intents"])` | legacy shim 构造一次性 StreamSpec，NotFoundError 路径走 add_stream |
| T9 | `tests/unit/test_nats_bus_skeleton.py:521` | `test_ensure_stream_unchanged_when_subjects_match` | legacy shim 单 stream + Fake stream_info 返回相等 subjects |
| T10 | `tests/unit/test_nats_bus_skeleton.py:560` | `test_ensure_stream_updates_when_subjects_differ` | 同上，subjects 不同走 update_stream |
| T11 | `tests/unit/test_nats_bus_skeleton.py:614` | `test_ensure_stream_updates_when_subject_removed` | 同上，subjects 减少走 update_stream |
| T12 | `tests/unit/test_nats_bus_skeleton.py:635` | `test_ensure_stream_signature_takes_topics_keyword`：用 inspect 检查 `ensure_stream` 签名的 `topics` 关键字参数存在 | legacy shim 签名保留 `topics` kwarg，测试自动通过 |

#### 7.5a.3 单 call site 测试改动例外（T2）

唯一允许修改的测试代码：`tests/integration/test_nats_event_bus_roundtrip.py:264`。

**原因**：这个 call 用 `start(topics=None)` 显式跳过 ensure_stream（stream 已被另一个 bus 创建）。新设计下 `topics=None` 走新 `ensure_streams()` 路径，会创建默认的 2 个 stream 污染测试环境。

**改动**：把 `await verifier.start(topics=None)` 改成 `await verifier.connect()`（直接 connect，跳过任何 ensure 路径）。

**这一行改动不违反 I-5 不变量**，因为：
- 原来 `start(topics=None)` 就是"跳过 ensure_stream"的意图
- `connect()` 正好等价于"跳过 ensure_stream"
- 语义保留，只是用更精确的 API

#### 7.5a.4 新路径 vs legacy 路径的运行时保证

| 保证 | 新路径（runtime） | Legacy 路径（测试） |
|---|---|---|
| 多 stream 并存 | ✓ 默认 2 个 stream 都创建 | × 只创建 1 个 ad-hoc stream |
| 容量策略 | ✓ 生产级（MB/GB 级） | × 测试级（128 MB / 10K msg） |
| Topic 归属互斥校验 | ✓ `NatsBusConfig.__post_init__` | × 不校验，允许 ad-hoc topic |
| Env var override | ✓ `_build_nats_streams_from_env` | × 不支持 |
| DeprecationWarning | × 不触发 | ✓ 每次触发（运行时可过滤） |
| Consumer 跨 stream 路由 | ✓ 新 stream 布局 | × 和 legacy 单 stream 行为一致 |

**结论**：两条路径互相独立，新路径保证生产行为，legacy 路径保证测试兼容性。测试用 ad-hoc 小 stream 验证基础行为（publish/subscribe/重放/durable ack）在新单 stream 架构下仍然正确，不需要跟着 runtime 一起迁移到多 stream。

### 7.6 env var 加载

在 `aats/bootstrap/config.py` 的 NATS 配置加载路径里（预计在 `build_runtime` 构造 `NatsBusConfig` 时）加一段 env var override 逻辑：

```python
def _build_nats_streams_from_env(
    default_specs: tuple[StreamSpec, ...],
) -> tuple[StreamSpec, ...]:
    """把 env var override 应用到默认 StreamSpec list。
    
    支持的 env var：
    - AATS_NATS_MARKET_MAX_BYTES / MAX_MSGS / MAX_MSG_SIZE / MAX_AGE_SECONDS
    - AATS_NATS_EVENTS_MAX_BYTES / MAX_MSGS / MAX_MSG_SIZE / MAX_AGE_SECONDS
    """
    from dataclasses import replace
    
    result: list[StreamSpec] = []
    for spec in default_specs:
        overrides: dict[str, int | float] = {}
        prefix = "AATS_NATS_MARKET_" if spec.name == "AATS_EVENTS_MARKET" \
                 else "AATS_NATS_EVENTS_" if spec.name == "AATS_EVENTS" \
                 else None
        if prefix:
            if v := os.environ.get(f"{prefix}MAX_BYTES"):
                overrides["max_bytes"] = int(v)
            if v := os.environ.get(f"{prefix}MAX_MSGS"):
                overrides["max_msgs"] = int(v)
            if v := os.environ.get(f"{prefix}MAX_MSG_SIZE"):
                overrides["max_msg_size"] = int(v)
            if v := os.environ.get(f"{prefix}MAX_AGE_SECONDS"):
                overrides["max_age_seconds"] = float(v)
        result.append(replace(spec, **overrides) if overrides else spec)
    return tuple(result)
```

### 7.7 单测矩阵（WP3 共 21 条）

在 `tests/unit/test_nats_bus_skeleton.py` 新增：

#### StreamSpec 构造与校验（8 条）
| 测试 | 场景 | 断言 |
|---|---|---|
| `test_stream_spec_accepts_valid_config` | 默认值构造成功 | 无 raise |
| `test_stream_spec_rejects_empty_topics` | `topics=frozenset()` | raise ValueError |
| `test_stream_spec_rejects_non_positive_max_bytes` | `max_bytes=0` | raise ValueError |
| `test_stream_spec_rejects_non_positive_max_msgs` | `max_msgs=-1` | raise ValueError |
| `test_stream_spec_rejects_non_positive_max_msg_size` | `max_msg_size=0` | raise ValueError |
| `test_stream_spec_rejects_invalid_storage` | `storage="disk"` | raise ValueError |
| `test_stream_spec_rejects_zero_num_replicas` | `num_replicas=0` | raise ValueError |
| `test_stream_spec_rejects_negative_duplicate_window_seconds` | `duplicate_window_seconds=-1` | raise ValueError |

#### 默认 SPEC 常量与归属（4 条，验证 I-1 / I-8 / I-9 / I-11）
| 测试 | 场景 | 断言 |
|---|---|---|
| `test_stream_specs_cover_all_critical_topics_exactly_once` | I-8 union 覆盖 | `∪ specs.topics == DEFAULT_CRITICAL_TOPICS` 且任两 spec 互斥 |
| `test_market_stream_spec_only_contains_market_and_feature_snapshots` | I-9 MARKET 窄归属 | `DEFAULT_AATS_EVENTS_MARKET_SPEC.topics == {MARKET_SNAPSHOTS, FEATURE_SNAPSHOTS}` |
| `test_total_stream_capacity_within_server_budget` | I-1 | `sum(spec.max_bytes for spec in DEFAULT_STREAM_SPECS) < 8 GB * 0.85` |
| `test_default_stream_specs_have_symmetric_max_msg_size` | I-11 对称 | `MARKET.max_msg_size == EVENTS.max_msg_size == 4_194_304` |

#### NatsBusConfig 多 stream 支持（2 条）
| 测试 | 场景 | 断言 |
|---|---|---|
| `test_nats_bus_config_default_has_two_streams` | 默认构造 | `len(cfg.streams) == 2` |
| `test_nats_bus_config_rejects_duplicate_topic_across_streams` | 两个 spec 都包含同一 topic | raise ValueError |

#### ensure_streams 多 stream + 三分支（7 条）
| 测试 | 场景 | 断言 |
|---|---|---|
| `test_ensure_streams_creates_both_when_missing` | stream_info NotFoundError × 2 | `add_stream` 被调用 2 次 |
| `test_ensure_streams_unchanged_when_both_match` | subjects + 容量全对 | `add_stream` / `update_stream` 都不调 |
| `test_ensure_streams_updates_when_subjects_differ` | MARKET subjects 不同 | MARKET `update_stream`，EVENTS 不动 |
| `test_ensure_streams_updates_when_max_bytes_differs` | EVENTS max_bytes 不同 | EVENTS `update_stream`，MARKET 不动 |
| `test_ensure_streams_updates_when_max_age_differs` | MARKET max_age 不同 | MARKET `update_stream` |
| `test_ensure_streams_updates_when_max_msg_size_differs` | EVENTS max_msg_size 不同 | EVENTS `update_stream` |
| `test_ensure_streams_mixed_one_create_one_update` | MARKET 缺失，EVENTS 存在但容量变了 | MARKET `add_stream`，EVENTS `update_stream` |

**共 21 条新测试**，加上已有 5 条（Slice 6.5 前置 4 条 + 原 max_age 回归 1 条）= **26 条 ensure_stream 单测**。

**此外 legacy shim 单测扩展**（§7.4 / §7.5a.2 T6-T12）：
- 老 4 条 Slice 6.5 单测的 FakeStreamInfo mock 需补 `max_bytes`/`max_msgs`/`max_msg_size`/`max_age` 字段，让 legacy shim 的新容量比较逻辑能判等 unchanged
- 断言语义**不变**，mock 字段扩充后测试继续绿
- 合计"扩展式"改动 4 条（不计入 21 新增）

### 7.8 invariant I-1 编译期保护

`StreamSpec.__post_init__` 不能知道 server `max_file_store`（解耦原则）。invariant I-1 保护走：

- **测试层**：`test_total_stream_capacity_within_server_budget` 单测硬编码 server 8 GB 作为 fixture（如果改 conf 必须同步改测试）
- **文档层**：runbook `deploy/wsl2-dev/RUNBOOK.md` §9.10 加 drift 检查步骤
- **运行时日志**：`_ensure_single_stream` 的 `nats_jetstream_stream_created` / `updated` 日志都会打 `max_bytes`，加起来和 server varz 对比可做手动 drift 监控

## 8. 工作包 4: chore(deploy) nats-server.conf + runbook

### 8.1 nats-server.conf 改动

```conf
# deploy/wsl2-dev/nats/nats-server.conf

jetstream {
  store_dir: "/data/jetstream"

  # 文件存储上限：8GB
  # ⚠️ 对齐约束：该值必须严格大于所有 stream 的 max_bytes 之和，建议留
  # ≥ 15% overhead 给 JetStream index / consumer state / write buffer。
  #
  # 当前 DEFAULT_STREAM_SPECS 总预算：
  #   AATS_EVENTS_MARKET.max_bytes = 2 GB
  #   AATS_EVENTS.max_bytes        = 4 GB
  #   ─────────────────────────────────
  #   合计 = 6 GB = max_file_store × 0.75（headroom 25%，充足）
  #
  # 如果改本值必须同时 review aats/bus/nats_bus.py 里的 DEFAULT_STREAM_SPECS
  # max_bytes 默认值 + env var override 保持对齐关系（单元测试
  # test_total_stream_capacity_within_server_budget 会锁死 8 GB 基准）。
  # 设计文档：docs/task/slice_nats_jetstream_capacity_fix_design.md §4 + §8.1
  max_file_store: 8GB

  # 内存存储上限：256MB（observer topic 总和）
  max_memory_store: 256MB
}

# max_payload: 4MB —— stream-level max_msg_size 的硬上限
# DEFAULT_STREAM_SPECS 两个 stream 的 max_msg_size 都是 4 MB（对称），
# 这个值不能降到 4 MB 以下，否则 max_msg_size 会被 publish path 拒绝。
max_payload: 4MB
```

### 8.2 RUNBOOK.md 新增章节

在 `deploy/wsl2-dev/RUNBOOK.md` 加 §9.10 NATS JetStream 容量运维，内容：

- 健康水位：stream_bytes / max_bytes < 80% 绿色，80-95% 黄色，> 95% 红色
- 抓取命令：`docker exec aats-nats wget -qO- http://localhost:8222/jsz?streams=1 | jq '.account_details[].stream_detail[] | {name, state: {messages, bytes}, config: {max_bytes, max_msgs}}'`
- drift 检查：如果服务器 conf 里的 max_file_store 和代码默认的 stream_max_bytes 失衡（比例 < 0.8 或 > 0.95）如何修
- 手动 purge 流程（应急）：`docker exec aats-nats nats stream purge AATS_EVENTS --force` 或走 `scripts/nats_stream_migrate.py`
- 容量规划：生产规模下从 dev 8 GB 升到多少的经验公式（写入 rate × 7d × avg msg size）

### 8.3 验证

- 改 conf 后 `docker compose restart nats`
- 用 `docker exec aats-nats wget -qO- http://localhost:8222/varz | jq .jetstream.config.max_storage` 验证新值生效
- 跑 `_probe_check_stream.py` 验证 stream 仍可用（此时未跑 migration，stream 可能还在 1 GB 老状态）

## 9. 工作包 5: feat(deploy) 多 stream migration script + 真跑验证

### 9.1 scripts/nats_stream_migrate.py 设计（支持多 stream）

```python
"""NATS AATS events stream 容量策略 + 分层归属 migration（幂等，多 stream）。

用途
----
1. 把老的单 AATS_EVENTS stream（含所有 critical topic）迁移到 slice
   nats-capacity 的分层架构（AATS_EVENTS + AATS_EVENTS_MARKET 两个 stream）
2. 对每个 stream 同步容量策略到最新 StreamSpec
3. 可选清洗：purge 遗留的累积观察运行噪音数据
4. 支持 --dry-run 只打印计划不执行

幂等性
------
- 跑 N 次效果等价于跑 1 次
- 已经是新配置则每个 stream 各自 noop
- stream 不存在则 noop（进程启动时 ensure_streams 会自动创建）

用法
----
# 只看当前所有 stream 状态 + 差异报告（每 stream 各一段）
python scripts/nats_stream_migrate.py --dry-run

# 同步容量策略 + 拆分 AATS_EVENTS → (AATS_EVENTS, AATS_EVENTS_MARKET)
python scripts/nats_stream_migrate.py --sync-config

# 同步 + purge 历史数据（dev 推荐，用户确认分库分表测试不影响）
python scripts/nats_stream_migrate.py --sync-config --purge

# 完全重建所有 stream（delete + recreate；最激进）
python scripts/nats_stream_migrate.py --recreate
"""
```

### 9.2 迁移矩阵（老 → 新 的 6 种状态转换）

脚本要能处理所有 6 种可能的出发状态：

| # | 出发状态 | 动作 | 说明 |
|---|---|---|---|
| **T1** | 老 AATS_EVENTS 存在（含全部 critical topics），AATS_EVENTS_MARKET 不存在 | ① update_stream AATS_EVENTS 移除 market/feature snapshots；② add_stream AATS_EVENTS_MARKET（含 market/feature snapshots） | **最常见**：首次升级场景 |
| **T2** | 两个 stream 都存在且完全对齐 | noop | 升级完的稳态 |
| **T3** | AATS_EVENTS 新状态，AATS_EVENTS_MARKET 容量参数略有漂移 | update_stream AATS_EVENTS_MARKET | env var override 后重启场景 |
| **T4** | AATS_EVENTS 不存在，AATS_EVENTS_MARKET 不存在 | add_stream 两个 | 全新 WSL2 环境 |
| **T5** | AATS_EVENTS 存在但 subjects 不完整（譬如少 OBLIGATION_UPDATES），AATS_EVENTS_MARKET 不存在 | ① update_stream AATS_EVENTS 把 subjects 补到新集合减 market；② add_stream AATS_EVENTS_MARKET | Slice 6.5 前置未跑过的环境 |
| **T6** | 诡异状态：AATS_EVENTS_MARKET 存在但 AATS_EVENTS 不存在 | **raise + 诊断输出** | 半回滚/半升级场景 —— 用户决策：不自动恢复，暴露人类介入 |

**T6 raise 语义**（用户决策："4. 要" = 要 T6 raise）：

```python
if "AATS_EVENTS_MARKET" in existing_streams and "AATS_EVENTS" not in existing_streams:
    raise RuntimeError(
        "inconsistent stream state detected:\n"
        "  AATS_EVENTS_MARKET exists but AATS_EVENTS does not\n"
        "  this is a partial-rollback/partial-upgrade state that the\n"
        "  migration script refuses to auto-recover from.\n"
        "\n"
        "  possible causes:\n"
        "    1. a previous migration was interrupted after creating\n"
        "       AATS_EVENTS_MARKET but before touching AATS_EVENTS\n"
        "    2. someone manually deleted AATS_EVENTS for debug purposes\n"
        "    3. bit rot / corruption\n"
        "\n"
        "  manual recovery steps:\n"
        "    1. inspect AATS_EVENTS_MARKET state:\n"
        "       docker exec aats-nats nats stream info AATS_EVENTS_MARKET\n"
        "    2. if its config looks sane:\n"
        "       python scripts/nats_stream_migrate.py --recreate-events-only\n"
        "       (will add_stream AATS_EVENTS and leave AATS_EVENTS_MARKET alone)\n"
        "    3. if not sane, full recovery:\n"
        "       python scripts/nats_stream_migrate.py --recreate\n"
        "       (deletes and recreates both streams; purges all data)\n"
    )
```

**为什么 raise 而不是 auto-recover？**
- 这个状态是**非预期的**，auto-recover 会掩盖根因，下次再出现时无迹可循
- dev 场景人工介入成本低，raise 能暴露原因让用户直接 fix
- 生产场景 raise 能阻断 deploy pipeline，避免带着不一致状态向下游扩散
- 用户明确决策："要"（T6 raise）

**purge 语义**：对每个 stream 独立 purge。`--sync-config --purge` 会 purge 两个 stream 各自。dev 场景下没问题，生产要慎用。

### 9.3 三种操作模式的语义

| 模式 | 动作 | 适用 |
|---|---|---|
| `--dry-run` | 每 stream 独立 `stream_info` + print diff；不改 state | 每次升级前先 dry-run |
| `--sync-config` | 按迁移矩阵执行 add/update；历史数据**保留** | 升级生产不想丢数据 |
| `--sync-config --purge` | 迁移矩阵 + 每 stream `purge_stream` | dev 场景（用户已确认分库分表测试不影响） |
| `--recreate` | 所有 stream `delete_stream` + 重建 | 全新初始化 |

本 slice 实施时 dev 环境跑 `--sync-config --purge` 清掉 45 万条历史数据并拆成两个 stream。

### 9.4 脚本单测

`tests/unit/test_nats_stream_migrate.py` 覆盖 9 条测试：

| 测试 | 场景 |
|---|---|
| `test_dry_run_prints_diff_no_side_effects` | T1 状态 dry_run，不调任何 add/update |
| `test_sync_config_splits_old_into_two_streams` | T1 → `update_stream AATS_EVENTS` + `add_stream AATS_EVENTS_MARKET` |
| `test_sync_config_noop_when_already_new` | T2 → 两 stream 都不动 |
| `test_sync_config_updates_capacity_drift` | T3 → 只 update AATS_EVENTS_MARKET |
| `test_sync_config_creates_both_from_empty` | T4 → 两次 add_stream |
| `test_sync_config_handles_incomplete_old_stream` | T5 → update + add 混合 |
| `test_sync_config_raises_on_weird_partial_state` | T6 → **raise RuntimeError**（含人工恢复 3 步提示） |
| `test_purge_calls_purge_stream_for_each` | `--purge` 时两 stream 都 purge |
| `test_rejects_conflicting_flags` | `--recreate + --sync-config` raise |

### 9.5 真跑烟测计划

在工作包 1 + 3 + 4 全部 commit 后，按顺序：

1. **(pre-check)** 读两个 stream 的 info 确认状态：
   ```bash
   docker exec aats-nats wget -qO- http://localhost:8222/jsz?streams=1 \
     | jq '.account_details[].stream_detail[] | {name, state: {messages, bytes}, config: {subjects, max_bytes, max_msgs, max_age}}'
   ```
   预期看到只有 `AATS_EVENTS`（老状态：`max_bytes=-1`，`bytes ≈ 1e9`，`messages ≈ 451_979`），**没有** `AATS_EVENTS_MARKET`。

2. **(dry run)** 跑 migration dry run：
   ```bash
   docker exec aats-gateway python scripts/nats_stream_migrate.py --dry-run
   ```
   预期输出：
   - `AATS_EVENTS`: subjects 减少（market/feature snapshots 移除），max_bytes: -1 → 4_294_967_296，max_age 保持
   - `AATS_EVENTS_MARKET`: NOT FOUND → will add_stream with 2 subjects, 1d / 2GB / 5M msgs / 1MB msg size

3. **(apply)** 跑 migration sync + purge：
   ```bash
   docker exec aats-gateway python scripts/nats_stream_migrate.py --sync-config --purge
   ```
   预期：
   - `AATS_EVENTS` update_stream + purge_stream → state.messages=0 state.bytes=0 config 新
   - `AATS_EVENTS_MARKET` add_stream → 空新 stream

4. **(recompose)** 重启 4 进程：
   ```bash
   docker compose -f docker-compose.aats.yml -f docker-compose.aats.derivatives.yml --env-file .env.wsl2 restart
   ```

5. **(启动日志验证)** 查 ensure_streams 日志：
   ```bash
   docker logs aats-gateway 2>&1 | grep nats_jetstream_stream_ | head -20
   ```
   预期看到：
   - `nats_jetstream_stream_unchanged stream=AATS_EVENTS`
   - `nats_jetstream_stream_unchanged stream=AATS_EVENTS_MARKET`
   - 每个 stream 各一条 `nats_jetstream_stream_ensured`
   （因为 migration 已经把 stream 带到目标状态，ensure_streams 不应该再改）

6. **(观察 15 分钟)**：
   ```bash
   docker logs -f aats-market | grep -E 'okx_market_message_error|market_snapshot_published'
   docker logs -f aats-decision | grep -E 'decision_cycle|degradation'
   ```
   预期：无 10023 错误，market 持续 publish，decision 产生周期 log。

7. **(双 stream 水位探测)**：
   ```bash
   docker exec aats-nats wget -qO- http://localhost:8222/jsz?streams=1 \
     | jq '.account_details[].stream_detail[] | {name, state: {messages, bytes}, utilization_pct: ((.state.bytes / .config.max_bytes) * 100)}'
   ```
   预期：
   - `AATS_EVENTS_MARKET`: messages 快速增长（market tick 速率，1-20/秒），bytes 增长快但远低于 2 GB max_bytes
   - `AATS_EVENTS`: messages 慢速增长（decision cycle + 状态事件），bytes 增长慢
   - 两个 stream 的 `utilization_pct` 都应该在 < 20% 范围内

8. **(goal 验证)** 查看 execution engine 是否有下单尝试：
   ```bash
   docker logs aats-execution | grep -E 'order_intent_received|order_placed|okx_rest_order'
   ```
   如果 strategy 产生 intent 但下单链路还有其他问题，那是后续 slice 话题，不阻塞本 slice 完成。

### 9.6 RUNBOOK.md §9.10 补全

把 9.5 的真跑烟测步骤文档化到 runbook，给未来升级或故障恢复留可操作手册，并专门写一节 "两 stream 架构下的运维技巧"：
- 水位监控命令按 stream 分列
- market stream 预警线（bytes > 1.6 GB 时预警）
- events stream 预警线（bytes > 3.4 GB 时预警）
- 回滚合并为单 stream 的手动步骤

## 10. 验收标准

### 10.1 代码质量

- 所有单测绿：
  - 工作包 1：**5 条**（Slice 6.5 前置：4 新 + 1 回归）
  - 工作包 3：**21 条新增**（§7.7）+ 4 条老 Slice 6.5 测试 FakeStreamInfo mock 字段扩展（语义不变）
  - 工作包 5：**9 条**（§9.4 migration 脚本测试）
  - 合计 = **35 条新/扩展单测**
- 集成测试继续绿：`tests/integration/test_nats_event_bus_roundtrip.py` 5 个 roundtrip case 通过 legacy shim 运行（带 DeprecationWarning），其中 1 条（T2 例外）改一行
- black + ruff 无 error
- mypy 无新 error（strict 模式）

### 10.2 真跑行为

- 4 进程 docker compose 启动成功
- shim 日志（compose hardening slice 的）+ `nats_jetstream_stream_ensured × 2`（两个 stream 各一次，本 slice 的）都如预期出现
- **15 分钟连续运行 0 次 `insufficient resources` 错误**
- `AATS_EVENTS_MARKET` bytes 增长快速（与 market tick 频率匹配），`AATS_EVENTS` bytes 增长缓慢（decision cycle 频率）
- 两个 stream 的 utilization % 都 < 20%（远离任何水位告警线）
- market.snapshots 的回放窗口严格等于 1 天（不受 AATS_EVENTS 的容量压力影响）
- critical 事件的回放窗口严格等于 7 天（不受 market tick 高频写入影响）

### 10.3 不变量验证

所有 I-1 ~ I-11 手工核对 + 单测覆盖。

| ID | 验证状态（目标） |
|---|---|
| I-1 容量和 < 8 GB × 0.85 | `test_total_stream_capacity_within_server_budget` |
| I-2 容量差异触发 update_stream | `test_ensure_streams_updates_when_max_bytes_differs` 等 4 条 |
| I-3 StreamSpec 非法值 raise | StreamSpec 构造 8 条测试 |
| I-4 discard=OLD + max_bytes 双保险 | StreamSpec.to_nats_stream_config 单测 |
| I-5 三分支事件向后兼容 | 4 个 legacy + 7 个新三分支测试 |
| I-6 migration 脚本幂等 | migration 9 条测试 |
| I-7 nats server conf 独立回滚 | runbook §9.10 文档 |
| I-8 分层归属完整 | `test_stream_specs_cover_all_critical_topics_exactly_once` |
| I-9 MARKET 窄约束 | `test_market_stream_spec_only_contains_market_and_feature_snapshots` |
| I-10 Legacy shim 不污染 runtime | grep 审查 + runtime 启动日志审计 |
| I-11 对称 max_msg_size | `test_default_stream_specs_have_symmetric_max_msg_size` |

### 10.4 回滚路径

每个 commit 单独 `git revert` 可回滚，不会把前面 commit 的 benefit 也回掉。

## 11. 风险与已知限制

### 11.1 归属漂移风险（分层 stream 特有）

**风险**：未来有人给 `aats/events/topics.py` 加新 critical topic 时，不知道应该放 MARKET stream 还是 EVENTS stream，随手归到 EVENTS 里。如果新 topic 其实是高频观察类（譬如 `orderbook.snapshots` 或 `tick_depth.snapshots`），归错会让 EVENTS stream 被挤爆，重新回到 pre-slice 的病状。

**缓解**：
- **I-8 不变量 + 单测** 强制：`test_stream_specs_cover_all_critical_topics_exactly_once` 会在任何新 topic 加入 `DEFAULT_CRITICAL_TOPICS` 但没归到任一 spec 时测试失败
- **I-9 不变量 + 单测** 强制：`test_market_stream_spec_only_contains_market_and_feature_snapshots` 锁死 MARKET spec 的窄归属，任何扩展必须动单测 + 同步设计文档
- **代码注释**：`DEFAULT_CRITICAL_TOPICS` 常量声明附近加醒目注释"加新 topic 时必须同步判断归属 MARKET or EVENTS"
- runbook 加运维 checklist："新 critical topic 加入前的归属评审步骤"

### 11.2 两 stream subjects 漂移风险

**风险**：升级链路中，`AATS_EVENTS` 里的老 subjects（含 market/feature snapshots）没被 migration 脚本正确剥离，结果 `ensure_streams` 运行时发现 `AATS_EVENTS.existing_subjects ⊋ AATS_EVENTS.spec.topics`（老 subjects 多出来），走 `update_stream` 把多余 subjects "下线"，然后 `AATS_EVENTS_MARKET.add_stream` 试图声明同一个 subject —— NATS 会拒绝，因为 **一个 subject 不能同时被多个 stream claim**。

**缓解**：
- migration 脚本的 T1 转换步骤必须**先** update_stream AATS_EVENTS 移除 market/feature subjects，**后** add_stream AATS_EVENTS_MARKET。顺序颠倒会死锁
- 单测 `test_sync_config_splits_old_into_two_streams` 验证调用顺序
- 脚本实现用 `await` 严格串行，不并发
- 真跑烟测步骤 9.5.1 验证 pre-check 能看到 subject 冲突源

### 11.3 ensure_streams 容量比较维度可能发散

**风险**：如果 nats-py 未来版本把 `StreamInfo.config` 返回的容量字段单位改了（譬如 max_age 从秒变成纳秒），现有的 `existing_cfg.max_age != spec.max_age_seconds` 比较会一直返回 True，每次启动都触发 `update_stream`。

**缓解**：
- 锁死 nats-py 主版本（`nats-py>=2.0,<3.0`）
- 升级 nats-py 时必须重跑 ensure_streams 真跑烟测
- runbook 加"升级 nats-py 前必须跑的检查清单"一节

### 11.4 8 GB 磁盘占用

**风险**：WSL2 宿主如果磁盘紧张（< 20 GB 空闲），8 GB JetStream + Loki/Jaeger 各自几 GB，可能挤爆。

**缓解**：
- 工作包 4 的 runbook 加 pre-check 步骤：`df -h /var/lib/docker`
- 如果空闲 < 15 GB 推迟 slice 并改方向
- 用户已经选了 γ（8 GB），假定磁盘充裕

### 11.5 purge 丢数据（用户已确认接受）

**风险**：`--purge` 丢掉 1 GB 历史观察数据。

**决策**：用户明确回复 "所有环境都是分库分表的，测试不影响"，**接受 dev 环境 purge**。生产环境不允许 `--purge`，runbook 里要写死。

### 11.6 两 stream durable consumer 数量翻倍

**已知影响**（非风险）：现在 decision 进程要订阅 `market.snapshots` 这个 topic，原来 durable consumer 的 stream target 是 `AATS_EVENTS`，升级后变成 `AATS_EVENTS_MARKET`。nats-py 的 durable consumer 是 per-stream 的，这意味着：
- 升级后 `decision-market-snapshots` consumer 会在 AATS_EVENTS_MARKET 上重新创建（全新 durable name 以避免和老的 AATS_EVENTS 里的 consumer 冲突）
- 老的 AATS_EVENTS 里的 `decision-market-snapshots` consumer 变成孤儿；migration 脚本需要顺带清理（新增 --cleanup-orphan-consumers flag）
- consumer 数量从 N 增加到 N + 拆出来的 topic 数（本 slice 只拆 2 个 topic，所以 +2 consumer）

**缓解**：
- `scripts/nats_stream_migrate.py` 加 `--cleanup-orphan-consumers` flag
- runbook 记录这个语义变化
- 本 slice 没有 consumer 配额压力（默认 256 max_ack_pending × 每个 consumer 独立，不影响总量）

### 11.7 Legacy shim 长期存在的 drift 风险

**风险**：`NatsEventBus.ensure_stream(topics=...)` legacy shim 保留给测试环境使用，但长期保留可能导致：
1. **认知漂移**：新开发者不知道哪条路径是规范，可能在 runtime 代码里误调 legacy path
2. **API 分裂**：legacy shim 的测试级默认容量（128 MB / 10K msg）和 runtime 的生产级容量（GB / 500M msg）差 10-1000x，如果哪天 legacy 被误用到生产，stream 容量会被严重低估
3. **维护成本**：同一个 `_ensure_single_stream` 方法要同时兼容两条路径的 FakeStreamInfo 结构和真实 StreamInfo 结构

**缓解**：
- **I-10 不变量** 硬约束：runtime 启动日志不应包含 `DeprecationWarning`
- **Code review 规则**：新 commit 如果引入 `ensure_stream(topics=...)` 调用必须在 PR 里说明为什么不能用 `ensure_streams()`
- **文档强标注**：§7.4 开头注释"legacy shim 仅供测试使用"
- **Follow-up slice**：本 slice 不做，但下一个 NATS 相关 slice 可以考虑把集成测试全部迁移到新 API，彻底删除 legacy shim
- **时间预期**：legacy shim 保留 ≤ 3 个月，到期 review 是否还有使用方

## 12. 实施顺序（写代码时的 step-by-step）

### Step 1: commit Slice 6.5 前置
```bash
git add aats/bus/nats_bus.py tests/unit/test_nats_bus_skeleton.py
pytest tests/unit/test_nats_bus_skeleton.py -xvs
# 5 绿后：
git commit -F .git/COMMIT_MSG_1.txt   # feat(nats-bus): ensure_stream 幂等 upsert
```

### Step 2: commit 本设计
```bash
git add docs/task/slice_nats_jetstream_capacity_fix_design.md
git commit -F .git/COMMIT_MSG_2.txt   # docs(slice-nats-capacity): 设计
```

### Step 3: 写代码（工作包 3 —— StreamSpec + 分层 stream + 容量）

**3a. 新 API 基础设施** — `aats/bus/nats_bus.py`：
  - 新增 `StreamSpec` dataclass（slots + frozen + __post_init__ 校验 + num_replicas/duplicate_window_seconds/deny_purge 三个新字段）
  - `StreamSpec.to_nats_stream_config(subject_prefix)` 方法
  - 新增 `DEFAULT_MARKET_STREAM_TOPICS` / `DEFAULT_CRITICAL_EVENTS_TOPICS` 常量（派生自 `DEFAULT_CRITICAL_TOPICS`）
  - 新增 `DEFAULT_AATS_EVENTS_MARKET_SPEC` / `DEFAULT_AATS_EVENTS_SPEC` / `DEFAULT_STREAM_SPECS` 常量（都用 4 MB max_msg_size 保持对称）
  - `NatsBusConfig.streams` 字段 + `__post_init__` 归属互斥校验（保留 `stream_name` + `stream_max_age_seconds` 作为 legacy 字段）
  - `NatsBusConfig.spec_for_topic(topic)` 辅助方法

**3b. 新 runtime API** — 继续 `aats/bus/nats_bus.py`：
  - 新增 `async def ensure_streams(self)` 无参版本：迭代 `self._config.streams`，每个调 `_ensure_single_stream(spec)`
  - 新增 `async def _ensure_single_stream(self, spec: StreamSpec)` 私有方法：Slice 6.5 三分支 upsert 扩展到容量比较维度（max_bytes + max_msgs + max_msg_size + max_age_seconds）
  - 修改 `async def start(self, *, topics=None)`：如果 `topics is not None` 走 legacy shim 路径（发 warning），否则走新 `ensure_streams()` 路径

**3c. Legacy shim 保留** — 继续 `aats/bus/nats_bus.py`：
  - 修改 `async def ensure_stream(self, topics=None)`：发 DeprecationWarning，从 legacy 字段 + 测试级默认容量（128 MB / 10K msg / 4 MB msg size）构造一次性 StreamSpec，调 `_ensure_single_stream(spec)`
  - 保留 `topics` kwarg 签名（被 `test_ensure_stream_signature_takes_topics_keyword` 断言）

**3d. Runtime caller 改动**（§7.5a.1）：
  - **R1** `aats/bootstrap/config.py:2941` `_construct_event_bus`：
    ```python
    nats_config = NatsBusConfig(
        servers=(runtime_settings.nats_url,),
        streams=_build_nats_streams_from_env(DEFAULT_STREAM_SPECS),
        # stream_name/stream_max_age_seconds 走 legacy 默认，runtime 不用
    )
    ```
  - **R2** `aats/bootstrap/config.py:2986` `_start_event_bus`：**不改**。`await start_method()` 已经是无 topics 参数，新 `start()` 无参会走 `ensure_streams()`
  - **R3** `aats/bus/nats_bus.py:760` `HybridEventBus.start()`：把 `await critical_start(topics=critical_topics)` 改成 `await critical_start()`

**3e. Runtime 配置层** — `aats/bootstrap/config.py`（或等价位置）：
  - 新增 `_build_nats_streams_from_env(default_specs: tuple[StreamSpec, ...])` 函数
  - 读 `AATS_NATS_{MARKET,EVENTS}_MAX_{BYTES,MSGS,MSG_SIZE,AGE_SECONDS}` env var
  - 返回 override 后的 tuple

**3f. Legacy 测试例外**（§7.5a.3）：
  - **T2 例外** `tests/integration/test_nats_event_bus_roundtrip.py:264`：
    - Old: `await verifier.start(topics=None)`
    - New: `await verifier.connect()`
  - **其他 11 个测试 call site** 全部**不改**，依赖 legacy shim 自动走通

**3g. 新单测** — `tests/unit/test_nats_bus_skeleton.py` 加 18 条：
  - StreamSpec 构造/校验 6 条
  - 默认 SPEC 常量归属（验证 I-8/I-9/I-1）3 条
  - NatsBusConfig 多 stream 支持 2 条
  - ensure_streams 三分支 × 多 stream 7 条

**3h. 老单测 FakeStreamInfo 扩展**：
  - 旧的 4 个 Slice 6.5 三分支单测的 FakeStreamInfo mock 需要加 `max_bytes`/`max_msgs`/`max_msg_size`/`max_age` 字段（默认值匹配 legacy shim 构造的一次性 StreamSpec：128 MB / 10K / 4 MB / stream_max_age_seconds），否则新的容量比较逻辑会把它们识别为 drift 永远走 update_stream 分支
  - 影响测试：`test_ensure_stream_unchanged_when_subjects_match`、`test_ensure_stream_updates_when_subjects_differ`、`test_ensure_stream_updates_when_subject_removed`、`test_ensure_stream_passes_max_age_in_seconds_not_nanoseconds`
  - 这些测试的断言语义**不变**，只是 mock 字段要扩充让 legacy shim 的容量比较能判等

**3i. 真跑验证**：
  - `pytest tests/unit/test_nats_bus_skeleton.py -xvs` — 5 老 + 18 新 = **23 条全绿**
  - `pytest tests/integration/test_nats_event_bus_roundtrip.py -xvs` — legacy shim 通过（带 DeprecationWarning）
  - ruff / black / mypy 全过

**3j. Commit**：`git commit -F .git/COMMIT_MSG_3.txt` → feat(nats-bus): StreamSpec 抽象 + 分层 stream + 容量参数 + legacy shim 保留

### Step 4: 改配置（工作包 4）
- 改 `deploy/wsl2-dev/nats/nats-server.conf` max_file_store
- 加 `deploy/wsl2-dev/RUNBOOK.md` §9.10
- `docker compose -f ... restart nats` 验证 NATS 重启后 varz 显示新值
- `git commit -F .git/COMMIT_MSG_4.txt` → chore(deploy): server conf + runbook

### Step 5: 多 stream migration 脚本 + 真跑（工作包 5）
- 写 `scripts/nats_stream_migrate.py` 支持多 stream：
  - 命令行 parser 认 `--dry-run / --sync-config / --purge / --recreate / --cleanup-orphan-consumers`
  - 按迁移矩阵 T1-T6 的 6 种出发状态分别处理
  - T1 场景**严格串行**：先 update_stream AATS_EVENTS 剥离 market/feature subjects，后 add_stream AATS_EVENTS_MARKET
- 写 `tests/unit/test_nats_stream_migrate.py` 9 条测试
- 单测全绿：`pytest tests/unit/test_nats_stream_migrate.py -xvs`
- WSL2 真跑按 §9.5 八步走：
  1. pre-check：看到单 AATS_EVENTS 老状态
  2. dry-run：预期 T1 diff 输出
  3. sync+purge：两 stream 各自到位
  4. 4 进程 restart
  5. 启动日志验证 `nats_jetstream_stream_ensured × 2`
  6. 观察 15 分钟无 10023 错误
  7. 两 stream 水位 < 20%
  8. execution engine 有 order_intent log
- `git commit -F .git/COMMIT_MSG_5.txt` → feat(deploy): 多 stream migration + 真跑验证

### Step 6: slice 完成报告
- 5 commits 的总结
- 不变量验证结果
- 真跑截图 / 日志摘录
- 已知 limitation（3 天保留窗口等）
- 下一步建议（follow-up slice 拆 market stream？）

## 13. 附录：术语对齐

- **retention policy**：NATS stream 的消息保留策略。`LIMITS` = 按容量/时间限制保留；`INTEREST` = 按 consumer 兴趣保留；`WORK_QUEUE` = 被 ack 后立即删除
- **discard policy**：stream 满了后怎么处理新消息。`OLD` = 丢最老的消息接新消息；`NEW` = 拒绝新消息保留老消息
- **storage type**：`FILE` = 持久化磁盘；`MEMORY` = 仅内存
- **durable consumer**：跨进程重启的 consumer，JetStream server 端记录 ack 位置
- **insufficient resources (err_code 10023)**：NATS server 级别的资源耗尽错误，通常是 max_file_store 触顶
- **10058 stream name already in use**：`add_stream` 对已存在但 config 不同的 stream 会抛；必须用 `update_stream`
- **jsz endpoint**：NATS 8222 HTTP 监控端口的 /jsz 路径，返回 JetStream 详细状态
- **max_file_store / max_memory_store**：NATS server conf 里的 server 级存储上限
- **max_bytes / max_msgs / max_msg_size**：NATS stream config 里的 stream 级容量上限
