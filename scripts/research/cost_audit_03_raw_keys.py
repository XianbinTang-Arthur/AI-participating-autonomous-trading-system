"""Step 3: inspect exact raw_payload structure on fills and orders (keys + shapes only).

Read-only. Does NOT print full raw_payload contents. Only keys and a sanitized subset
of non-sensitive scalar values.
"""
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


# whitelist of scalar keys safe to print (no API keys, no passwords).
SAFE_SCALAR_KEYS = {
    # order-level
    "ordType", "tdMode", "side", "posSide", "sz", "px", "reduceOnly",
    "schema_version", "venue", "status", "exchange_status", "execution_error",
    "submission_mode", "cancel_reason",
    # fill-level from OKX
    "instType", "instId", "billId", "category", "execType", "feeRate",
    "fee", "fillFee", "fillPx", "fillSz", "fillIdxPx", "fillMarkPx",
    "fillPnl", "tradeId", "ts", "liquidity", "rebate", "rebateCcy",
    "subType", "ccy", "fillFeeCcy",
}


def main() -> None:
    with psycopg2.connect(get_dsn()) as conn:
        conn.autocommit = False
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # fetch 3 orders + 3 fills with full raw_payload
            cur.execute(
                "SELECT order_id, raw_payload FROM public.execution_orders "
                "ORDER BY created_at DESC LIMIT 3"
            )
            orders_sample = cur.fetchall()

            cur.execute(
                "SELECT fill_id, order_id, raw_payload, fee_amount, fee_currency, liquidity_role "
                "FROM public.execution_fills ORDER BY exchange_ts DESC LIMIT 3"
            )
            fills_sample = cur.fetchall()

    print("=== Order raw_payload keys (3 most recent) ===")
    for o in orders_sample:
        rp = o["raw_payload"] or {}
        top_keys = sorted(rp.keys()) if isinstance(rp, dict) else []
        sub = rp.get("submission_payload") if isinstance(rp, dict) else None
        sub_keys = sorted(sub.keys()) if isinstance(sub, dict) else []
        fees = rp.get("fees") if isinstance(rp, dict) else None
        print(f"\norder_id={o['order_id'][:16]}...")
        print(f"  top-level keys ({len(top_keys)}): {top_keys}")
        print(f"  submission_payload keys ({len(sub_keys)}): {sub_keys}")
        print(f"  fees type: {type(fees).__name__}, value summary:")
        if isinstance(fees, list):
            print(f"    list len={len(fees)}")
            if fees:
                fkeys = sorted(fees[0].keys()) if isinstance(fees[0], dict) else []
                print(f"    item 0 keys: {fkeys}")
        elif isinstance(fees, dict):
            print(f"    dict keys: {sorted(fees.keys())}")
        else:
            print(f"    scalar: {fees!r}")
        # safe scalar values only
        if isinstance(sub, dict):
            print(f"  sub.ordType={sub.get('ordType')!r}")
            print(f"  sub.tdMode={sub.get('tdMode')!r}")
            print(f"  sub.reduceOnly={sub.get('reduceOnly')!r}")
        print(f"  rp.exchange_status={rp.get('exchange_status')!r}")
        print(f"  rp.status={rp.get('status')!r}")
        print(f"  rp.average_fill_price={rp.get('average_fill_price')!r}")
        print(f"  rp.filled_qty={rp.get('filled_qty')!r}")
        print(f"  rp.cancel_reason={rp.get('cancel_reason')!r}")
        print(f"  rp.execution_error={rp.get('execution_error')!r}")

    print("\n\n=== Fill raw_payload keys (3 most recent) ===")
    for f in fills_sample:
        rp = f["raw_payload"] or {}
        top_keys = sorted(rp.keys()) if isinstance(rp, dict) else []
        print(f"\nfill_id={f['fill_id'][:16]}... order_id={f['order_id'][:16]}...")
        print(f"  top-level keys ({len(top_keys)}): {top_keys}")
        # print only whitelisted scalars
        if isinstance(rp, dict):
            for k in sorted(rp.keys()):
                if k in SAFE_SCALAR_KEYS:
                    v = rp[k]
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        print(f"    {k}={v!r}")
                    elif isinstance(v, dict):
                        print(f"    {k} <dict keys={sorted(v.keys())}>")
                    else:
                        print(f"    {k} <{type(v).__name__} len={len(v) if hasattr(v,'__len__') else '?'}>")
        print(f"  derived: fee_amount={f['fee_amount']} ccy={f['fee_currency']} liq={f['liquidity_role']}")


if __name__ == "__main__":
    main()
