# Microstructure Bronze/Staging Retention Housekeeping SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> **状态**: 实施中（2026-04-23）
> **来源**: [`microstructure_silver_pipeline_gaps_followup_sow.md`](./microstructure_silver_pipeline_gaps_followup_sow.md) §P0-2
> **性质**: 纯 RDP housekeeping / 卫生补漏，不影响 live 交易主链路

## 1. 背景

`docs/design/p1d_phase1a_implementation_design_2026_04_20.md` §6.x 已写出 bronze/staging retention 策略（trades 30d / bbo 14d / books5 14d / staging.market_oi_funding_ticks 7d），但仓库里没有自动清理脚本。长期 bronze.market_trades 每日 ~300MB 无限增长，会导致磁盘与查询性能恶化。

## 2. Scope（严格收窄）

**允许改动文件**
- `scripts/rdp_microstructure_retention.py`（新增）
- `configs/rdp_workflows/data_maintenance.json`（追加 task）
- `tests/unit/scripts/test_rdp_microstructure_retention.py`（新增）
- `docs/task/microstructure_bronze_retention_sow.md`（本文件）

**不改动**
- `rdp_task_db` / daemon / scheduler 源码
- live runtime config / `.env.*`
- silver / gold / event_store 保留策略
- 现有 workflow 名；只是给 `data_maintenance` 追加一个 task

## 3. Retention Plan（常量，设计文档对齐）

| 表 | 保留天数 |
|---|---|
| `bronze.market_trades` | 30 |
| `bronze.market_orderbook_bbo` | 14 |
| `bronze.market_orderbook_books5` | 14 |
| `staging.market_oi_funding_ticks` | 7 |

## 4. 脚本语义

- 默认 **dry-run**：只 `SELECT COUNT(*) WHERE ts < cutoff`，不 DELETE
- 实际删除：**必须** `--apply --confirm`（双层保护，复用 `backfill_event_store_archive.py` 的风格）
- 不读取 / 不打印 `.env.*` 内容
- 每表独立事务；失败 log 并继续跑下一表（housekeeping 不允许一表失败拖垮其他表）
- 脚本 per-table 输出 cutoff / rows / deleted summary 便于回看

**Exit codes**（与 backfill 脚本一致）：
- `0` = apply 成功
- `2` = dry-run 完成
- `3` = DB / 参数错误
- `4` = `--apply` 未 `--confirm` 的保护错误

## 5. Workflow 挂载位置

追加到 `configs/rdp_workflows/data_maintenance.json` 的 `tasks` 数组末尾（在 `artifact_index_rebuild` 之后）：

- **位置选择**：放在末尾（索引重建之后）。retention 是 best-effort housekeeping，不应延后 daily_ingest / gold / gap 检测这些硬链路；放在最后失败了也不影响前置产出。
- `allow_failure: true` 让 DB 偶发问题（锁等待、pg_stat 查询抖动）只导致 `data_maintenance` degraded，不硬 fail。
- command：`python scripts/rdp_microstructure_retention.py --apply --confirm`（workflow 场景直接执行）

## 6. 测试（最窄）

单测文件 `tests/unit/scripts/test_rdp_microstructure_retention.py`：
1. `--apply` 缺 `--confirm` → exit 4（保护层）
2. 默认无 `--apply` / `--dry-run` → 走 dry-run 分支，exit 2
3. retention plan 常量严格等于 `{trades:30, bbo:14, books5:14, oi_funding_ticks:7}`
4. dry-run 路径通过 monkeypatch 注入 fake session，验证对 4 张表各跑一次 COUNT 并汇总
5. apply 路径通过 monkeypatch 验证调用 DELETE SQL 并输出 deleted rows
6. `data_maintenance.json` 解析：新任务存在、command 指向该脚本、带 `--apply --confirm`、`allow_failure=true`

不触真实 Postgres。session / execute 用 monkeypatch。

## 7. 非目标

- 不修 P0-1（scheduler gap）、P1-4（runner watermark）、P1-3/P2-6（silent skip 观测）、P2-5（oi_price_regime），那些属于 followup SOW 的其它条目
- 不调 retention 天数（严格按设计文档）
- 不对 silver / gold / event_store 表做任何动作

## 8. 审批

| 角色 | 姓名 | 日期 | 意见 |
|---|---|---|---|
| 起草 | Claude | 2026-04-23 | 实施中 |
| 触发启动 | 用户 | 2026-04-23 | 批准（bounded slice） |
