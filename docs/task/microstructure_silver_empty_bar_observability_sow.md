# Microstructure silver empty-bar observability SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../project_positioning.md)。

> **状态**：实施中 (2026-04-23)
> **父 SOW（备忘）**：[`microstructure_silver_pipeline_gaps_followup_sow.md`](microstructure_silver_pipeline_gaps_followup_sow.md) 的 P1-3 / P2-6 切片
> **性质**：纯 platform hygiene / observability；不改 ETL 语义、不触 live 交易链路

---

## 1. 背景

父 SOW 的 P1-3 / P2-6 指出：当 bronze / staging 为空时，`build_silver_microstructure_15m`
仍会写一行 NULL/0 的 silver row 并打 `*_no_data` quality_flag，但

- merger 的 `metrics_registry` 只累积 `etl_runs_success` + `rows_written`，
- runner (`scripts/rdp_build_microstructure_silver.py`) 的日志只打 `COMMITTED`，

于是 Grafana / Loki / oncall 无法把 **"ETL 成功 + 输入有数据"** 和
**"ETL 成功 + 输入为空（bronze 断档）"** 两种状态区分开。Collector 断 4h 后
`etl_failed=0` 仍然绿灯，但 silver 里全是 NULL 行——这是当前观测死角。

## 2. 范围

只在 merger + runner 两个文件、三份 unit test 文件里加 **observability primitives**：

1. merger 的 metrics_registry 新增 per-table no-data counter
   - `microstructure_silver_bars_with_no_data_orderbook_total`
   - `microstructure_silver_bars_with_no_data_trade_flow_total`
   - `microstructure_silver_bars_with_no_data_oi_funding_total`
   - `microstructure_silver_bars_with_no_data_volume_profile_total`
   - `microstructure_silver_bars_with_no_data_liquidation_total`
2. merger final summary log 在 `tables_failed=∅ + 至少一个 *_no_data flag 命中`
   时从 `COMMITTED` 区分为 `COMMITTED_BUT_EMPTY`
3. runner 的 apply+confirm 成功分支同样把 `COMMITTED` 细分成 `COMMITTED_BUT_EMPTY`

## 3. 明确不做

- 不改 ETL 语义：空 bar 继续 UPSERT NULL/0 row、success 不变 error/partial
- 不引入 deploy / compose / Grafana / Prometheus / Loki 配置变更
- 不改 runner exit code 表
- 不动 scheduler / retention / watermark（留给父 SOW 其他切片）
- 不碰 live / runtime / trading config

## 4. 实现骨架

### merger (`aats/data_platform/merge/microstructure_silver_merger.py`)

在模块顶部加常量：每张 silver 表的 "源空" 判定条件（都是 source_no_data flag
子集 — flag 全命中即该表 row 为 NULL/0）。

```python
_TABLE_NO_DATA_TRIGGERS: dict[str, frozenset[str]] = {
    "orderbook":       frozenset({"orderbook_bbo_no_data", "orderbook_books5_no_data"}),
    "trade_flow":      frozenset({"trades_no_data"}),
    "oi_funding":      frozenset({"oi_no_data", "funding_no_data", "mark_no_data"}),
    "volume_profile":  frozenset({"trades_no_data"}),
    "liquidation":     frozenset({"liquidation_no_data"}),
}
```

`build_silver_microstructure_15m` 聚合指标块里，对每张成功写入 (`rowcount>0`
且 `table_key not in tables_failed`) 的表，如果 `_TABLE_NO_DATA_TRIGGERS[table_key]`
⊆ 当前 flags，就 `_record_metric(registry, f"microstructure_silver_bars_with_no_data_{table_key}_total")`。

final summary log 把 `tables_failed=∅ + 任一 *_no_data flag` 的 `COMMITTED`
替换为 `COMMITTED_BUT_EMPTY`，保持 INFO 级别与原先一致（不动告警语义）。

### runner (`scripts/rdp_build_microstructure_silver.py`)

apply+confirm 成功分支在 `log.info("COMMITTED ...")` 之前判定：若
`result.quality_flags` 任一以 `_no_data` 结尾，则改打
`log.info("COMMITTED_BUT_EMPTY ...")`。其他路径（PARTIAL / FAILED / DRY-RUN）
原样不动。

### Tests

- `test_microstructure_silver_pipeline.py`：TestSilverMetricsPlumbing 加一个
  空 bar 用例，确认 5 张表全部命中 `bars_with_no_data_*_total` 计数
- `test_microstructure_silver_trade_flow.py`：加两个 case — happy path 不应
  增加 counter，empty bar 增加
- `test_microstructure_silver_orderbook.py`：同上，对 orderbook 表

## 5. 测试与验证

1. `ruff check` 5 个受影响文件
2. 三份目标 unit test 跑通（最窄）
3. `tests/unit/` 全量回归

## 6. 风险

- **False negative**: 如果某张表只部分 source 断（e.g. orderbook 只断 bbo），
  `_TABLE_NO_DATA_TRIGGERS[orderbook]` 要求两个都命中才 +1，不会误报也
  不会触发——此时 row 仍有部分真实数据，不算 "bars with no data"。
- **False positive**: 计数器仅在对应 no_data flag 全命中时打，正常 bar 不会
  误增；happy-path test 直接断言覆盖。
- **日志消费方**: 已有 Loki query 以 `COMMITTED` 做 substring 匹配的，会
  连同 `COMMITTED_BUT_EMPTY` 一并命中；需要严格区分时改成
  `| = "COMMITTED " or | = "COMMITTED_BUT_EMPTY"` 即可（留给 dashboard
  维护者，SOW 不引入 dashboard 改动）。

## 7. 审批

| 角色 | 姓名 | 日期 | 意见 |
|---|---|---|---|
| 起草 + 实施 | Claude | 2026-04-23 | 范围收敛，仅 observability primitives |
