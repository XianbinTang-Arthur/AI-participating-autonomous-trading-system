#!/bin/bash
# scripts/diag/pg_connection_health.sh
# 采样 Postgres 连接状态分布，外加 idle-in-transaction 的 query preview。
# 用法: bash scripts/diag/pg_connection_health.sh
set -e

DB="${AATS_DIAG_DB:-aats_live_derivatives}"

echo "=== Postgres connection state distribution ($DB) ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT state,
       COUNT(*) AS count,
       COALESCE(MAX(EXTRACT(EPOCH FROM NOW()-state_change))::int, 0) AS oldest_age_s
FROM pg_stat_activity
WHERE datname='$DB'
GROUP BY state
ORDER BY count DESC;
"

echo "=== Non-advisory idle-in-tx (top 5 oldest) ==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT pid,
       EXTRACT(EPOCH FROM NOW()-xact_start)::int AS xact_age_s,
       LEFT(query, 100) AS q
FROM pg_stat_activity
WHERE datname='$DB'
  AND state='idle in transaction'
  AND query NOT LIKE '%pg_try_advisory_lock%'
ORDER BY xact_start
LIMIT 5;
"

echo "=== Advisory-lock holders (expected = 4, one per process role) ==="
# 2026-04-21 fix：23c8e7e 之后 acquire_single_runtime_lock 会 commit tx，
# pg_stat_activity.query 不再是 'SELECT pg_try_advisory_lock(...)' 而是
# 'COMMIT'。必须查 pg_locks 才能看真正的持锁状态。
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT COUNT(*) AS advisory_lock_holders
FROM pg_locks
WHERE locktype='advisory' AND granted=true;
"

echo "=== Idle-in-tx 中持 advisory_lock 的连接（应为 0 —— 有 commit）==="
wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -c "
SELECT COUNT(*) AS idle_tx_with_advisory
FROM pg_stat_activity a
JOIN pg_locks l ON l.pid = a.pid
WHERE a.datname='$DB'
  AND a.state='idle in transaction'
  AND l.locktype='advisory';
"
