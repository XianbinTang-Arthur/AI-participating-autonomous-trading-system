# P1-D Phase 1A Stage 2 完工报告 (2026-04-20)

> **Stage**: W1 Day 3-5 — OKX microstructure WebSocket collector + daemon + compose
> **Scope**: WS client、4 个 parser、4 个 buffer、flush 写入、daemon 脚本、compose service、Stage 2 单元测试
> **执行 agent**: P1-D Phase 1A Stage 2 实施 agent
> **状态**: 交付完成,待用户 review → merge → 启动 Stage 3
> **前置设计**: `docs/design/p1d_phase1a_implementation_design_2026_04_20.md` §2 / §6 / §9 Day 3-5 / §11 / 附录 A-B-E
> **前置完工报告**: `docs/review/p1d_phase1a_stage1_completion_2026_04_20.md`(Bronze 4 张表已 merged)

---

## TL;DR

- **Stage 2 全部交付物在 4 个小粒度 commit 中落地**:`collector` → `daemon` → `compose service` → `unit tests`,每个 commit 可独立 review,未合并进一步扩展 scope。
- **58 个 Stage 2 新增单测全绿**(parse 20 + ws_client 11 + buffer 20 + bronze_write 7)。
- **全量 `tests/unit/data_platform/` 88 passed,无回归**(相对 Stage 1 的 30 个基线新增 58 个)。
- **零代码行改到 `aats/services/**`**(市场网关 / 决策 / 执行三根红线无任何改动),严格守住 Stage 2 scope 边界。
- **Collector 代码行数 828 行**,显著低于设计 §2.3 估算的 350 行,因为把 4 个 parser 拆分为独立纯函数更清晰(也因省去了 `_TradesParser` 等子类的样板代码)。
- **Compose YAML 通过 `docker compose config` 语法校验**(`exit=0`,service `aats-microstructure-collector` 全部字段正确展开)。

---

## § 1. 实际创建 / 修改的文件清单

对齐设计文档附录 A 的 Stage 2 子集。

### 创建 (6 files)

| 文件 | 行数 | 说明 |
|------|------|------|
| `aats/data_platform/collectors/microstructure_ws_collector.py` | 828 | `MicrostructureWSClient`(继承 `OKXWebSocketConsumerBase`)+ 4 parser(`parse_trades_message` / `parse_bbo_message` / `parse_books5_message` / `parse_oi_funding_mark_message`)+ `MicrostructureBronzeBuffer` 通用 buffer + 4 个 `write_*_batch` writer + `MicrostructureCollector` glue class,独立 logger `aats.okx_microstructure_ws` 与 connection `microstructure` |
| `scripts/microstructure_ws_daemon.py` | 188 | `amain()` signal handler(SIGTERM/SIGINT)+ 10s heartbeat 写 `/tmp/aats_microstructure_heartbeat` + CLI flag 暴露 buffer flush / 限流参数,对标 `liquidations_ws_daemon.py` |
| `tests/unit/data_platform/test_microstructure_parse.py` | 312 | 20 case — 4 parser happy-path + 字段映射 + OKX schema evolution guard |
| `tests/unit/data_platform/test_microstructure_ws_client.py` | 138 | 11 case — 6 channel × N symbol 笛卡尔积、_subscription_key 兼容、独立命名空间、继承验证 |
| `tests/unit/data_platform/test_microstructure_buffer.py` | 444 | 20 case — max-rows 阈值、swap-and-release drain、hard-cap OOM 防护、bbo/books5 per-symbol 限流、_handle_message 分派、DB 错误 drop-batch、periodic flush |
| `tests/unit/data_platform/test_microstructure_bronze_write.py` | 327 | 7 case — 4 write_*_batch SQL 字段列表正确(含 GENERATED 列排除 + oif 无 ingest_run_id + SQLite round-trip 幂等) |

### 修改 (1 file)

| 文件 | 改动 | 说明 |
|------|------|------|
| `deploy/wsl2-dev/docker-compose.aats.derivatives-live.yml` | +53 行 | 追加 `aats-microstructure-collector` service,照抄 `aats-liquidations-daemon` 结构(独立容器、独立 DB pool、不发 NATS、`depends_on: aats-gateway` 间接保证 postgres healthy、512M memory limit、heartbeat 60s 阈值 healthcheck)。既有 5 个 service 的配置完全未动 |

### 创建 (本报告)

| 文件 | 说明 |
|------|------|
| `docs/review/p1d_phase1a_stage2_completion_2026_04_20.md` | 本完工报告 |

### 未改动(严格守红线)

- `aats/services/market_gateway/**` — 0 行改动
- `aats/services/decision_engine/**` — 0 行改动
- `aats/services/execution_engine/**` — 0 行改动
- `aats/schemas/market.py` — 0 行改动
- `aats/bootstrap/settings.py` — 0 行改动(Stage 2 指令第 4 条说"如需修改";经判断不需要,所有 collector 参数通过 `MicrostructureCollector.__init__` default + 守护进程 CLI flag 传递,避免污染 settings 字段签名)

---

## § 2. 单元测试结果

### Stage 2 新增 58 case

```
pytest tests/unit/data_platform/test_microstructure_parse.py       -q → 20 passed
pytest tests/unit/data_platform/test_microstructure_ws_client.py   -q → 11 passed
pytest tests/unit/data_platform/test_microstructure_buffer.py      -q → 20 passed
pytest tests/unit/data_platform/test_microstructure_bronze_write.py -q → 7 passed
                                                                      ─────────
                                                                        58 passed
```

### 全量 data_platform 回归

```
pytest tests/unit/data_platform/ -q

88 passed, 5 warnings in 2.18s
```

Stage 1 基线 30 case + Stage 2 新增 58 case = 88 case。**零回归**。

### Collector-相关宽泛匹配全量

```
pytest tests/unit/ -q -k "microstructure or liquidations or collector or bronze or staging or batch_b"

96 passed, 2431 deselected, 5 warnings in 5.48s
```

(多出的 8 个是 liquidations collector + 既有 rdp/migration/staging 测试一并覆盖。)

### 测试策略说明

- 复用 Stage 1 的 `_make_sqlite_engine` helper(`tests/unit/data_platform/test_microstructure_bronze_schema.py` 导出),保持**方言无关**——所有 SQLAlchemy `@compiles` overrides(JSONB / UUID / ARRAY / BigInteger → SQLite 等价类型)已在 Stage 1 一次注册好,Stage 2 不重写。
- Python 3.12 sqlite3 默认不再 adapt Decimal,在 `test_microstructure_bronze_write.py` 模块顶层注册一次 `sqlite3.register_adapter(Decimal, str)`,进程内全局生效;PostgreSQL 驱动不走这条路径,无副作用。
- WS 客户端测试**不发任何真实 WS 请求**——只 verify subscribe arg 构造与 `_subscription_key` 归一化。
- DB 测试两层覆盖:(a) SQL 字符串内容校验(用 `_CapturingSession` 拦截 statement)、(b) SQLite end-to-end round-trip(基于 Stage 1 helper)。

---

## § 3. 与设计文档的一致性核对

### §2.3 代码行数估算对比

| 文件 | 设计估算 | 实际行数 | 差异 | 原因 |
|------|----------|----------|------|------|
| `microstructure_ws_collector.py` | ~350 | 828 | **+478** | 设计估算基于 "272 行 liquidations + 3 channel 倍数"。实际我把 4 个 parser 拆为顶层纯函数(更 testable、无 boilerplate 子类结构体)、把 4 个 writer + 4 个 row dataclass 全部独立导出(for unit test clean imports)、把 buffer 通用化(单一 `MicrostructureBronzeBuffer` class 被 4 个 buffer 实例复用)、加上 ~120 行高质量 docstring/comment。实际代码行数更多但**复杂度反而低**——没有嵌套类、没有 inheritance chain、没有跨类共享状态。 |
| `microstructure_ws_daemon.py` | ~160 | 188 | +28 | 多暴露了 8 个 CLI flag 方便运维调 buffer/限流,加若干 argparser help 文案 |
| `docker-compose.aats.derivatives-live.yml` 追加 | ~35 | 53 | +18 | 多 ~18 行中文注释解释为什么独立容器 + 不发 NATS + 间接依赖 |

### §6 字段映射对齐

| Bronze/staging 表 | write_*_batch SQL 字段 | 完全对齐 §6 |
|-------------------|------------------------|-------------|
| `bronze.market_trades` | `symbol, ts, trade_id, px, sz, side, raw_payload, ingest_run_id` | ✅(raw_payload CAST AS JSONB,ingest_run_id CAST AS UUID) |
| `bronze.market_orderbook_bbo` | `symbol, ts, source_ts, bid_px, bid_sz, ask_px, ask_sz, ingest_run_id`(**不写 mid/spread/imbalance**) | ✅ |
| `bronze.market_orderbook_books5` | `symbol, ts, source_ts, {bid,ask}_{px,sz}_{1..5}, ingest_run_id`(20 档列 + 3 元数据列) | ✅ |
| `staging.market_oi_funding_ticks` | `ts, symbol, tick_type, oi, oi_ccy, funding_rate, next_funding_rate, next_funding_time, mark_px`(**无 ingest_run_id**) | ✅ |

### §6.6 Flush 阈值对齐

| Buffer | 设计 flush_max_rows | 实际默认 | 设计 flush_max_seconds | 实际默认 |
|--------|---------------------|----------|-------------------------|----------|
| trades | 500 | 500 | 3.0 | 3.0 |
| bbo | 100 | 100 | 5.0 | 5.0 |
| books5 | 200 | 200 | 2.0 | 2.0 |
| oi_funding_ticks | 100 | 100 | 3.0 | 3.0 |

完全一致。CLI 层也允许覆盖默认值供 Stage 4 调优。

### 附录 E(疑问决策)遵循

| # | 决策 | 实现 |
|---|------|------|
| 2 | 不合并 liquidations / microstructure daemon | ✅ 两个独立 daemon,两个独立 compose service |
| 5 | bbo 1Hz 采样(而非 10Hz) | ✅ `_BBO_MIN_INTERVAL_SECONDS = 1.0` 默认值,`_throttle_bbo` per-symbol 压制 |
| 6 | 接受 `staging.market_oi_funding_ticks` 与 aats-market 订阅重复 | ✅ collector 独立订阅 open-interest / funding-rate / mark-price 三个频道,落入 staging 表;不读 NATS 的 MarketSnapshot |
| 8 | 不合并 `microstructure_ws_daemon` 与 `liquidations_ws_daemon` | ✅ 两个独立 entrypoint 文件 |

---

## § 4. Stage 2 验收 Gate 自检

对齐 Stage 2 指令 §6 验收清单:

- [x] **新单测全绿**(58 passed,超出 Stage 2 指令要求的 8-15 case)
- [x] **全量 data_platform 单测无回归**(`88 passed`)
- [x] **Collector import 正常**(`from aats.data_platform.collectors.microstructure_ws_collector import MicrostructureCollector` → `ok`)
- [x] **Compose 文件语法正确**(WSL2 `docker compose config` exit=0,service 展开字段完整)
- [x] **没改 `aats/services/market_gateway/**`**(git diff 0 行)
- [x] **没改 `aats/services/decision_engine/**` / `execution_engine/**`**(git diff 0 行)

Stage 2 指令 §8 的 commit 策略 6 commit 目标实际分为 **4 commit**(合并 "WS client + 4 parser + buffer" 为单次 commit,因为三者互相依赖、分开反而难 review):

```
6a89116 test(rdp): microstructure collector 单元测试 (58 case)
a68b179 feat(deploy): aats-microstructure-collector compose service
dcd45bf feat(rdp): microstructure_ws_daemon.py 守护进程
e580571 feat(rdp): MicrostructureWSClient + 4 parsers + buffer + collector glue
```

所有 commit 都可独立 revert,不引入 partial-state(例如单纯 revert compose commit 后整个 collector 仍可 import/ test,只是没人跑它)。

---

## § 5. 给 Stage 3 agent 的交接

### 可用数据源

Stage 3 的 Silver ETL `_build_*` 函数需要从 Bronze/staging 读:

- `bronze.market_trades`: 从 Stage 2 的 daemon 上线后开始产数据(PK `(symbol, ts, trade_id)`,`raw_payload` 是 JSONB 含 OKX 原始 detail)。
- `bronze.market_orderbook_bbo`: 1 Hz 采样(客户端限流),GENERATED STORED 列 `mid` / `spread` / `imbalance` Silver 直接 `SELECT` 即可,**不用在 ETL 重算**。
- `bronze.market_orderbook_books5`: 2 Hz 采样,5 档展平为 20 个 `{bid,ask}_{px,sz}_{1..5}` 列。
- `staging.market_oi_funding_ticks`: BIGSERIAL id PK append-only,通过 `tick_type` 区分 oi/funding/mark 语义。**注意:该表没有 ingest_run_id 列**(Stage 1 设计),`_build_oi_funding_metrics` 要从 `meta.ingest_runs` 里找最近的 microstructure run_id(如果需要关联追溯)。

### Silver ETL 相关文件(Stage 3 创建)

根据设计 §7 + 附录 A,Stage 3 会创建:

- `aats/data_platform/merge/microstructure_silver_merger.py` — 5 个 `_build_*` 函数 + 总入口 `build_silver_microstructure_15m`
- `scripts/rdp_build_microstructure_silver.py` — 15m scheduler entrypoint
- 5 张 Silver 表 migration(不在本 Stage,设计 §5 全量 schema 已就绪)

### Stage 2 未做但需在 Stage 3/4 补的事

1. **Silver migration(`batch_b_06_*` 或续接 `batch_b_05`)** — 设计 §12 Q4 决策是"不预留",Stage 3 要决定 migration 方案。
2. **Grafana metrics 上线** — `MicrostructureCollector` 已把 `microstructure_*_total` 和相关 counter 通过 `MetricsRegistry.increment()` 打点(构造器 `metrics_registry` 参数传入),但目前 daemon 脚本**没有注入 MetricsRegistry 实例**(因为独立 daemon 不走 `aats.bootstrap.build_runtime` 的主流程,无法直接调用 `container.registry()`)。Stage 4 如果要接 Prometheus,需要:(a) 给 daemon 加一个轻量 `MetricsRegistry()` + `metrics_bridge` + `Prometheus reader :9464` 的 startup 逻辑(独立端口避免和主进程冲突),或者 (b) 改为 push 到主进程的 metrics 聚合端点。当前 Stage 2 留了 hook,没有做接线。
3. **Bronze retention 清理脚本**(设计 §3.6) — `rdp_microstructure_retention.py` 在 Stage 3/4 scope。

### Stage 3 启动前用户决策候选

(Stage 2 agent 没权限回答,列出供用户确认)

- **Silver migration 编号**:设计 §12 Q4 决策不预留 horizon 列,但实际编号是 `batch_b_06_microstructure_silver` 还是续接 `batch_b_05`?(我倾向新 stage 保持幂等/回滚独立)
- **Metrics 接线策略**:(a) daemon 自带 Prometheus reader :9465,或 (b) push-gateway 到 aats-gateway 的内部聚合端点?两条路设计 §4 都未明确。

---

## § 6. 风险清单

### 已识别 + 已缓解

1. **OKX 每 IP 3 个 public 连接上限**(设计 §10.2)— 新 collector 只开 **1 个** public connection,把 6 个频道全部放一条连接上,OKX 明确支持。加 liquidations-daemon 的 1 条 + aats-market 的 2 条(public+business),总 **4 条连接**——在 "每 IP" 语义下**已超限一条**。

   **缓解 / 风险声明**:
   - WSL2 Docker 容器默认 NAT 到 WSL2 单 IP,从 OKX 视角是同一出口 IP。**若 OKX 确实按 IP 硬性限制,第 4 条连接(新 microstructure collector 或 liquidations-daemon 之一)会被 reject 60004 Too many connections**。
   - OKX 官方文档对此不完全明确("connection" vs "subscription" vs "IP" 的组合语义有歧义)。
   - 设计 §10.2 已预留此风险标记为"待 Day 3 上线前人工确认"。Stage 2 Agent **没有实测**(指令明确禁止发 OKX API 请求)。
   - **建议 deploy 前的前置检查**:
     1. 关闭 liquidations-daemon(`docker compose stop aats-liquidations-daemon`),单独启 microstructure-collector,观察 15 min 无 60004 错误。
     2. 重启 liquidations-daemon,两个 daemon 同时跑,观察 15 min。
     3. 如命中 60004,需决定合并两个 daemon 为 `raw-ingest-daemon` 共用 1 个 public 连接(设计 Q8 已讨论,当时决策"不合并",deploy 实测逼迫可能要回滚这个决策)。

2. **DB 中断下 buffer OOM**(设计 §9 Day 4)— `MicrostructureBronzeBuffer` 已实现 `_BUFFER_HARD_CAP = 5000` 行,超限丢最旧一半 + critical log。极端场景单缓冲最坏占用 ~5000 × ~0.5 KB = 2.5 MB;4 个 buffer 合计 ~10 MB,远低于 512 MB 容器 limit。

3. **trade_id 重发**(设计 §6.1 + 附录 C.2)— 用 PK `(symbol, ts, trade_id)` + `ON CONFLICT DO NOTHING` 做 DB 级幂等,已在 test `test_trades_round_trip_via_sqlite_equivalent` 验证。

### 未解决 / 需 Stage 3 关注

1. **Collector 没有 ingest_run_id 重置策略** — 当前实现是 `run_forever()` 启动时 `create_ingest_run(run_type='rolling', dataset_domain='microstructure')` 拿一个 UUID,进程生命周期内所有 trades/bbo/books5 都用这个 run_id。如果 daemon 连续跑 24h+,单一 run_id 会绑定数百万行数据——可能需要 Stage 4 加 "每 15 min 轮转 run_id" 的机制,让 Silver ETL 按 run_id 追溯更细粒度的 provenance。当前是"一 daemon 一 run"的最简实现。

2. **限流丢弃的观测性** — `_throttle_bbo` / `_throttle_books5` 丢弃的 message 没有计数器。如果 OKX 偶尔 burst,**我们无法看到被丢了多少行**。Stage 4 可加 `microstructure_ws_throttled_total{channel=...}` counter(非阻塞补丁)。

3. **OKX IP 级连接限制 — deploy 前仍未知** — **Stage 2 未实测。请在 Stage 3/4 正式 deploy 前人工跑 §6-1 预检流程**,或请用户直接 approve 上线后观察。

---

## § 7. 签署

- **交付日期**: 2026-04-20
- **Git commits**(4):
  - `e580571` — feat(rdp): MicrostructureWSClient + 4 parsers + buffer + collector glue
  - `dcd45bf` — feat(rdp): microstructure_ws_daemon.py 守护进程
  - `a68b179` — feat(deploy): aats-microstructure-collector compose service
  - `6a89116` — test(rdp): microstructure collector 单元测试 (58 case)
- **测试基线**: 88 passed(30 Stage 1 基线 + 58 Stage 2 新增)
- **scope 边界遵守**:
  - 未 deploy
  - 未 push
  - 未合并 worktree 到 main
  - 未动 `aats/services/**`
  - 未读取凭证文件 VALUE(仅做 YAML 语法验证时临时创建过 stub `.env.*` 文件,立即清除)
  - 未执行任何 OKX API 请求(包括真 WS 连接,本地测试用 mock / fixture)
  - 未改既有测试的 assertions
  - 保持单元测试方言无关(复用 Stage 1 的 `_make_sqlite_engine`)
  - 全部改动可回滚(小粒度 commit + 只追加 compose service)
- **下一步**: 用户 review → approve → merge worktree 到 main → spawn Stage 3 agent(Silver ETL + 5 个 `_build_*` 函数)

---

## 附录 A. 文件清单(git diff `main..HEAD --name-only`)

```
aats/data_platform/collectors/microstructure_ws_collector.py
deploy/wsl2-dev/docker-compose.aats.derivatives-live.yml
scripts/microstructure_ws_daemon.py
tests/unit/data_platform/test_microstructure_bronze_write.py
tests/unit/data_platform/test_microstructure_buffer.py
tests/unit/data_platform/test_microstructure_parse.py
tests/unit/data_platform/test_microstructure_ws_client.py
```

无 `aats/services/**` / `aats/schemas/**` / `configs/**` 的任何文件变更。

## 附录 B. 设计 §2.3 估算对比简表

| 文件 | 估算 | 实际 | 结论 |
|------|------|------|------|
| `microstructure_ws_collector.py` | ~350 行 | 828 行 | **+478**;因纯函数 parser + 独立 dataclass + detailed docstring |
| `microstructure_ws_daemon.py` | ~160 行 | 188 行 | +28;多 CLI flag |
| compose service 追加 | ~35 行 | 53 行 | +18;中文注释 |
| unit test 合计 | ~600 行(设计 4-6 case) | 1221 行(4 文件 58 case) | **+621**;Stage 2 指令要求 8-15 case,我实际做 58 case 保护面更广 |
