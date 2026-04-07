"""Rolling ingestion driver — daily-batch friendly.

⚠️ HISTORY (2026-04-07)
─────────────────────────────────────────────────────────────────
本模块原本包含一套基于"分钟级 cadence bucket"的调度机制 (~150 行),
用于支撑 rdp_realtime_daemon.py 的 60s tick 模式。该 daemon 模式已废弃
(详见 docs/operations/rdp_scheduling_strategy.md "数据采集迁移到日批"),
对应的 cadence/bucket 状态机也一并删除:

  - _TF_SECONDS / _FUNDING_CADENCE_SECONDS  (cadence 表)
  - _last_candle_bucket / _last_funding_bucket  (in-memory dedup state)
  - _bucket_for_timeframe / _bucket_for_funding  (bucket 计算)
  - _is_on_cadence_boundary  (60s 边界检测)
  - _should_fire_candle / _should_fire_funding  (gating 函数)
  - run_scheduler_loop  (循环驱动入口)

新的"日批模式"由 scripts/rdp_run_daily_ingest.py 直接调用本模块的
run_one_rolling_cycle, 它会无条件对每个 (symbol, timeframe) 增量采集
(基于 checkpoint, max_pages 控制回拉范围)。
─────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging

from aats.data_platform.collectors.rolling.candles_api_collector import collect_candles_incremental
from aats.data_platform.collectors.rolling.funding_api_collector import collect_funding_incremental
from aats.data_platform.config import ResearchPlatformSettings, get_settings
from aats.data_platform.db import get_session
from aats.data_platform.merge.merge_pipeline import (
    ValidationBlockedError,
    run_candle_merge_pipeline,
    run_funding_merge_pipeline,
)

log = logging.getLogger(__name__)


def run_one_rolling_cycle(
    settings: ResearchPlatformSettings | None = None,
    *,
    max_pages: int = 30,
) -> None:
    """Execute a single rolling ingestion + merge cycle.

    本函数无条件对所有 (symbol, timeframe) 增量采集——增量边界由各 collector
    内部的 checkpoint 控制, 不再受任何"分钟边界 / cadence bucket"约束。

    日批模式下推荐 max_pages>=30 (默认), 足够覆盖 24h+ 数据。
    灾后恢复或追历史时可显式传入更大的 max_pages。

    Args:
        settings: 可选的 settings 实例 (默认从 get_settings 加载)
        max_pages: 单次 collect 的最大分页数, 透传给 collector
    """
    settings = settings or get_settings()

    # ── Candles ──
    if settings.rolling_candles_enabled:
        for symbol in settings.rolling_candles_symbols:
            for tf in settings.rolling_candles_timeframes:
                try:
                    with get_session(settings) as session:
                        run_id = collect_candles_incremental(
                            session, settings,
                            symbol=symbol, timeframe=tf,
                            max_pages=max_pages,
                        )
                    with get_session(settings) as session:
                        run_candle_merge_pipeline(
                            session, symbol=symbol, timeframe=tf, ingest_run_id=run_id,
                        )
                except ValidationBlockedError:
                    log.warning(
                        "Candle merge blocked by quality gate: %s %s", symbol, tf,
                    )
                except Exception:
                    log.exception("Rolling candle failed: %s %s", symbol, tf)

    # ── Funding ──
    if settings.rolling_funding_enabled:
        for symbol in settings.rolling_funding_symbols:
            try:
                with get_session(settings) as session:
                    run_id = collect_funding_incremental(
                        session, settings,
                        symbol=symbol,
                        max_pages=max_pages,
                    )
                with get_session(settings) as session:
                    run_funding_merge_pipeline(
                        session, symbol=symbol, ingest_run_id=run_id,
                    )
            except ValidationBlockedError:
                log.warning("Funding merge blocked by quality gate: %s", symbol)
            except Exception:
                log.exception("Rolling funding failed: %s", symbol)
