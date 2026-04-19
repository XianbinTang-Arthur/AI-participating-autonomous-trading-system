"""Feature engine 启动预热 — 用 OKX REST 拉历史 K 线初始化 RollingCandleState.

Bug-1 时序平滑修复配套：FeatureCalculator 运行时需要 >= max(roc_window+1,
atr_window+1) 根历史才能输出平滑后的 momentum / volatility。冷启动无历史 →
退化到单 K 线瞬时算法 → Bug-1 未修复。

本模块在 market slice 启动时一次性调用 OKX ``GET /api/v5/market/candles``
（上限 ``limit`` 根），把历史 bars 灌入 state，令系统"冷启动即可用"。

设计原则：
  - 自包含：用 httpx.AsyncClient 直连 OKX 公共端点，不依赖 execution slice
    的 ``OKXRESTClient``（跨进程耦合，且 market slice 冷启动时 execution
    slice 可能尚未就绪）。
  - Best-effort：REST 失败、数据不足等场景一律返回 False + WARNING，不 raise；
    FeatureCalculator 会自动走退化路径（单 K 线瞬时算法）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from aats.schemas.market import KlineBar
from aats.services.feature_engine.timeseries import RollingCandleState

log = logging.getLogger("aats.feature_engine.warmup")

# 内部 canonical timeframe → OKX bar 字符串.
# OKX 用大写 'H' / 'D'，小写 'm' 表示分钟。
_TF_TO_OKX_BAR: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
}


def _parse_candle_row(row: list[str]) -> tuple[datetime, KlineBar] | None:
    """将 OKX 返回的一行 [ts, o, h, l, c, vol, volCcy, volQuote, confirm]
    解析为 (ts, KlineBar)。

    只用闭合（``confirm=1``）的 bar 做预热，避免首次启动拿到半截 bar 导致
    ATR / ROC 异常。
    """
    if len(row) < 9:
        return None
    try:
        ts_ms = int(row[0])
        ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        confirm = row[8] in ("1", "true", "True")
        if not confirm:
            return None
        bar = KlineBar(
            open=Decimal(row[1]),
            high=Decimal(row[2]),
            low=Decimal(row[3]),
            close=Decimal(row[4]),
            volume=Decimal(row[5]) if row[5] else Decimal("0"),
        )
        return ts, bar
    except (ValueError, IndexError, InvalidOperation, OSError):
        return None


async def _fetch_candles(
    *,
    client: httpx.AsyncClient,
    okx_rest_url: str,
    symbol: str,
    okx_bar: str,
    limit: int,
) -> list[list[str]]:
    """Call OKX /api/v5/market/candles 一次，返回原始 data 列表（按 ts 降序）."""
    url = f"{okx_rest_url}/api/v5/market/candles"
    resp = await client.get(
        url,
        params={"instId": symbol, "bar": okx_bar, "limit": str(limit)},
    )
    resp.raise_for_status()
    body: Any = resp.json()
    if not isinstance(body, dict):
        return []
    if str(body.get("code")) != "0":
        raise RuntimeError(f"OKX candles API error: {body.get('msg')}")
    data = body.get("data")
    if isinstance(data, list):
        return data
    return []


async def prewarm_state_from_okx(
    state: RollingCandleState,
    *,
    okx_rest_url: str,
    symbol: str,
    timeframe: str,
    limit: int = 50,
    timeout_seconds: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """拉历史 K 线填入 ``state``。

    Args:
      state: 目标 RollingCandleState
      okx_rest_url: OKX REST base URL（来自 settings.okx_rest_url）
      symbol: instId，例如 "BTC-USDT-SWAP"
      timeframe: canonical timeframe ("15m" / "1h" ...)
      limit: 拉多少根（<= rolling_max_bars）
      timeout_seconds: HTTP 超时
      client: 可选复用 client；None 时本函数自建一次性 client

    Returns:
      ``True``  预热后 state 已 ready（indicators.ready == True）
      ``False`` REST 失败 / 数据不足 / 解析失败 → caller 容忍，FeatureCalculator
                会自动退化到单 K 线瞬时算法
    """
    okx_bar = _TF_TO_OKX_BAR.get(timeframe)
    if okx_bar is None:
        log.warning(
            "feature_warmup_unsupported_timeframe",
            extra={"symbol": symbol, "timeframe": timeframe},
        )
        return False

    owned_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_seconds)
        owned_client = True
    try:
        try:
            data = await _fetch_candles(
                client=client,
                okx_rest_url=okx_rest_url,
                symbol=symbol,
                okx_bar=okx_bar,
                limit=limit,
            )
        except Exception as exc:
            log.warning(
                "feature_warmup_okx_rest_failed",
                extra={
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200],
                },
            )
            return False

        if not data:
            log.warning(
                "feature_warmup_empty_response",
                extra={"symbol": symbol, "timeframe": timeframe},
            )
            return False

        bars: list[tuple[datetime, KlineBar]] = []
        for row in data:
            parsed = _parse_candle_row(row) if isinstance(row, list) else None
            if parsed is not None:
                bars.append(parsed)

        if not bars:
            log.warning(
                "feature_warmup_no_confirmed_bars",
                extra={
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "raw_rows": len(data),
                },
            )
            return False

        state.prewarm(bars)
        indicators = state.indicators()
        log.info(
            "feature_warmup_ok",
            extra={
                "symbol": symbol,
                "timeframe": timeframe,
                "bars_loaded": len(bars),
                "state_bars": indicators.bars_available,
                "state_ready": indicators.ready,
            },
        )
        return indicators.ready
    finally:
        if owned_client:
            await client.aclose()


async def prewarm_many(
    states_by_key: dict[tuple[str, str], RollingCandleState],
    *,
    okx_rest_url: str,
    limit: int = 50,
    timeout_seconds: float = 10.0,
    per_request_delay_seconds: float = 0.1,
) -> dict[tuple[str, str], bool]:
    """顺序预热多个 (symbol, timeframe) state。

    按 OKX 公共数据限速 (20 req / 2s)，默认每次请求后 100ms 间隔即可安全覆盖
    (10 req/s < 10)。对小量 symbol × 2 个 timeframe 这非常宽裕。

    返回 (symbol, timeframe) → 是否 ready 的映射，供 observability 使用。
    全部请求共用一个 httpx.AsyncClient 减少连接建立开销。
    """
    results: dict[tuple[str, str], bool] = {}
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for key, state in states_by_key.items():
            symbol, timeframe = key
            ok = await prewarm_state_from_okx(
                state,
                okx_rest_url=okx_rest_url,
                symbol=symbol,
                timeframe=timeframe,
                limit=limit,
                timeout_seconds=timeout_seconds,
                client=client,
            )
            results[key] = ok
            if per_request_delay_seconds > 0:
                await asyncio.sleep(per_request_delay_seconds)
    return results


def collect_state_keys(
    *,
    symbols: Iterable[str],
    timeframes: Iterable[str] = ("15m", "1h"),
) -> list[tuple[str, str]]:
    """便捷函数：枚举 (symbol, timeframe) 组合，供 caller 批量构建 state."""
    return [(sym, tf) for sym in symbols for tf in timeframes]
