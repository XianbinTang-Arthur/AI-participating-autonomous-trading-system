#!/bin/bash
# scripts/diag/event_store_bloat_audit.sh
#
# 按 event_type 聚合 event_store，找出"每行很大 + 高频 publish"的 bloat
# 贡献者。专为 2026-04-21 夜场审计 `recovery` 信号 709 KB × 13s 问题设计。
#
# 2026-04-21 首次发现：
#   - event_store 4 天增长到 6.6 GB（1.65 GB/天）
#   - 单一 event_type 'GuardSignalUpdate' + 'recovery' 信号占 50%
#   - 其中 `independent_recovery_snapshots` 字段 709 KB × 每 13s 一次
#   - 连续 1 小时中 97% 的 payload 完全相同（dedup 潜力 33×）
#
# 用法: bash scripts/diag/event_store_bloat_audit.sh
set -e

DB="${AATS_DIAG_DB:-aats_live_derivatives}"

echo "=== event_store 总量 & 时间窗 ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT
  COUNT(*) AS rows,
  pg_size_pretty(pg_total_relation_size('event_store')) AS total_size,
  MIN(event_timestamp) AS oldest,
  MAX(event_timestamp) AS newest,
  ROUND(EXTRACT(EPOCH FROM (MAX(event_timestamp) - MIN(event_timestamp))) / 86400, 2) AS days_span
FROM event_store;
"

echo ""
echo "=== Top 10 event_type by total payload size ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT
  event_type,
  COUNT(*) AS rows,
  pg_size_pretty(SUM(pg_column_size(payload))) AS total_payload,
  pg_size_pretty((SUM(pg_column_size(payload))/COUNT(*))::bigint) AS avg_size,
  ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM event_store), 1) AS pct_rows
FROM event_store
GROUP BY event_type
ORDER BY SUM(pg_column_size(payload)) DESC
LIMIT 10;
"

echo ""
echo "=== GuardSignalUpdate 按 signal_name 拆分 (最近 1h) ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT
  payload->>'_signal_name' AS signal_name,
  COUNT(*) AS events_last_hour,
  pg_size_pretty(AVG(pg_column_size(payload))::bigint) AS avg_payload_size,
  pg_size_pretty(SUM(pg_column_size(payload))) AS total_last_hour,
  ROUND(EXTRACT(EPOCH FROM (MAX(event_timestamp) - MIN(event_timestamp)))::numeric / NULLIF(COUNT(*)-1, 0), 2) AS avg_interval_s
FROM event_store
WHERE event_type='GuardSignalUpdate'
  AND event_timestamp > NOW() - INTERVAL '1 hour'
GROUP BY 1
ORDER BY SUM(pg_column_size(payload)) DESC;
"

echo ""
echo "=== 'recovery' 信号 payload 字段 top size (最新 1 个 event) ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT
  k AS field,
  pg_column_size(payload -> k) AS bytes
FROM event_store,
     LATERAL json_object_keys(payload) k
WHERE event_type='GuardSignalUpdate'
  AND payload->>'_signal_name' = 'recovery'
  AND event_timestamp = (
    SELECT MAX(event_timestamp) FROM event_store
    WHERE event_type='GuardSignalUpdate' AND payload->>'_signal_name' = 'recovery'
  )
ORDER BY bytes DESC
LIMIT 8;
"

echo ""
echo "=== 'recovery' 信号 dedup 潜力 (最近 1h) ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
WITH hashes AS (
  SELECT md5((payload -> 'independent_recovery_snapshots')::text) AS h, event_timestamp
  FROM event_store
  WHERE event_type='GuardSignalUpdate'
    AND payload->>'_signal_name' = 'recovery'
    AND event_timestamp > NOW() - INTERVAL '1 hour'
)
SELECT
  COUNT(*) AS total_events,
  COUNT(DISTINCT h) AS unique_snapshot_hashes,
  ROUND(100.0 * (1 - COUNT(DISTINCT h)::numeric / NULLIF(COUNT(*), 0)), 2) AS dedup_savings_pct
FROM hashes;
"
