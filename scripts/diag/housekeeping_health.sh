#!/bin/bash
# scripts/diag/housekeeping_health.sh
#
# 检查 event_store / outbox / archive housekeeping 是否运行正常。
# 配置见 aats/storage/housekeeping.py::run_housekeeping_cycle：
#   - hot_event_retention_days = 14
#   - archive_older_than_days = 90
#   - outbox: 发布成功后 N 天清理
#
# 红旗信号：
#   - event_store hot 里有 >14 天旧数据（归档失败）
#   - event_store_archive 里有 >90 天旧数据（purge 失败）
#   - outbox 里有大量 published=true 但未清理
#
# 用法: bash scripts/diag/housekeeping_health.sh
set -e

DB="${AATS_DIAG_DB:-aats_live_derivatives}"

echo "=== event_store 年龄分布 ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT
  CASE
    WHEN event_timestamp > NOW() - INTERVAL '1 day' THEN '0-1d (new)'
    WHEN event_timestamp > NOW() - INTERVAL '7 days' THEN '1-7d'
    WHEN event_timestamp > NOW() - INTERVAL '14 days' THEN '7-14d (在 retention 内)'
    WHEN event_timestamp > NOW() - INTERVAL '30 days' THEN '14-30d (应已归档)'
    WHEN event_timestamp > NOW() - INTERVAL '90 days' THEN '30-90d (应已归档)'
    ELSE '>90d (归档失败 → 红旗)'
  END AS age_bucket,
  COUNT(*) AS rows,
  pg_size_pretty(SUM(pg_column_size(payload))) AS size
FROM event_store
GROUP BY 1
ORDER BY MIN(event_timestamp);
"

echo ""
echo "=== event_store_archive 年龄分布（purge 应清 >90d） ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT
  CASE
    WHEN event_timestamp > NOW() - INTERVAL '30 days' THEN '0-30d (新归档)'
    WHEN event_timestamp > NOW() - INTERVAL '90 days' THEN '30-90d (在 archive retention 内)'
    ELSE '>90d (purge 失败 → 红旗)'
  END AS age_bucket,
  COUNT(*) AS rows
FROM event_store_archive
GROUP BY 1
ORDER BY MIN(event_timestamp);
"

echo ""
echo "=== outbox_events 状态分布（PUBLISHED 应被清理） ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT
  status,
  COUNT(*) AS rows,
  MIN(created_at) AS oldest_created_at,
  MIN(published_at) AS oldest_published_at
FROM outbox_events
GROUP BY 1
ORDER BY 2 DESC;
"

echo ""
echo "=== housekeeping loop 最近运行事件（从 event_store 反查）==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT event_type, MAX(event_timestamp) AS last_seen
FROM event_store
WHERE event_type LIKE '%ousekeeping%'
   OR event_type LIKE '%rchive%'
   OR event_type LIKE '%urge%'
GROUP BY 1
ORDER BY 2 DESC
LIMIT 10;
"

echo ""
echo "=== reconciliation_reports 年龄（应只保留决策需要的最近窗口）==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE as_of_ts > NOW() - INTERVAL '1 hour') AS last_hour,
  COUNT(*) FILTER (WHERE as_of_ts > NOW() - INTERVAL '1 day') AS last_day,
  COUNT(*) FILTER (WHERE as_of_ts > NOW() - INTERVAL '7 days') AS last_week,
  MIN(as_of_ts)::date AS oldest_date
FROM reconciliation_reports;
"

echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "如何判断是否健康："
echo "  - event_store hot >14d 桶 应为 0（housekeeping 归档）"
echo "  - event_store_archive >90d 桶 应为 0（purge 正常）"
echo "  - outbox_events PUBLISHED 最老 published_at 应 <= 7d"
echo "  - reconciliation_reports 增速稳定，不应爆炸式增长"
echo "════════════════════════════════════════════════════════════════════"
