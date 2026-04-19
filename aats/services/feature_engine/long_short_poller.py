"""LongShortRatioPoller — OKX Rubik 大户持仓多空比轮询器 (P2.7).

OKX 的多空比数据不通过 WebSocket 推送，只能 REST 轮询。数据端点:
  GET /api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader
  params: instType=SWAP, ccy=BTC, period=5m|1H, limit=1

数据语义:
  - lsRatio > 1 : 大户账户多头占优 (>0.5 多头 / <0.5 空头比)
  - 极端 lsRatio (> 4 或 < 0.25) 通常是"情绪顶/底" 反转信号

本 poller 定时拉取，维护 per-symbol 最新缓存 (ts, ls_ratio)，FeatureCalculator
通过 ``latest(symbol)`` 读取。缓存 >15 min 视为过期 (stale)，此时 FeatureCalculator
应该视 ls_alpha = 0.

Best-effort: REST 失败不 raise，保留上次有效值 + 记录 last_error.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("aats.feature_engine.long_short_poller")


@dataclass(frozen=True, slots=True)
class LongShortRatioSample:
    """(symbol, ts, ls_ratio) 的只读三元组。"""
    symbol: str
    ts: datetime
    ls_ratio: float


def _symbol_to_ccy(symbol: str) -> str:
    """BTC-USDT-SWAP → BTC, BTC-USD-SWAP → BTC, ETH-USDT-SWAP → ETH."""
    upper = symbol.upper()
    parts = upper.split("-")
    if not parts or not parts[0]:
        raise ValueError(f"invalid_symbol_for_ls_ratio: {symbol!r}")
    return parts[0]


class LongShortRatioPoller:
    """Per-process poller；后台 async task 周期性轮询后供 FeatureCalculator 读."""

    def __init__(
        self,
        *,
        okx_rest_url: str,
        poll_interval_seconds: float = 300.0,
        timeout_seconds: float = 10.0,
        period: str = "5m",
    ) -> None:
        self.okx_rest_url = okx_rest_url
        self.poll_interval_seconds = poll_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.period = period
        self._cache: dict[str, LongShortRatioSample] = {}
        self._last_error: str | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

    def latest(self, symbol: str) -> LongShortRatioSample | None:
        """线程安全读取最新样本（asyncio 单线程内不需要锁，此注释供未来参考）."""
        return self._cache.get(symbol.upper())

    def status(self) -> dict[str, Any]:
        return {
            "cache_size": len(self._cache),
            "last_error": self._last_error,
            "poll_interval_seconds": self.poll_interval_seconds,
            "period": self.period,
            "cached_symbols": list(self._cache.keys()),
        }

    async def stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self, symbols: Iterable[str]) -> None:
        """后台 loop - 轮询给定 symbols 直到 stop 事件触发."""
        symbols_tuple = tuple(dict.fromkeys(s.upper() for s in symbols if s))
        if not symbols_tuple:
            log.info("long_short_poller_no_symbols_noop")
            return

        log.info(
            "long_short_poller_started symbols=%s period=%s interval=%ss",
            symbols_tuple, self.period, self.poll_interval_seconds,
        )
        # 首轮立即拉取 (不等 interval); 后续按 interval 周期
        try:
            await self._poll_round(symbols_tuple)
        except Exception as exc:  # best-effort first-round
            log.warning("long_short_poller_initial_round_failed: %s", exc)

        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.poll_interval_seconds,
                )
                return  # stop 触发，退出
            except (asyncio.TimeoutError, TimeoutError):
                pass  # 正常 interval 到期
            try:
                await self._poll_round(symbols_tuple)
            except Exception as exc:  # 单轮失败不终止 loop
                log.warning(
                    "long_short_poller_round_failed: %s",
                    exc,
                )
                self._last_error = f"{type(exc).__name__}: {exc}"

    async def _poll_round(self, symbols: tuple[str, ...]) -> None:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            for symbol in symbols:
                try:
                    sample = await self._poll_one(client, symbol)
                    if sample is not None:
                        self._cache[symbol] = sample
                        self._last_error = None
                except Exception as exc:
                    self._last_error = f"{symbol}: {type(exc).__name__}: {exc}"
                    log.warning(
                        "long_short_poller_symbol_failed symbol=%s error=%s",
                        symbol, exc,
                    )

    async def _poll_one(
        self, client: httpx.AsyncClient, symbol: str,
    ) -> LongShortRatioSample | None:
        ccy = _symbol_to_ccy(symbol)
        url = (
            f"{self.okx_rest_url}/api/v5/rubik/stat/contracts/"
            "long-short-account-ratio-contract-top-trader"
        )
        resp = await client.get(
            url,
            params={"ccy": ccy, "period": self.period, "limit": "1"},
        )
        resp.raise_for_status()
        body: Any = resp.json()
        if not isinstance(body, dict):
            return None
        if str(body.get("code")) != "0":
            log.warning(
                "long_short_api_error symbol=%s msg=%s",
                symbol, body.get("msg"),
            )
            return None
        data = body.get("data")
        if not isinstance(data, list) or not data:
            return None
        # OKX 返回结构: data = [[ts_ms, lsRatio], ...] 降序
        row = data[0]
        if not isinstance(row, list) or len(row) < 2:
            return None
        try:
            ts_ms = int(row[0])
            ls_ratio = float(row[1])
        except (ValueError, TypeError):
            return None
        if ls_ratio <= 0:
            return None
        return LongShortRatioSample(
            symbol=symbol,
            ts=datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc),
            ls_ratio=ls_ratio,
        )
