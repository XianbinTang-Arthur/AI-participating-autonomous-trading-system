"""Step 5: final reconciliation — compute detailed per-order breakdown and
simulate fee_resolver predictions, compare vs actual derived fee_bps."""
from __future__ import annotations

import os
import statistics
from decimal import Decimal, getcontext
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

getcontext().prec = 40

_aats_root_env = os.environ.get("AATS_ROOT")
if _aats_root_env:
    ROOT = Path(_aats_root_env).resolve()
else:
    _here = Path(__file__).resolve()
    ROOT = _here
    for candidate in (_here, *_here.parents):
        if (candidate / ".env.derivatives.live").exists():
            ROOT = candidate
            break
load_dotenv(ROOT / ".env.wsl2", override=False)
load_dotenv(ROOT / ".env.derivatives.live", override=False)


def get_dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "admin")
    pw = os.environ.get("POSTGRES_PASSWORD")
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("AATS_DB_NAME") or "aats_live_derivatives"
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


# Derivative taker/maker fee in bps (from config defaults, OKX regular tier).
# Reference: configs/templates/base.yml and AATSSettings defaults.
DERIV_TAKER_BPS = Decimal("5.0")
DERIV_MAKER_BPS = Decimal("1.0")


def predict_fee_bps(
    *,
    execution_style: str | None,
    order_type: str | None,  # "market" / "limit" / "ioc" — lowercase
    passive_bias: Decimal = Decimal("0.7"),
    maker_taker_bias: Decimal = Decimal("0"),
) -> Decimal:
    """Mirror of fee_resolver.estimated_execution_fee_bps_decimal (post-H2 fix)."""
    style = str(execution_style or "").lower()
    ot = str(order_type or "").lower()
    # OKX "ioc" and "market" both hit taker.
    if ot in {"market", "ioc"} or style in {"taker", "bounded_taker_cap", "bounded_limit_ioc", "exchange"}:
        return DERIV_TAKER_BPS
    if ot == "limit" or style in {"maker", "passive"}:
        passive = min(max(passive_bias, Decimal("0")), Decimal("1"))
        maker_bias = min(max(-maker_taker_bias, Decimal("0")), Decimal("1"))
        maker_weight = min(
            max(Decimal("0.15") + (passive * Decimal("0.45")) + (maker_bias * Decimal("0.20")), Decimal("0")),
            Decimal("0.80"),
        )
        return (DERIV_TAKER_BPS * (Decimal("1") - maker_weight)) + (DERIV_MAKER_BPS * maker_weight)
    return DERIV_TAKER_BPS


def predict_fee_bps_prefix(
    *,
    execution_style: str | None,
    order_type: str | None,
    passive_bias: Decimal = Decimal("0.7"),
    maker_taker_bias: Decimal = Decimal("0"),
) -> Decimal:
    """Mirror of fee_resolver BEFORE H2 fix — bounded_limit_ioc goes maker-blend."""
    style = str(execution_style or "").lower()
    ot = str(order_type or "").lower()
    if ot == "market" or style in {"taker", "bounded_taker_cap", "exchange"}:
        return DERIV_TAKER_BPS
    if ot == "limit" or style in {"bounded_limit_ioc", "maker", "passive"}:
        passive = min(max(passive_bias, Decimal("0")), Decimal("1"))
        maker_bias = min(max(-maker_taker_bias, Decimal("0")), Decimal("1"))
        maker_weight = min(
            max(Decimal("0.15") + (passive * Decimal("0.45")) + (maker_bias * Decimal("0.20")), Decimal("0")),
            Decimal("0.80"),
        )
        return (DERIV_TAKER_BPS * (Decimal("1") - maker_weight)) + (DERIV_MAKER_BPS * maker_weight)
    return DERIV_TAKER_BPS


def main() -> None:
    with psycopg2.connect(get_dsn()) as conn:
        conn.autocommit = False
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    o.order_id,
                    o.symbol,
                    o.side,
                    o.order_type                                                AS order_type_col,
                    o.time_in_force,
                    o.requested_qty,
                    o.state                                                      AS order_state,
                    o.created_at,
                    o.last_exchange_ts,
                    o.strategy_family,
                    o.strategy_leg_role,
                    o.strategy_sleeve_id,
                    o.product_type,
                    o.margin_mode,
                    o.reduce_only,
                    o.close_only,
                    o.execution_action,
                    o.position_intent,
                    o.raw_payload::jsonb ->> 'execution_style'                   AS style_top,
                    o.raw_payload::jsonb -> 'submission_payload' ->> 'ordType'   AS ord_type_payload,
                    o.raw_payload::jsonb ->> 'cancel_reason'                     AS cancel_reason,
                    o.raw_payload::jsonb ->> 'average_fill_price'                AS avg_fill_price,
                    o.raw_payload::jsonb ->> 'filled_qty'                        AS filled_qty,
                    o.raw_payload::jsonb ->> 'strategy_execution_mode'           AS strategy_execution_mode,
                    o.raw_payload::jsonb -> 'submission_payload' ->> 'maxSlippageToleranceBps' AS max_slippage_tolerance_bps
                FROM public.execution_orders o
                ORDER BY o.created_at ASC
                LIMIT 1000
                """
            )
            orders = cur.fetchall()

            cur.execute(
                """
                SELECT
                    f.fill_id,
                    f.order_id,
                    f.symbol,
                    f.side,
                    f.fill_qty,
                    f.fill_price,
                    f.fee_amount,
                    f.fee_currency,
                    f.liquidity_role,
                    f.exchange_ts
                FROM public.execution_fills f
                ORDER BY f.exchange_ts ASC
                LIMIT 2000
                """
            )
            fills = cur.fetchall()

    # Aggregate fills per order
    fills_by_order: dict[str, list[dict]] = {}
    for f in fills:
        fills_by_order.setdefault(f["order_id"], []).append(f)

    # Per-order breakdown
    print("=" * 100)
    print(f"{'order_id':<20s} {'symbol':<14s} {'side':<5s} {'ordType':<7s} {'style':<6s} "
          f"{'state':<8s} {'fills':<5s} {'notional':<12s} {'fee':<12s} {'act_bps':<8s} "
          f"{'pred_now':<9s} {'pred_pre':<9s}")
    print("=" * 100)

    total_notional = Decimal("0")
    total_fee = Decimal("0")
    fill_rows: list[tuple[str, str, Decimal, Decimal, Decimal, Decimal, Decimal]] = []
    # style_bucket, ord_type, n, notional, fee, act_bps, pred_bps
    aggregate_by_cell: dict[tuple[str, str], dict[str, Decimal]] = {}

    for o in orders:
        order_fills = fills_by_order.get(o["order_id"], [])
        notional = Decimal("0")
        fee = Decimal("0")
        for f in order_fills:
            try:
                q = Decimal(str(f["fill_qty"]))
                p = Decimal(str(f["fill_price"]))
                fe = Decimal(str(f["fee_amount"]))
                notional += q * p
                fee += abs(fe)
            except Exception:  # noqa: BLE001
                pass
        act_bps = (fee / notional * Decimal(10000)) if notional > 0 else Decimal(0)
        # Normalize ordType for prediction
        ot = o["ord_type_payload"] or o["order_type_col"]
        pred_now = predict_fee_bps(execution_style=o["style_top"], order_type=ot)
        pred_pre = predict_fee_bps_prefix(execution_style=o["style_top"], order_type=ot)

        oid = o["order_id"][:18] + ".."
        style_s = str(o["style_top"])[:6]
        print(f"{oid:<20s} {o['symbol'] or '':<14s} {o['side']:<5s} {str(ot):<7s} "
              f"{style_s:<6s} {o['order_state'] or '':<8s} {len(order_fills):<5d} "
              f"{notional:<12.4f} {fee:<12.4f} {act_bps:<8.3f} "
              f"{pred_now:<9.3f} {pred_pre:<9.3f}")

        # Aggregate
        cell_key = (str(o["style_top"]), str(ot))
        cell = aggregate_by_cell.setdefault(cell_key, {
            "n_orders": Decimal(0), "n_fills": Decimal(0),
            "notional": Decimal(0), "fee": Decimal(0),
            "act_bps_sum": Decimal(0),
            "pred_now_sum": Decimal(0),
            "pred_pre_sum": Decimal(0),
            "filled_only_notional": Decimal(0),
            "filled_only_fee": Decimal(0),
        })
        cell["n_orders"] += Decimal(1)
        cell["n_fills"] += Decimal(len(order_fills))
        cell["notional"] += notional
        cell["fee"] += fee
        cell["pred_now_sum"] += pred_now
        cell["pred_pre_sum"] += pred_pre
        if notional > 0:
            cell["act_bps_sum"] += act_bps
            cell["filled_only_notional"] += notional
            cell["filled_only_fee"] += fee
            fill_rows.append((str(o["style_top"]), str(ot), notional, fee, act_bps, pred_now, pred_pre))

        total_notional += notional
        total_fee += fee

    print("=" * 100)
    print(f"TOTALS: notional={total_notional:.2f} fee={total_fee:.4f} USDT")
    if total_notional > 0:
        avg_bps = total_fee / total_notional * Decimal(10000)
        print(f"         overall actual fee_bps = {avg_bps:.3f}")

    # --- Summary table ---
    print("\n### Summary by (execution_style, OKX ordType) ###\n")
    print(f"{'style':<10s} {'ordType':<8s} {'n_ord':<6s} {'n_fill':<7s} "
          f"{'notional':<14s} {'actual_bps':<11s} {'pred_now':<10s} {'pred_pre_H2':<12s} "
          f"{'diff_now':<10s} {'diff_pre':<10s}")
    for (style, ot), cell in sorted(aggregate_by_cell.items()):
        n = int(cell["n_orders"])
        nf = int(cell["n_fills"])
        # Compute weighted bps only among filled orders
        if cell["filled_only_notional"] > 0:
            actual_bps = cell["filled_only_fee"] / cell["filled_only_notional"] * Decimal(10000)
        else:
            actual_bps = Decimal(0)
        pred_now_avg = cell["pred_now_sum"] / cell["n_orders"] if cell["n_orders"] > 0 else Decimal(0)
        pred_pre_avg = cell["pred_pre_sum"] / cell["n_orders"] if cell["n_orders"] > 0 else Decimal(0)
        diff_now = actual_bps - pred_now_avg
        diff_pre = actual_bps - pred_pre_avg
        print(f"{style:<10s} {ot:<8s} {n:<6d} {nf:<7d} "
              f"{cell['notional']:<14.2f} {actual_bps:<11.3f} {pred_now_avg:<10.3f} "
              f"{pred_pre_avg:<12.3f} {diff_now:<+10.3f} {diff_pre:<+10.3f}")

    # --- Slippage ---
    # Slippage = (fill_price - reference_price) * sign(side) / reference_price * 10000
    # Reference price = limit_price if limit, else pre-trade mid (not stored).
    # Without stored ref price, we can only report overall fee bps.
    print("\nNote: slippage requires reference/mid price at submit, which isn't in this data.")


if __name__ == "__main__":
    main()
