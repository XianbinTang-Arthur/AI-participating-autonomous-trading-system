#!/bin/bash
# scripts/diag/pg_full_scan_audit.sh
# 扫当前是否有无 WHERE + 大表 + JSONB 的 SELECT active，以及 idle-in-tx 等待。
# 用法: bash scripts/diag/pg_full_scan_audit.sh
set -e

DB="${AATS_DIAG_DB:-aats_live_derivatives}"

# 定义"大表"：任何有 payload (JSONB) 列的 reconciliation_reports /
# portfolio_snapshots / event_store 等
BIG_TABLES="reconciliation_reports|portfolio_snapshots|event_envelopes|fill_events|order_states"

echo "=== 当前 active 的无 WHERE 大表 SELECT ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT pid,
       state,
       EXTRACT(EPOCH FROM NOW()-query_start)::int AS age_s,
       LEFT(query, 200) AS q
FROM pg_stat_activity
WHERE datname='$DB'
  AND state='active'
  AND query ~ 'FROM ($BIG_TABLES)'
  AND query NOT LIKE '%WHERE%'
  AND query NOT LIKE '%pg_stat_activity%'
ORDER BY age_s DESC
LIMIT 10;
"

echo "=== 历史统计：pg_stat_statements 是否开启 ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT name, setting
FROM pg_settings
WHERE name='shared_preload_libraries';
"

echo ""
echo "若 pg_stat_statements 未 enable，可改 postgresql.conf:"
echo "   shared_preload_libraries = 'pg_stat_statements'"
echo "然后重启 Postgres，查 SELECT * FROM pg_stat_statements ORDER BY total_exec_time DESC;"
