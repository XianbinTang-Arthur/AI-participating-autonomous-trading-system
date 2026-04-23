# Microstructure Silver Runner — Watermark-aware Backfill SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> **状态**：实施中（2026-04-23）
> **来源**：[microstructure_silver_pipeline_gaps_followup_sow.md](./microstructure_silver_pipeline_gaps_followup_sow.md) P1-4
> **范围**：纯 RDP 平台卫生 / 数据连续性 bugfix，**不触 live path**

---

## 1. Bug 现状

[`scripts/rdp_build_microstructure_silver.py`](../../scripts/rdp_build_microstructure_silver.py) 的 `main()` 在 `--bar-start` 未指定时，仅按 `--backfill-bars N` 回看最近 N 根 bar：

```python
for i in range(1, args.backfill_bars + 1):
    bs, be = latest_complete_bar(lookback_bars=i)
    bars.append((bs, be))
```

后果：
- scheduler 每 tick 只跑 `lookback_bars=1`，一根 bar；
- 中间任何 gap（daemon 停机 / collector 断链）**只能靠人肉跑 `scripts/maintenance/microstructure_silver_catchup_*.py` 补**；
- `--backfill-bars=N` 也只是从最新向后 1..N 回溯，跳不过中间 gap。

## 2. 修复目标

默认行为（未传 `--bar-start`）：
1. 查 `silver.market_trade_flow_15m` 的 `MAX(ts)` 作为 watermark；
2. 若 watermark 存在且落后于 `latest_complete_bar()`：按 15m 枚举 `watermark + 15min` 到 latest 的连续 bars，**单次 cap 64 根**（防 stampede / EMA 递归代价爆炸）；
3. 若 watermark 不存在（冷启动 / 空库 / DB 不可达）：fallback 到旧的 `--backfill-bars` 语义，保持冷启动路径可用。

显式 `--bar-start` 语义**不变**：用户指定就只跑该 bar / 该区间。`--backfill-bars` public CLI 参数**保留**，仅在冷启动 fallback 起作用。

## 3. 不做的事

- 不改 workflow JSON / scheduler / daemon；
- 不改 `_run_one_bar` exit code 语义；
- 不新增 DB 表 / 配置文件；
- 不改 live runtime。

## 4. 实现要点

- 新增 helper `_detect_trade_flow_watermark(symbol)`：打开一个独立 session 查 `MAX(ts)`，任何异常 → `None`（视作冷启动）。
- 新增 helper `_resolve_bars_from_watermark(symbol, backfill_bars, watermark_cap=64, watermark=_UNSET)`：
  - `watermark=_UNSET` → 自动调 `_detect_trade_flow_watermark`；
  - 有 watermark → 枚举到 `latest_complete_bar(lookback_bars=1)` 的 `(bar_start, bar_end)`；
  - 超过 cap → 保留最近 `cap` 根（最新优先；更早的缺口留给运维 catchup 脚本兜底）；
  - 无 watermark → 旧 `lookback_bars=1..N` 路径。
- `main()`：未传 `--bar-start` 时调用 helper 而非直接循环。排序路径保留，EMA/baseline 按时间升序执行。

## 5. 测试点

新增 / 改动在 `tests/unit/data_platform/test_microstructure_silver_pipeline.py`：

1. watermark 落后多根 bar → helper 返回 `watermark+15m` 到 latest 的连续 bars；
2. gap > 64 → cap 到最近 64 根；
3. 无 watermark → 退回 `--backfill-bars=N` 行为；
4. 显式 `--bar-start` → 语义不变（`main()` 单根 bar）。

在 `tests/unit/data_platform/test_microstructure_silver_merger_partial_fail.py::TestRunnerExitCode` 中，将 `_detect_trade_flow_watermark` mock 为返回 `None`，保持冷启动 fallback 路径不触 DB，让既有 exit code 断言继续生效。

## 6. 回滚

- `git revert` 单次 commit 即可；
- 无 schema / config 变更。
