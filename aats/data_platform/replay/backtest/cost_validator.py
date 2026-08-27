"""Backtest Cost Validator — decision-vs-fill cost 对比诊断（MVP）.

背景
----
AATS 的策略决策层（``ReplayDecision``）产出 ``expected_net_edge_bps``，其内嵌
了一个"假设"的 ``cost_bps``（blended fee + slippage buffer）。但这个假设值
与 fill 侧模拟器（``fill_simulator.FillSimulator``）在后续 execution event
价格/流动性下计算出来的实际 cost（fee_bps + 调用方估算 slippage）可能不一致：

* 如果 live/backtest fee schedule 变化，assumed_cost 就偏离实际；
* 如果决策层低估了 post_only 落空率，那么"穿盘口兜底"后的实际 cost 会比假设高；
* 反向：若决策层按 taker 成本估算，但实际落入 post_only fill，assumed 偏悲观。

Cost Validator 做的事就是把**一个 decision 与其后续因果 fill**配对，计算：

    cost_diff_bps     = actual_cost - assumed_cost
    actual_net_edge   = assumed_net_edge - cost_diff
    edge_flipped_neg  = (assumed_net_edge > 0) and (actual_net_edge <= 0)

从而回答研究问题："在真实成本下，有多少决策会从正 edge 翻成负 edge？"

与 live path 的隔离
-------------------
本模块是 replay 侧的纯聚合器。不做 I/O、不打 log、不访问 DB、不读配置。
调用方自行把 ``ReplayDecision.cost_bps`` / ``ReplayDecision.expected_net_edge_bps``
与 ``FillResult.fee_bps`` + 估算 slippage 映射进来。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from aats.data_platform.replay.backtest.numeric import finite_float


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostDiagnostic:
    """单个决策的 cost 对比结果。

    Attributes:
        decision_id: 标识（调用方自选，例如 ``str(bar_ts_ms)`` 或 ``f"{symbol}:{ts}"``）。
        assumed_cost_bps: 决策层假设的 cost（来自 ``ReplayDecision.cost_bps``）。
        actual_cost_bps: fill 模拟实际产出的 cost（``FillResult.fee_bps`` + slippage 估算）。
        cost_diff_bps: ``actual_cost_bps - assumed_cost_bps``（正 = 实际更贵 = 悲观偏离）。
        assumed_net_edge_bps: 决策时估算的 net edge（来自 ``ReplayDecision.expected_net_edge_bps``）。
        actual_net_edge_bps: 换用 actual cost 后的 net edge = ``assumed_net_edge - cost_diff``。
        edge_flipped_negative: ``True`` 表示 assumed 正、actual 非正（<= 0），即 edge 翻负。
        notes: 调试/审计备注。
        actual_fee_bps: 可选实际手续费分项；历史调用方可省略。
        actual_slippage_bps: 可选实际滑点分项；历史调用方可省略。
        resolved_at_ts_ms: 订单解析完成的 UTC epoch 毫秒；旧产物可为空。
        fill_ts_ms: 实际成交事件的 UTC epoch 毫秒；旧产物可为空。
        equity_attribution_ts_ms: 该成交首次进入 equity curve 的点位时间；
            IOC/open-event 成交通常晚于 fill_ts 一个 bar close，旧产物可为空。
        filled_exchange_quantity / average_fill_price / actual_fee_notional:
            versioned artifact 用于在显式 InstrumentContract 下独立重算手续费。
        fee_currency: ``actual_fee_notional`` 的结算币种。
        fee_asset / fee_asset_quantity: 实际扣费资产与有符号数量；可与
            ``fee_currency`` 不同，用于重建 SPOT base-fee 库存变化。
    """

    decision_id: str
    assumed_cost_bps: float
    actual_cost_bps: float
    cost_diff_bps: float
    assumed_net_edge_bps: float
    actual_net_edge_bps: float
    edge_flipped_negative: bool
    notes: str = ""
    actual_fee_bps: float | None = None
    actual_slippage_bps: float | None = None
    # Keep new fields at the end so existing positional construction remains
    # source-compatible with historical tests and artifact readers.
    resolved_at_ts_ms: int | None = None
    fill_ts_ms: int | None = None
    equity_attribution_ts_ms: int | None = None
    filled_exchange_quantity: Decimal | None = None
    average_fill_price: Decimal | None = None
    actual_fee_notional: Decimal | None = None
    fee_currency: str | None = None
    fee_asset: str | None = None
    fee_asset_quantity: Decimal | None = None

    def __post_init__(self) -> None:
        # Preserve existing constructor compatibility while ensuring the
        # versioned JSON/fingerprint spelling of every zero-valued float is
        # unique.  Other type/finite/identity checks remain in the strict
        # artifact validator so legacy diagnostic readers keep their boundary.
        for field_name in (
            "assumed_cost_bps",
            "actual_cost_bps",
            "cost_diff_bps",
            "assumed_net_edge_bps",
            "actual_net_edge_bps",
            "actual_fee_bps",
            "actual_slippage_bps",
        ):
            value = getattr(self, field_name)
            if type(value) is float and value == 0.0:
                object.__setattr__(self, field_name, 0.0)


@dataclass(frozen=True)
class CostValidationSummary:
    """整批决策的 cost 对比汇总。

    所有 float 字段空批次下返回 ``0.0``。
    """

    total_decisions: int = 0
    decisions_with_fills: int = 0
    avg_cost_diff_bps: float = 0.0
    max_cost_diff_bps: float = 0.0            # 绝对值最大，保留原 sign
    flipped_negative_count: int = 0            # assumed 正 → actual 非正
    flipped_positive_count: int = 0            # assumed 非正 → actual 正
    stable_sign_count: int = 0                 # 无翻转
    p50_cost_diff_bps: float = 0.0
    p95_cost_diff_bps: float = 0.0

    def __post_init__(self) -> None:
        for field_name in (
            "avg_cost_diff_bps",
            "max_cost_diff_bps",
            "p50_cost_diff_bps",
            "p95_cost_diff_bps",
        ):
            value = getattr(self, field_name)
            if type(value) is float and value == 0.0:
                object.__setattr__(self, field_name, 0.0)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class CostValidator:
    """Collects decision-vs-fill cost comparisons and produces summary.

    使用模式：

        validator = CostValidator()
        for decision, fill_result in paired_stream:
            validator.record(
                decision_id=str(decision.ts),
                assumed_cost_bps=decision.cost_bps,
                actual_cost_bps=fill_result.fee_bps + slippage_estimate,
                assumed_net_edge_bps=decision.expected_net_edge_bps,
            )
        summary = validator.summary()

    纯计算、无 I/O、无 logging。实例可重置（调用方新建一个即可，不提供 reset
    方法以避免误用）。
    """

    def __init__(self) -> None:
        self._diagnostics: list[CostDiagnostic] = []

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        decision_id: str,
        assumed_cost_bps: float,
        actual_cost_bps: float,
        assumed_net_edge_bps: float,
        notes: str = "",
        actual_fee_bps: float | None = None,
        actual_slippage_bps: float | None = None,
        resolved_at_ts_ms: int | None = None,
        fill_ts_ms: int | None = None,
        equity_attribution_ts_ms: int | None = None,
        filled_exchange_quantity: Decimal | None = None,
        average_fill_price: Decimal | None = None,
        actual_fee_notional: Decimal | None = None,
        fee_currency: str | None = None,
        fee_asset: str | None = None,
        fee_asset_quantity: Decimal | None = None,
    ) -> CostDiagnostic:
        """记录一次 decision-vs-fill 对比。

        Formula::

            cost_diff       = actual_cost - assumed_cost
            actual_net_edge = assumed_net_edge - cost_diff
            flipped_neg     = (assumed_net_edge > 0) and (actual_net_edge <= 0)

        返回构造好的 ``CostDiagnostic``，并在内部追加。
        """
        assumed_cost = finite_float(
            assumed_cost_bps,
            reason="cost_diagnostic_non_finite",
        )
        actual_cost = finite_float(
            actual_cost_bps,
            reason="cost_diagnostic_non_finite",
        )
        assumed_edge = finite_float(
            assumed_net_edge_bps,
            reason="cost_diagnostic_non_finite",
        )
        cost_diff_bps = finite_float(
            actual_cost - assumed_cost,
            reason="cost_diagnostic_non_finite",
        )
        actual_net_edge_bps = finite_float(
            assumed_edge - cost_diff_bps,
            reason="cost_diagnostic_non_finite",
        )
        edge_flipped_negative = (
            assumed_edge > 0.0 and actual_net_edge_bps <= 0.0
        )

        resolved_ms = _optional_epoch_ms(
            resolved_at_ts_ms,
            field_name="resolved_at_ts_ms",
        )
        fill_ms = _optional_epoch_ms(fill_ts_ms, field_name="fill_ts_ms")
        attribution_ms = _optional_epoch_ms(
            equity_attribution_ts_ms,
            field_name="equity_attribution_ts_ms",
        )
        if (
            fill_ms is not None
            and attribution_ms is not None
            and attribution_ms < fill_ms
        ):
            raise ValueError("equity_attribution_precedes_fill")

        diag = CostDiagnostic(
            decision_id=decision_id,
            assumed_cost_bps=assumed_cost,
            actual_cost_bps=actual_cost,
            cost_diff_bps=cost_diff_bps,
            assumed_net_edge_bps=assumed_edge,
            actual_net_edge_bps=actual_net_edge_bps,
            edge_flipped_negative=edge_flipped_negative,
            notes=notes,
            actual_fee_bps=(
                None
                if actual_fee_bps is None
                else finite_float(
                    actual_fee_bps,
                    reason="cost_diagnostic_non_finite",
                )
            ),
            actual_slippage_bps=(
                None
                if actual_slippage_bps is None
                else finite_float(
                    actual_slippage_bps,
                    reason="cost_diagnostic_non_finite",
                )
            ),
            resolved_at_ts_ms=resolved_ms,
            fill_ts_ms=fill_ms,
            equity_attribution_ts_ms=attribution_ms,
            filled_exchange_quantity=_optional_finite_decimal(
                filled_exchange_quantity,
                field_name="filled_exchange_quantity",
            ),
            average_fill_price=_optional_finite_decimal(
                average_fill_price,
                field_name="average_fill_price",
            ),
            actual_fee_notional=_optional_finite_decimal(
                actual_fee_notional,
                field_name="actual_fee_notional",
            ),
            fee_currency=fee_currency,
            fee_asset=fee_asset,
            fee_asset_quantity=_optional_finite_decimal(
                fee_asset_quantity,
                field_name="fee_asset_quantity",
            ),
        )
        self._diagnostics.append(diag)
        return diag

    def summary(self) -> CostValidationSummary:
        """聚合所有已记录的 diagnostics。

        空集合时返回全零 summary。
        """
        if not self._diagnostics:
            return CostValidationSummary()

        diffs = [d.cost_diff_bps for d in self._diagnostics]
        n = len(diffs)

        avg = finite_float(
            sum(diffs) / n,
            reason="cost_summary_non_finite",
        )

        # max_cost_diff_bps：绝对值最大，保留原 sign
        max_abs_diff = max(diffs, key=lambda v: abs(v))

        flipped_neg = 0
        flipped_pos = 0
        stable = 0
        for d in self._diagnostics:
            assumed_pos = d.assumed_net_edge_bps > 0.0
            actual_pos = d.actual_net_edge_bps > 0.0
            if assumed_pos and not actual_pos:
                flipped_neg += 1
            elif (not assumed_pos) and actual_pos:
                flipped_pos += 1
            else:
                stable += 1

        sorted_diffs = sorted(diffs)
        p50 = sorted_diffs[int(n * 0.5)] if n > 0 else 0.0
        p95_idx = int(n * 0.95)
        if p95_idx >= n:
            p95_idx = n - 1
        p95 = sorted_diffs[p95_idx] if n > 0 else 0.0

        return CostValidationSummary(
            total_decisions=n,
            decisions_with_fills=n,
            avg_cost_diff_bps=avg,
            max_cost_diff_bps=max_abs_diff,
            flipped_negative_count=flipped_neg,
            flipped_positive_count=flipped_pos,
            stable_sign_count=stable,
            p50_cost_diff_bps=p50,
            p95_cost_diff_bps=p95,
        )

    @property
    def diagnostics(self) -> tuple[CostDiagnostic, ...]:
        """已记录所有 diagnostics（不可变 tuple）。"""
        return tuple(self._diagnostics)


def _optional_epoch_ms(value: int | None, *, field_name: str) -> int | None:
    """Validate one optional integer epoch timestamp without float coercion."""

    if value is None:
        return None
    if type(value) is not int:
        raise ValueError(f"{field_name}_must_be_integer")
    return value


def _optional_finite_decimal(
    value: Decimal | None,
    *,
    field_name: str,
) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name}_must_be_finite_decimal")
    return value


__all__ = [
    "CostDiagnostic",
    "CostValidationSummary",
    "CostValidator",
]
