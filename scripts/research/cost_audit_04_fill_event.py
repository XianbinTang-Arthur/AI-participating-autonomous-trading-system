"""Step 4: look into execution_fills.raw_payload.fill_event for OKX fee raw."""
from __future__ import annotations

import os
import json
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

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


# Only these OKX-native scalar keys are safe to print — none should contain secrets.
SAFE_FILL_EVENT_KEYS = {
    # OKX fills endpoint keys (https://www.okx.com/docs-v5/ :: trade-fills)
    "instType", "instId", "tradeId", "ordId", "clOrdId", "billId",
    "subType", "tag", "fillPx", "fillSz", "fillIdxPx", "fillPnl",
    "fillMarkVol", "fillMarkPx", "fillFwdPx", "side", "execType",
    "feeRate", "fee", "rebate", "rebateCcy", "ts", "fillTime",
    "bizType", "feeCcy", "ccy", "fillTimeUtc",
    # AATS-internal additions
    "notional", "notional_usdt", "liquidity_role", "source_system",
}


def main() -> None:
    with psycopg2.connect(get_dsn()) as conn:
        conn.autocommit = False
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT fill_id, order_id, fee_amount, fee_currency, fill_qty, fill_price, "
                "liquidity_role, raw_payload "
                "FROM public.execution_fills ORDER BY exchange_ts ASC LIMIT 30"
            )
            fills = cur.fetchall()

    # Map order_id -> raw_payload.submission_payload.ordType for cross-ref
    order_ot: dict[str, str] = {}
    with psycopg2.connect(get_dsn()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT order_id, raw_payload::jsonb -> 'submission_payload' ->> 'ordType' AS ot "
                "FROM public.execution_orders LIMIT 200"
            )
            for r in cur.fetchall():
                order_ot[r["order_id"]] = r["ot"] or "?"

    print("=== execution_fills.raw_payload.fill_event key inventory ===\n")
    all_keys: dict[str, int] = {}
    for f in fills:
        rp = f["raw_payload"] or {}
        fe = rp.get("fill_event") or {}
        if not isinstance(fe, dict):
            continue
        for k in fe.keys():
            all_keys[k] = all_keys.get(k, 0) + 1
    for k, v in sorted(all_keys.items()):
        mark = " [safe]" if k in SAFE_FILL_EVENT_KEYS else " [UNKNOWN]"
        print(f"  {k:<25s} count={v}{mark}")

    print("\n=== Sample fill_event scalars (safe keys only) ===")
    for i, f in enumerate(fills[:10]):
        rp = f["raw_payload"] or {}
        fe = rp.get("fill_event") or {}
        print(f"\nfill #{i+1}  fill_id={f['fill_id'][:20]}... order={f['order_id'][:16]}...")
        print(f"  order.ordType={order_ot.get(f['order_id'])!r}")
        print(f"  derived: liq_role={f['liquidity_role']} fee_amount={f['fee_amount']} ccy={f['fee_currency']}")
        if isinstance(fe, dict):
            for k in sorted(fe.keys()):
                if k in SAFE_FILL_EVENT_KEYS:
                    v = fe[k]
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        print(f"    fe.{k}={v!r}")

    # Compute fee_bps = |fee|/notional * 10000 AND compare to OKX raw feeRate*10000
    print("\n=== fee_bps derived vs OKX raw feeRate ===")
    print(f"  {'fill_id':<22s} {'ordType':<8s} {'liq':<6s} {'derived_bps':<12s} {'raw_feeRate':<14s} {'raw_fee':<14s}")
    for f in fills:
        rp = f["raw_payload"] or {}
        fe = rp.get("fill_event") or {}
        try:
            notional = float(f["fill_qty"]) * float(f["fill_price"])
            derived_bps = abs(float(f["fee_amount"])) / notional * 10000 if notional > 0 else float("nan")
        except Exception:
            derived_bps = float("nan")
        raw_fee_rate = fe.get("feeRate") if isinstance(fe, dict) else None
        raw_fee = fe.get("fee") if isinstance(fe, dict) else None
        ot = order_ot.get(f["order_id"], "?")
        print(f"  {f['fill_id'][:22]:<22s} {str(ot):<8s} {str(f['liquidity_role']):<6s} "
              f"{derived_bps:<12.3f} {str(raw_fee_rate):<14s} {str(raw_fee):<14s}")


if __name__ == "__main__":
    main()
