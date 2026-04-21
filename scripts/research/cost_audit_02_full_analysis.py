"""Cost audit step 2: full analysis of all execution_orders + fills.

Since the total corpus is small (~28 orders pre-fix), we analyze the ENTIRE
population rather than restrict to post-fix window.

Read-only. No mutations.
"""
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
    for key in ("AATS_DATABASE_URL", "DATABASE_URL", "POSTGRES_DSN"):
        val = os.environ.get(key)
        if val:
            return val
    user = os.environ.get("POSTGRES_USER", "admin")
    pw = os.environ.get("POSTGRES_PASSWORD")
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("AATS_DB_NAME") or "aats_live_derivatives"
    if not pw:
        raise RuntimeError("no postgres password in env")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def main() -> None:
    dsn = get_dsn()
    with psycopg2.connect(dsn) as conn:
        conn.autocommit = False
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Fetch all 28 orders with their submission_payload + fills
            cur.execute(
                """
                SELECT
                    o.order_id,
                    o.symbol,
                    o.side,
                    o.order_type                                              AS order_type_col,
                    o.time_in_force,
                    o.requested_qty,
                    o.state                                                    AS order_state,
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
                    o.raw_payload::jsonb ->> 'execution_style'                 AS style_top,
                    o.raw_payload::jsonb -> 'submission_payload' ->> 'ordType' AS ord_type_payload,
                    o.raw_payload::jsonb -> 'submission_payload' ->> 'tdMode'  AS td_mode_payload,
                    o.raw_payload::jsonb -> 'submission_payload' ->> 'reduceOnly' AS reduce_only_payload,
                    o.raw_payload::jsonb -> 'submission_payload' ->> 'px'      AS px_payload,
                    o.raw_payload::jsonb -> 'fees'                              AS fees_json,
                    o.raw_payload::jsonb ->> 'average_fill_price'               AS avg_fill_price,
                    o.raw_payload::jsonb ->> 'filled_qty'                       AS filled_qty,
                    o.raw_payload::jsonb ->> 'exchange_status'                  AS exchange_status,
                    o.raw_payload::jsonb ->> 'cancel_reason'                    AS cancel_reason,
                    o.raw_payload::jsonb ->> 'leg_intent_id'                    AS leg_intent_id
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
                    f.exchange_ts,
                    f.raw_payload::jsonb ->> 'execType'    AS exec_type,
                    f.raw_payload::jsonb ->> 'feeRate'     AS fee_rate_raw,
                    f.raw_payload::jsonb ->> 'fee'         AS fee_raw,
                    f.raw_payload::jsonb ->> 'fillFee'     AS fill_fee_raw,
                    f.raw_payload::jsonb ->> 'tradeId'     AS trade_id,
                    f.raw_payload::jsonb ->> 'ordId'       AS ord_id_raw
                FROM public.execution_fills f
                ORDER BY f.exchange_ts ASC
                LIMIT 2000
                """
            )
            fills = cur.fetchall()

    print("=" * 60)
    print(f"Total orders: {len(orders)}")
    print(f"Total fills:  {len(fills)}")
    print("=" * 60)

    # --- 1. Distribution by order_type (internal col) ---
    by_order_type: dict[str, int] = {}
    for o in orders:
        by_order_type[o["order_type_col"]] = by_order_type.get(o["order_type_col"], 0) + 1
    print("\n[A] order_type (internal column) counts:")
    for k, v in sorted(by_order_type.items(), key=lambda x: -x[1]):
        print(f"  {k!r:20s}: {v}")

    # --- 2. Distribution by OKX ordType (from submission_payload) ---
    by_ord_type_payload: dict[str, int] = {}
    for o in orders:
        k = o["ord_type_payload"]
        by_ord_type_payload[str(k)] = by_ord_type_payload.get(str(k), 0) + 1
    print("\n[B] OKX ordType (from submission_payload) counts:")
    for k, v in sorted(by_ord_type_payload.items(), key=lambda x: -x[1]):
        print(f"  {k!r:20s}: {v}")

    # --- 3. Distribution by execution_style ---
    by_style: dict[str, int] = {}
    for o in orders:
        k = o["style_top"]
        by_style[str(k)] = by_style.get(str(k), 0) + 1
    print("\n[C] execution_style (raw_payload top-level) counts:")
    for k, v in sorted(by_style.items(), key=lambda x: -x[1]):
        print(f"  {k!r:20s}: {v}")

    # --- 4. Distribution by strategy_family + leg_role ---
    by_sf: dict[str, int] = {}
    for o in orders:
        k = f"{o['strategy_family']} / {o['strategy_leg_role']}"
        by_sf[k] = by_sf.get(k, 0) + 1
    print("\n[D] strategy_family / leg_role counts:")
    for k, v in sorted(by_sf.items(), key=lambda x: -x[1]):
        print(f"  {k:40s}: {v}")

    # --- 5. Order state distribution ---
    by_state: dict[str, int] = {}
    for o in orders:
        k = o["order_state"]
        by_state[k] = by_state.get(k, 0) + 1
    print("\n[E] order state counts:")
    for k, v in sorted(by_state.items(), key=lambda x: -x[1]):
        print(f"  {k!r:20s}: {v}")

    # --- 6. Timeline ---
    if orders:
        print(f"\n[F] Time window: {orders[0]['created_at']} → {orders[-1]['created_at']}")

    # --- 7. Fills: fee bps per fill ---
    fills_fee_bps: list[float] = []
    per_fill = []
    for f in fills:
        try:
            fill_qty = Decimal(str(f["fill_qty"]))
            fill_price = Decimal(str(f["fill_price"]))
            fee_amount = Decimal(str(f["fee_amount"]))
            notional = fill_qty * fill_price
            if notional > 0:
                fee_bps = abs(fee_amount) / notional * Decimal(10000)
                fills_fee_bps.append(float(fee_bps))
                per_fill.append((f, float(fee_bps), float(notional)))
        except Exception as e:  # noqa: BLE001
            print(f"  ERR computing fee bps for fill {f['fill_id']}: {e}")

    print("\n[G] fills fee_bps distribution:")
    if fills_fee_bps:
        print(f"  count: {len(fills_fee_bps)}")
        print(f"  min:   {min(fills_fee_bps):.3f}")
        print(f"  p25:   {percentile(fills_fee_bps, 0.25):.3f}")
        print(f"  p50:   {percentile(fills_fee_bps, 0.50):.3f}")
        print(f"  mean:  {statistics.mean(fills_fee_bps):.3f}")
        print(f"  p75:   {percentile(fills_fee_bps, 0.75):.3f}")
        print(f"  p95:   {percentile(fills_fee_bps, 0.95):.3f}")
        print(f"  max:   {max(fills_fee_bps):.3f}")
        print(f"  stdev: {statistics.stdev(fills_fee_bps) if len(fills_fee_bps) >= 2 else 0:.3f}")

    # --- 8. liquidity_role distribution ---
    by_liq: dict[str, int] = {}
    for f in fills:
        k = str(f["liquidity_role"])
        by_liq[k] = by_liq.get(k, 0) + 1
    print("\n[H] fills liquidity_role counts:")
    for k, v in sorted(by_liq.items(), key=lambda x: -x[1]):
        print(f"  {k!r:10s}: {v}")

    # --- 9. exec_type from OKX (raw_payload.execType) ---
    by_exec_type: dict[str, int] = {}
    for f in fills:
        k = str(f["exec_type"])
        by_exec_type[k] = by_exec_type.get(k, 0) + 1
    print("\n[I] fills OKX execType counts:")
    for k, v in sorted(by_exec_type.items(), key=lambda x: -x[1]):
        print(f"  {k!r:10s}: {v}")

    # --- 10. Cross: join orders x fills, tabulate (ordType, liq_role) -> fee_bps ---
    order_map = {o["order_id"]: o for o in orders}
    cell_stats: dict[tuple[str, str, str], list[float]] = {}
    for f, bps, _nt in per_fill:
        o = order_map.get(f["order_id"])
        if not o:
            continue
        style = str(o["style_top"])
        ord_type = str(o["ord_type_payload"])
        liq = str(f["liquidity_role"])
        key = (style, ord_type, liq)
        cell_stats.setdefault(key, []).append(bps)

    print("\n[J] cross: (execution_style, OKX ordType, liq_role) -> fee_bps")
    print(f"  {'style':<22s} {'ordType':<8s} {'liq':<6s} {'n':<4s} {'mean':<8s} {'p50':<8s} {'p95':<8s}")
    for (style, ot, liq), vals in sorted(cell_stats.items()):
        mean_v = statistics.mean(vals)
        p50 = percentile(vals, 0.5)
        p95 = percentile(vals, 0.95)
        print(f"  {style:<22s} {ot:<8s} {liq:<6s} {len(vals):<4d} {mean_v:<8.3f} {p50:<8.3f} {p95:<8.3f}")

    # --- 11. fee_rate from OKX raw ---
    print("\n[K] fills raw feeRate values (first 10):")
    for f in fills[:10]:
        print(f"  fill {f['fill_id'][:16]}... feeRate={f['fee_rate_raw']!r} fee={f['fee_raw']!r}")


if __name__ == "__main__":
    main()
