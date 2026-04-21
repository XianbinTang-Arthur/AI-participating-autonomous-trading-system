#!/bin/bash
# scripts/diag/table_growth_audit.sh
#
# 采样生产 Postgres 所有大表，按 size 排序 + 估算日增速（基于最近时间戳
# 字段）。用于早期发现无界增长 / 未归档表。
#
# 2026-04-21 首次使用：发现 event_store 增速 1.65 GB/day、portfolio_allocation_decisions
# 197 MB 异常大。
#
# 用法: bash scripts/diag/table_growth_audit.sh [top_n]
#   top_n: 显示前 N 大表（默认 20）
set -e

DB="${AATS_DIAG_DB:-aats_live_derivatives}"
TOP_N="${1:-20}"

echo "=== Top $TOP_N 大表（按总 size） ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT
  tablename,
  pg_size_pretty(pg_total_relation_size('public.'||tablename)) AS total_size,
  pg_total_relation_size('public.'||tablename) AS size_bytes
FROM pg_tables
WHERE schemaname='public'
ORDER BY pg_total_relation_size('public.'||tablename) DESC
LIMIT $TOP_N;
"

echo ""
echo "=== 核心无界增长表的 row count ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT tbl, rows
FROM (
  SELECT 'event_store' AS tbl, COUNT(*) AS rows FROM event_store
  UNION ALL SELECT 'event_store_archive', COUNT(*) FROM event_store_archive
  UNION ALL SELECT 'reconciliation_reports', COUNT(*) FROM reconciliation_reports
  UNION ALL SELECT 'reconciliation_findings', COUNT(*) FROM reconciliation_findings
  UNION ALL SELECT 'portfolio_allocation_decisions', COUNT(*) FROM portfolio_allocation_decisions
  UNION ALL SELECT 'exit_execution_intents', COUNT(*) FROM exit_execution_intents
  UNION ALL SELECT 'exit_execution_child_refs', COUNT(*) FROM exit_execution_child_refs
  UNION ALL SELECT 'execution_fills', COUNT(*) FROM execution_fills
  UNION ALL SELECT 'fill_events', COUNT(*) FROM fill_events
  UNION ALL SELECT 'order_states', COUNT(*) FROM order_states
  UNION ALL SELECT 'execution_orders', COUNT(*) FROM execution_orders
  UNION ALL SELECT 'outbox_events', COUNT(*) FROM outbox_events
  UNION ALL SELECT 'order_obligations', COUNT(*) FROM order_obligations
) AS counts
ORDER BY rows DESC;
"

echo ""
echo "=== event_store 增速估算（最近 1h 行数 / 1h） ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT
  COUNT(*) AS rows_last_hour,
  pg_size_pretty(SUM(pg_column_size(payload))) AS payload_last_hour,
  ROUND((COUNT(*)::numeric * 24), 0) AS projected_daily_rows,
  pg_size_pretty((SUM(pg_column_size(payload))::bigint * 24)) AS projected_daily_payload
FROM event_store
WHERE event_timestamp > NOW() - INTERVAL '1 hour';
"

echo ""
echo "=== Housekeeping / 归档状态 ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT
  'event_store hot' AS layer,
  MIN(event_timestamp)::date AS oldest_date,
  MAX(event_timestamp)::date AS newest_date,
  (MAX(event_timestamp)::date - MIN(event_timestamp)::date) AS day_span
FROM event_store
UNION ALL SELECT
  'event_store_archive',
  MIN(event_timestamp)::date,
  MAX(event_timestamp)::date,
  (MAX(event_timestamp)::date - MIN(event_timestamp)::date)
FROM event_store_archive;
"

echo ""
echo "注意事项："
echo "- event_store hot_event_retention_days=14 —— 超过 14 天的应该在 archive"
echo "- event_store_archive 如果 rows=0 但 hot 有 >14 天数据 —— housekeeping 异常"
echo "- 若看到 >100 MB 的表不在表内、未归档 —— 考虑加 housekeeping policy"
