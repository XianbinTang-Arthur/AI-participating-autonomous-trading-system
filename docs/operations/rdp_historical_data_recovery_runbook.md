# RDP 历史数据恢复与持续采集运行手册

> 文档状态：现行操作说明
> 最后核对：2026-08-26（实现基线 `fe5596fd5ee4`；derivatives generation `fe5596fd5ee4-20260826T151737Z-392-3966`）
> 核对范围：当前代码、迁移、CLI、单元契约及本次受控 derivatives 模拟部署；数据库覆盖、磁盘容量、网络和 collector 连续性会漂移，执行时必须重新验证
> 安全边界：只允许 RDP research/governance 数据库与 `derivatives` 模拟栈；禁止 live profile、真实订单和参数 apply

本手册是历史数据恢复、raw archive、持续采集、retention 和历史 bundle 重建的当前操作入口。任务背景保留在 [`../task/rdp_historical_data_recovery_and_collection_hardening_sow_2026_08_26.md`](../task/rdp_historical_data_recovery_and_collection_hardening_sow_2026_08_26.md)，但执行时以本页和当前代码为准。

## 1. 先理解三类事实

| 类型 | 示例 | 当前处理 |
| --- | --- | --- |
| 可由官方来源回填 | confirmed candle、funding settlement、官方成交、官方 L2 文件、mark-price bar | 保留 raw checksum、来源、时间语义、gap 和 bundle；通过资格门后研究使用 |
| 只能从现在持续采集 | 本地 BBO/books5 采样、连接代次、drop/flush、公共 `liquidation-orders` 事件 | 建立 collector continuity；过去缺失保持 `prospective_only`，不得插值 |
| 可从合格输入重建 | 历史 orderbook/trade-flow Silver、后续研究 artifact | 只从 `ELIGIBLE` bundle 按代码/transform 版本确定性重建 |

mark-price bar 是代理数据，只能进入允许 proxy 的研究角色；它不能证明 tick 级 mark、实时 collector 完整或真实成交。官方 L2 replay、OHLCV bar proxy 和模拟订单生命周期必须分别陈述，不能互相替代。

## 2. 停止条件

遇到以下任一条件立即停止当前阶段：

- 目标数据库或 profile 无法确认；
- raw/archive 目录不是绝对路径，或磁盘低于安全水位；
- 来源不是可验证的 OKX 官方端点/文件；
- checksum、schema、时间范围、单位或半开边界异常；
- 已有同 key 来源/bundle 与新证据不一致；
- 归档未进入 `DELETE_ELIGIBLE` 却准备执行 retention；
- collector 有 drop/flush failure、陈旧或未知，却准备把窗口标为完整；
- 需要展示 `.env.*`、DSN、token、API key 或账户标识才能继续；
- 动作可能启动 live profile、真实订单或参数应用。

所有失败都保留真实状态。不得为了让完整 RDP 通过而伪造数据、heartbeat、gap classification、intent lineage 或 readiness。

## 3. WSL2 标准 Python 环境

WSL2 规定路径是 `~/aats-venv`。如果缺失，从已同步的仓库运行受版本控制的 bootstrap：

```bash
cd ~/aats
bash scripts/bootstrap_wsl2_venv.sh
~/aats-venv/bin/python -c 'import aats, pyarrow, sqlalchemy; print("WSL_VENV_READY")'
~/aats-venv/bin/python scripts/verify_dependency_locks.py
```

脚本固定 Python/uv 版本，验证 uv 发布资产 SHA-256，并按 Linux hash lock 安装；CI lock 同时包含隔离 PostgreSQL 集成所需的 testcontainers/driver。若目标已存在但不是有效 venv，或 Python 版本不符，脚本失败关闭，不会自动删除目录。

Windows 静态检查仍使用：

```powershell
.\.venv\Scripts\python.exe -m ruff check aats\
.\.venv\Scripts\python.exe -m pytest tests\unit -x -q
```

## 4. 第一步：只读覆盖审计

覆盖审计在 PostgreSQL `REPEATABLE READ, READ ONLY` 快照中读取全部 98 张当前 ORM 表；有时间列的表只扫描给定窗口，无时间列的表只读 planner estimate。数据库已用完全一致的主键/唯一约束禁止重复时，审计直接报告重复为 0，不再重复执行高成本分组扫描。它不会建表、补数或修改状态。

```bash
cd ~/aats
~/aats-venv/bin/python scripts/rdp_audit_data_coverage.py --window-days 90
```

输出写入 `artifacts/data_governance/coverage/coverage_<UTC>.json|md`，包含：

- Git、脱敏数据库指纹、查询时间和半开窗口；
- 表存在性、行数、最早/最晚时间、版本、ingest run、自然键重复和规则化缺口；
- `missing`、`collector_unknown`、`zero_event_with_healthy_collector`、`audit_failed` 的不同语义；
- `official_backfill`、`deterministic_rebuild`、`prospective_only`、`cannot_recover` 恢复分类。

任何 `audit_failed` 必须先修复审计本身，不能直接进入导入或重建。

2026-08-26 的最后一次 v5 现场基线为 `coverage_20260826T151922950145Z.json`（SHA-256 `53672eb8f548cc41472d1082d5e793b4d721b0238bedc6a2f7bdee55d96b3607`）：`audit_failed=0`，但仍有 47 个 dataset 缺失、23 个 dataset 存在质量问题。该快照用于审计追溯，不得代替下一次操作前的新审计。

## 5. 第二步：先归档到期 live 原始事实

默认只发现分区，不写文件：

```bash
cd ~/aats
~/aats-venv/bin/python scripts/rdp_archive_microstructure.py --archive-root /root/aats-data/rdp-archive
```

确认目标分区、预计行数和磁盘后才执行：

```bash
~/aats-venv/bin/python scripts/rdp_archive_microstructure.py --archive-root /root/aats-data/rdp-archive --apply --confirm
```

每个 UTC 日/表/symbol 分区使用 repeatable-read 只读快照流式写 Parquet；发布前验证源行数、Parquet metadata、SHA-256 和 manifest。重复运行只接受完全一致的不可变 artifact；部分孤儿文件必须由操作员隔离，不能覆盖。

retention 默认 dry-run：

```bash
~/aats-venv/bin/python scripts/rdp_microstructure_retention.py --dry-run
```

只有所有到期分区均存在唯一 `DELETE_ELIGIBLE` 记录，且路径、SHA-256、manifest 和行数全部一致，才允许：

```bash
~/aats-venv/bin/python scripts/rdp_microstructure_retention.py --apply --confirm
```

验证对任一分区失败时，整个 retention 事务删除 0 行。

## 6. 第三步：官方历史数据分级导入

统一命令默认只输出计划；实际写入必须同时使用 `--apply --confirm`。时间必须携带 offset，窗口统一为 `[start, end)` UTC，raw 目录必须是绝对路径。

### 6.1 官方成交 REST：先做 1 个 UTC 日

```bash
~/aats-venv/bin/python scripts/rdp_import_official_history.py trade-rest --symbol BTC-USDT-SWAP --start 2026-08-01T00:00:00Z --end 2026-08-02T00:00:00Z --raw-archive-dir /root/aats-data/rdp-raw
```

计划核对后：

```bash
~/aats-venv/bin/python scripts/rdp_import_official_history.py trade-rest --symbol BTC-USDT-SWAP --start 2026-08-01T00:00:00Z --end 2026-08-02T00:00:00Z --raw-archive-dir /root/aats-data/rdp-raw --apply --confirm
```

### 6.2 官方文件：trade 或 L2

```bash
~/aats-venv/bin/python scripts/rdp_import_official_history.py l2-file --symbol BTC-USDT-SWAP --start 2026-08-01T00:00:00Z --end 2026-08-02T00:00:00Z --input /absolute/path/official-l2-file.zip --raw-archive-dir /root/aats-data/rdp-raw
```

L2 每次最多一个 UTC 日。文件先流式复制到不可变 raw archive，再解析 sequence/action/bids/asks；因果重采样只使用 `source_state_ts <= sample_ts` 的状态，连续缺口压缩成 range，不能跨 gap forward-fill。

L2 完整性证据必须按来源 schema 与数据日期解释：当前 OKX 订单簿协议以 `seqId`/`prevSeqId` 连续性为主；官方在 2026-06-23 起将 WebSocket 增量频道的 `checksum` 标为废弃并固定返回 `0`。因此，固定 `0` 既不能证明完整，也不能单独判失败；只有历史文件明确采用旧版非零 checksum 语义时才校验该字段。无论哪种版本，raw 文件/响应本身的 SHA-256 始终强制保留，且它与订单簿 payload checksum 不是同一概念。

### 6.3 mark-price bar proxy

```bash
~/aats-venv/bin/python scripts/rdp_import_official_history.py mark-rest --symbol BTC-USDT-SWAP --timeframe 15m --start 2026-08-01T00:00:00Z --end 2026-08-02T00:00:00Z --raw-archive-dir /root/aats-data/rdp-raw
```

只有 confirmed bar 会写入；缺失 bar 形成 gap。输出明确标记 proxy，不得满足 tick 级门。

### 6.4 confirmed OHLCV 与 funding 深度回填

旧入口保留兼容，但已接入 raw archive、重试、数据库检查点、gap 和 bundle：

```bash
~/aats-venv/bin/python scripts/rdp_deep_backfill_api.py --symbol BTC-USDT-SWAP --timeframes 15m 1H --days 1 --dry-run
~/aats-venv/bin/python scripts/rdp_deep_backfill_funding.py --symbols BTC-USDT-SWAP --days 1 --dry-run
```

实际执行必须指定 raw 目录：

```bash
~/aats-venv/bin/python scripts/rdp_deep_backfill_api.py --symbol BTC-USDT-SWAP --timeframes 15m 1H --days 1 --raw-archive-dir /root/aats-data/rdp-raw
~/aats-venv/bin/python scripts/rdp_deep_backfill_funding.py --symbols BTC-USDT-SWAP --days 1 --raw-archive-dir /root/aats-data/rdp-raw
```

funding 覆盖依据实际 `fundingTime`；脚本报告实际观察到的结算间隔，不假定固定 8 小时。OHLCV/funding 对未知字段、非法 OHLC、非有限数值、错误标的、分页停滞和最大页数耗尽均失败关闭；bundle 行数使用窗口内唯一时间戳，不把跨页重复计为新事实。重试耗尽后，已提交批次作为断点保留，但顶层操作返回失败且不会登记一个“完整成功” bundle。

## 7. 第四步：资格与确定性重建

导入完成后，系统按 source kind、角色、raw hash、覆盖率、gap、因果检查、schema/transform/Git 生成 bundle。只有 `status=ELIGIBLE` 的历史 bundle 可重建：

```bash
~/aats-venv/bin/python scripts/rdp_rebuild_historical_bundle.py --bundle-id <bundle-uuid>
```

确认计划后：

```bash
~/aats-venv/bin/python scripts/rdp_rebuild_historical_bundle.py --bundle-id <bundle-uuid> --apply --confirm
```

当前重建输出隔离在 bundle-scoped 历史 Silver，不写 live-capture Silver：

- `silver.historical_orderbook_metrics_15m`；
- `silver.historical_trade_flow_15m`。

operation key 绑定 bundle fingerprint、Git 和 transform version。同一输入重复运行返回既有成功证据；输入或来源发生变化必须形成新证据，不能改写旧 bundle。

## 8. 第五步：持续采集和有效零

`derivatives` 模拟部署会启动公共采集 daemon。微观结构频道是实际 `trades`、BBO/books5 及 OI/funding/mark；不是历史文档中的 `trades-all`。公共强平使用 `liquidation-orders` WebSocket，没有被当前实现信任的强平历史 REST 回填。

continuity 账本记录 connect/reconnect/disconnect、真实入站 frame/message、flush、drop、shutdown、connection generation、ingest run 和时间语义。DB outage、flush failure、buffer hard cap 和 continuity queue overflow 均形成 drop/gap；存在 drop 的 ingest run 不得报告 `succeeded`。

强平是稀疏事件。零行只能在同一窗口有持续入站连接帧且没有 drop/disconnect 时解释为有效零；没有 continuity 证据时状态必须是 `collector_unknown`。

## 9. UI、API 与告警

Operator 的 RDP Workspace 展示“数据覆盖、历史导入、实时采集、不可变归档、质量资格、确定性重建、监控告警”。数据来自服务端统一快照：

- `GET /rdp/v3/workspace`：页面单一读模型；
- `GET /rdp/v3/data-governance`：治理专题只读快照。

请求路径只读最新覆盖 artifact 与 bounded meta 聚合，不扫描 raw tick 表。页面中的“治理快照可用”只表示快照传输与 schema 可读，不表示数据完整、候选盈利或 production-ready。

hourly reliability cycle 会把治理快照不可用、collector 陈旧/drop、开放 gap、归档失败/积压、重建失败、低磁盘和 ineligible bundle 提升为 current alert。任何 critical 告警都必须先处理。

## 10. 分级扩展与容量门

严格按以下顺序推进：

1. 1 个 UTC 日：记录下载字节、raw 文件数、行数、解析/入库时间、数据库增长、gap 和 UI；
2. 30 日 research window：只有第 1 级全部可解释且容量有余量才批准；
3. 目标 90 日：只有 30 日重建、资格和查询性能稳定后批准。

任何阶段都不得降低 raw SHA-256、适用版本的序列/完整性证据、gap、因果或来源门来换取更快完成。L2 文件或账户历史需要额外来源/只读授权时，保持 `awaiting_source`/`UNKNOWN`，不要改用第三方数据冒充生产真相。

## 11. 部署与验收

代码必须先提交。Windows 到 WSL2 的唯一部署入口：

```bash
bash scripts/deploy.sh --profile derivatives --skip-commit
```

不要手工运行 Compose 或 rsync。部署后至少验证：应用容器、RDP daemon、两个 collector、stage 18 schema/ledger、`/healthz`、`/system/health`、`/system/recovery`、治理快照、collector freshness 和最新覆盖 artifact。

完整 RDP 只可在模拟环境触发。recommendation 可以生成和审阅，但本手册不授权 apply；live attribution 或 execution reconciliation 缺只读事实时保持 NO-GO/UNKNOWN。

最后一次完整模拟 RDP（`task_235c5e4eb2a7` / `run_ff3e022b420444f7`）在约 7 秒内开始、10 个步骤全部完成，但结果为 `blocked_by_attribution`：四个策略/周期组合的精确 replay/live 对齐均为 0，系统动作均为 `pause`。这证明立即调度与失败关闭生效，不构成研究通过或参数应用授权。

## 12. 故障恢复

| 故障 | 预期行为 | 恢复 |
| --- | --- | --- |
| API 429/5xx/网络断开 | 指数退避；耗尽后失败关闭 | 保留 raw/数据库断点，修复网络后重复同一窗口 |
| raw checksum 冲突 | 拒绝覆盖 | 隔离冲突文件，核对来源与 operation key |
| 损坏 ZIP/schema | 不写成功 bundle | 重新取得官方文件，保留失败证据 |
| DB outage/flush failure | collector 记录 drop/gap，run 非 succeeded | 恢复 DB 后重启采集；过去不可恢复窗口保持 gap |
| 归档中断 | 完整 orphan 可校验续跑；partial orphan 失败 | 人工隔离 partial 文件，再重复分区 |
| 磁盘不足 | archive/import 停止 | 扩容或清理非证据数据；不得先运行 retention |
| 重建中断 | run 标记 FAILED | 同 operation 受控重试；旧成功 fingerprint 不覆盖 |

所有运行结论都必须记录时间、commit/profile、数据库脱敏指纹、operation/run/bundle id、输入窗口和实际验证范围。

## 13. 官方事实来源

- [OKX API v5 文档](https://app.okx.com/docs-v5/en)：端点、分页参数、字段和 WebSocket 频道语义；
- [OKX API v5 更新日志](https://app.okx.com/docs-v5/log_en/)：协议变更日期与 `checksum` 废弃状态。

外部接口会变化。每次新增来源、扩大历史窗口或升级 schema 前必须重新核对官方文档；本页的核对日期不是未来运行的永久证明。
