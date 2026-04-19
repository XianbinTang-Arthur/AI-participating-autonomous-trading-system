"""Step 6: slippage analysis — compare referencePrice (in submission_payload)
vs avg_fill_price."""
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
    with psycopg2.connect(get_dsn()) as conn:
        conn.autocommit = False
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    o.order_id,
                    o.symbol,
                    o.side,
                    o.state,
                    o.raw_payload::jsonb -> 'submission_payload' ->> 'ordType' AS ord_type,
                    o.raw_payload::jsonb -> 'submission_payload' ->> 'referencePrice' AS ref_px,
                    o.raw_payload::jsonb -> 'submission_payload' ->> 'maxSlippageToleranceBps' AS max_slip_bps,
                    o.raw_payload::jsonb ->> 'average_fill_price' AS avg_fill_px,
                    o.raw_payload::jsonb ->> 'filled_qty' AS filled_qty
                FROM public.execution_orders o
                ORDER BY o.created_at ASC
                LIMIT 1000
                """
            )
            rows = cur.fetchall()

    side_sign = {"buy": Decimal("1"), "sell": Decimal("-1")}
    slippages_bps: list[float] = []
    per_order = []
    for r in rows:
        ref = r["ref_px"]
        avg = r["avg_fill_px"]
        side = r["side"]
        if r["state"] != "FILLED" or ref in (None, "") or avg in (None, "") or side not in side_sign:
            continue
        try:
            ref_d = Decimal(str(ref))
            avg_d = Decimal(str(avg))
            if ref_d <= 0:
                continue
            # Slippage cost (positive = unfavorable): for buy, paid higher than ref; for sell, sold lower than ref.
            sign = side_sign[side]
            slip_bps = (avg_d - ref_d) / ref_d * Decimal(10000) * sign
            slippages_bps.append(float(slip_bps))
            per_order.append((r["order_id"], r["symbol"], side, r["ord_type"], float(ref_d), float(avg_d), float(slip_bps), r["max_slip_bps"]))
        except Exception as e:  # noqa: BLE001
            print(f"ERR on {r['order_id']}: {e}")

    print(f"Total orders with ref+avg: {len(slippages_bps)} of {len(rows)} orders")
    print()
    print(f"{'order_id':<22s} {'sym':<14s} {'side':<5s} {'ordType':<7s} {'ref_px':<10s} "
          f"{'avg_px':<10s} {'slip_bps':<10s} {'max_slip':<8s}")
    for oid, sym, side, ot, ref_p, avg_p, slip, max_slip in per_order:
        print(f"{oid[:22]:<22s} {sym:<14s} {side:<5s} {str(ot):<7s} "
              f"{ref_p:<10.2f} {avg_p:<10.2f} {slip:<+10.3f} {str(max_slip):<8s}")

    if slippages_bps:
        print(f"\nSlippage bps distribution (cost sign; +ve = unfavorable):")
        print(f"  count: {len(slippages_bps)}")
        print(f"  min:   {min(slippages_bps):+.3f}")
        print(f"  p05:   {percentile(slippages_bps, 0.05):+.3f}")
        print(f"  p25:   {percentile(slippages_bps, 0.25):+.3f}")
        print(f"  p50:   {percentile(slippages_bps, 0.50):+.3f}")
        print(f"  mean:  {statistics.mean(slippages_bps):+.3f}")
        print(f"  p75:   {percentile(slippages_bps, 0.75):+.3f}")
        print(f"  p95:   {percentile(slippages_bps, 0.95):+.3f}")
        print(f"  max:   {max(slippages_bps):+.3f}")
        print(f"  stdev: {statistics.stdev(slippages_bps) if len(slippages_bps) >= 2 else 0:.3f}")

    # Total cost (fee + slippage) per order — rough, assume taker 5 bps for filled
    total_cost_bps = [5.0 + s for s in slippages_bps]
    if total_cost_bps:
        print(f"\nTotal entry cost (fee 5.0 bps + slippage) bps distribution:")
        print(f"  mean:  {statistics.mean(total_cost_bps):.3f}")
        print(f"  p50:   {percentile(total_cost_bps, 0.50):.3f}")
        print(f"  p95:   {percentile(total_cost_bps, 0.95):.3f}")
        print(f"  max:   {max(total_cost_bps):.3f}")


if __name__ == "__main__":
    main()
