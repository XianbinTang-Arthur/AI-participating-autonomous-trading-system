#!/bin/bash
# scripts/diag/recovery_rollback_gap.sh
# 实时采样 idle-in-tx 的 state_change → now gap，测量实际的 session.close() → rollback 延迟。
# 用法: bash scripts/diag/recovery_rollback_gap.sh [ticks]
#   ticks: 采样次数（默认 30），每次间隔 1s
set -e

TICKS="${1:-30}"
DB="${AATS_DIAG_DB:-aats_live_derivatives}"

echo "=== Rollback gap sampling ($TICKS ticks, 1s interval) ==="
echo "Each line: tick | pid | gap (s since state='idle in tx') | xact_age"

for i in $(seq 1 "$TICKS"); do
  wsl -d Ubuntu -- docker exec aats-postgres psql -U admin -d "$DB" -tc "
    SELECT 'tick=$i ' || pid || '|gap=' ||
           ROUND(EXTRACT(EPOCH FROM NOW()-state_change)::numeric, 3) || 's|xact_age=' ||
           ROUND(EXTRACT(EPOCH FROM NOW()-xact_start)::numeric, 3) || 's'
    FROM pg_stat_activity
    WHERE datname='$DB'
      AND state='idle in transaction'
      AND query NOT LIKE '%pg_try_advisory_lock%';
  " 2>&1 | grep -v '^$' | tr -d ' '
  sleep 1
done

echo ""
echo "=== 基线参考 ==="
echo "正常: gap < 0.1s (ms 级)"
echo "轻度异常: gap 0.5-3s (GIL/threading 瓶颈)"
echo "严重: gap > 10s (sessionClose 真卡住 / pool 耗尽)"
