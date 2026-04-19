"""Cost audit step 1: probe execution_orders schema + data window.

Read-only. No INSERT/UPDATE/DELETE. Safe to run against aats_live_derivatives.
"""
from __future__ import annotations

import os
import json
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# Load DB credentials from .env.derivatives.live WITHOUT printing contents.
# python-dotenv reads in-process only.
# Resolve repo root: respect AATS_ROOT env override; otherwise walk up from __file__.
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
# Load .env.wsl2 first (Postgres creds), then .env.derivatives.live (db name).
# Values flow only in-process; we never print them.
load_dotenv(ROOT / ".env.wsl2", override=False)
load_dotenv(ROOT / ".env.derivatives.live", override=False)

DB_KEYS = [
    "AATS_DATABASE_URL",
    "AATS_DERIVATIVES_DATABASE_URL",
    "AATS_DATABASE_DSN",
    "DATABASE_URL",
    "POSTGRES_DSN",
]


def get_dsn() -> str:
    # Prefer a pre-built DSN env var; fall back to constructed Postgres DSN.
    for key in DB_KEYS:
        val = os.environ.get(key)
        if val:
            # Do not print the DSN; return it directly.
            return val
    user = os.environ.get("POSTGRES_USER", "admin")
    pw = os.environ.get("POSTGRES_PASSWORD")
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    # Prefer AATS_DB_NAME (from .env.derivatives.live) over POSTGRES_DB
    # because POSTGRES_DB is the infra default (aats), and we want aats_live_derivatives.
    db = (
        os.environ.get("AATS_DB_NAME")
        or os.environ.get("AATS_POSTGRES_DB")
        or "aats_live_derivatives"
    )
    if not pw:
        raise RuntimeError("no postgres password in env; cannot connect")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def main() -> None:
    dsn = get_dsn()
    # sanitize for print: remove password segment
    try:
        from urllib.parse import urlparse
        u = urlparse(dsn)
        sanitized = f"{u.scheme}://{u.username}:***@{u.hostname}:{u.port}{u.path}"
    except Exception:  # noqa: BLE001
        sanitized = "<unparseable>"
    print(f"[probe] connecting via: {sanitized}")

    with psycopg2.connect(dsn) as conn:
        conn.autocommit = False  # we only read
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 1) total row counts per table
            cur.execute(
                """
                SELECT table_name, (xpath('/row/c/text()', query_to_xml(
                    format('SELECT COUNT(*) AS c FROM %I', table_name),
                    true, true, '')))[1]::text::bigint AS n_rows
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN (
                    'execution_orders', 'execution_fills',
                    'execution_order_state_history', 'order_states'
                  )
                ORDER BY table_name
                """
            )
            print("\n[probe] row counts:")
            for row in cur.fetchall():
                print(f"  {row['table_name']}: {row['n_rows']}")

            # 2) execution_orders columns
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name='execution_orders'
                ORDER BY ordinal_position
                """
            )
            print("\n[probe] execution_orders columns:")
            for row in cur.fetchall():
                print(f"  {row['column_name']}: {row['data_type']} (nullable={row['is_nullable']})")

            # 3) execution_fills columns
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name='execution_fills'
                ORDER BY ordinal_position
                """
            )
            print("\n[probe] execution_fills columns:")
            for row in cur.fetchall():
                print(f"  {row['column_name']}: {row['data_type']} (nullable={row['is_nullable']})")

            # 4) data window: recent orders since H2 fix commit time
            # H2 commit 7f55176 2026-04-19 12:22:09 -0400 -> UTC 16:22:09
            cur.execute(
                """
                SELECT MIN(created_at) AS first_ts,
                       MAX(created_at) AS last_ts,
                       COUNT(*) AS n_total,
                       COUNT(*) FILTER (WHERE created_at >= '2026-04-19 16:22:09+00') AS n_post_fix,
                       COUNT(*) FILTER (
                         WHERE created_at >= '2026-04-19 00:00:00+00'
                           AND created_at <  '2026-04-19 16:22:09+00'
                       ) AS n_pre_fix_day,
                       COUNT(*) FILTER (
                         WHERE created_at >= '2026-04-12 00:00:00+00'
                           AND created_at <  '2026-04-19 16:22:09+00'
                       ) AS n_week_pre_fix
                FROM public.execution_orders
                """
            )
            window = cur.fetchone()
            print("\n[probe] execution_orders time window:")
            print(f"  first_ts: {window['first_ts']}")
            print(f"  last_ts:  {window['last_ts']}")
            print(f"  total:    {window['n_total']}")
            print(f"  post-fix (>= 2026-04-19 16:22 UTC): {window['n_post_fix']}")
            print(f"  pre-fix same day 2026-04-19: {window['n_pre_fix_day']}")
            print(f"  week before fix: {window['n_week_pre_fix']}")

            # 5) sample raw_payload structure from 3 orders
            cur.execute(
                """
                SELECT order_id, order_type, state, created_at,
                       jsonb_object_keys(raw_payload::jsonb) AS k
                FROM public.execution_orders
                ORDER BY created_at DESC
                LIMIT 50
                """
            )
            rows = cur.fetchall()
            if rows:
                keysets: dict[str, int] = {}
                order_ids_seen: set[str] = set()
                for r in rows:
                    if r["order_id"] in order_ids_seen:
                        pass
                    order_ids_seen.add(r["order_id"])
                    k = r["k"]
                    keysets[k] = keysets.get(k, 0) + 1
                print("\n[probe] top-level raw_payload keys (top 50 recent orders):")
                for k, cnt in sorted(keysets.items(), key=lambda x: -x[1]):
                    print(f"  {k}: {cnt}")

            # 6) probe whether raw_payload has submission_payload.ordType
            cur.execute(
                """
                SELECT order_id, created_at,
                       raw_payload::jsonb -> 'submission_payload' ->> 'ordType' AS ord_type_in_payload,
                       raw_payload::jsonb ->> 'execution_style'                     AS style_top,
                       raw_payload::jsonb -> 'submission_payload' ->> 'tdMode' AS td_mode,
                       order_type AS order_type_col
                FROM public.execution_orders
                WHERE created_at >= NOW() - INTERVAL '30 days'
                ORDER BY created_at DESC
                LIMIT 10
                """
            )
            print("\n[probe] sample raw_payload ordType + style:")
            for r in cur.fetchall():
                print(
                    f"  {r['order_id'][:16]}... {r['created_at']} "
                    f"style_top={r['style_top']!r} ordType_payload={r['ord_type_in_payload']!r} "
                    f"order_type_col={r['order_type_col']!r}"
                )


if __name__ == "__main__":
    main()
