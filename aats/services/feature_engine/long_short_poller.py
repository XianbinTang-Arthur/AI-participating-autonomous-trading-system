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
        # B-1 审查修复: asyncio.Event 必须在 running loop 上构造，否则
        # stop() 与 run_forever() 内的 wait() 可能绑不同 loop → stop 无效、
        # shutdown 挂住。_build_market_slice 在同步 build_runtime 路径里调
        # __init__, 那时通常还没有 running loop. 因此延迟到 run_forever 首行.
        self._stop_event: asyncio.Event | None = None

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
        # B-1 审查修复: _stop_event 在 run_forever 首行惰性创建。若 stop() 在
        # run_forever 启动前被调用 (极罕见)，构造一个空 event 并 set，下一次
        # run_forever 检测到已 set 会立即返回.
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        self._stop_event.set()

    async def run_forever(self, symbols: Iterable[str]) -> None:
        """后台 loop - 轮询给定 symbols 直到 stop 事件触发."""
        # B-1 审查修复: 在 running loop 上延迟构造 asyncio.Event. 如果 stop()
        # 先调了 (init 过早)，_stop_event 已非 None 并 set，本方法会立即退.
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        if self._stop_event.is_set():
            log.info("long_short_poller_already_stopped_on_start")
            return

        symbols_tuple = tuple(dict.fromkeys(s.upper() for s in symbols if s))
        if not symbols_tuple:
            log.info("long_short_poller_no_symbols_noop")
            return

        log.info(
            "long_short_poller_started symbols=%s period=%s interval=%ss",
            symbols_tuple, self.period, self.poll_interval_seconds,
        )
        # B-3 审查修复: 把首轮和主循环的 poll 调用统一到一个路径，loop 开头
        # 就检查 _stop_event，首轮失败也不会阻塞 shutdown.
        first_iteration = True
        while not self._stop_event.is_set():
            if not first_iteration:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.poll_interval_seconds,
                    )
                    return  # stop 触发，退出
                except (asyncio.TimeoutError, TimeoutError):
                    pass  # 正常 interval 到期
            first_iteration = False
            try:
                await self._poll_round(symbols_tuple)
            except Exception as exc:  # 单轮失败不终止 loop
                log.warning(
                    "long_short_poller_round_failed: %s",
                    exc,
                )
                self._last_error = f"{type(exc).__name__}: {exc}"

    async def _poll_round(self, symbols: tuple[str, ...]) -> None:
        # B-2 + R2-M2 审查修复: _last_error 只有在**本轮所有 symbol 都新获得**
        # sample 时才清。不能用 "cache.get(s) is not None" 判断 —— 那会让
        # 上一轮已缓存的 symbol 即使本轮失败也保持 cache 非 None，误清 last_error。
        success_this_round: set[str] = set()
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            for symbol in symbols:
                try:
                    sample = await self._poll_one(client, symbol)
                    if sample is not None:
                        # R3-M4 审查修复: 首次成功样本打 info, 后续只在失败时打
                        # warning. Ops 启动时能看到 ls_alpha 预热期完成的明确信号.
                        was_first = symbol not in self._cache
                        self._cache[symbol] = sample
                        success_this_round.add(symbol)
                        if was_first:
                            log.info(
                                "long_short_poller_first_sample symbol=%s "
                                "ls_ratio=%.4f ts=%s",
                                symbol, sample.ls_ratio, sample.ts.isoformat(),
                            )
                    else:
                        # 静默失败 (code!=0 或 empty data) — 记录可见诊断
                        self._last_error = f"{symbol}: empty_or_non_zero_code"
                except Exception as exc:
                    self._last_error = f"{symbol}: {type(exc).__name__}: {exc}"
                    log.warning(
                        "long_short_poller_symbol_failed symbol=%s error=%s",
                        symbol, exc,
                    )
        if success_this_round == set(symbols):
            # 仅当本轮所有 symbol 都拿到新 sample 才清错误
            self._last_error = None

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
