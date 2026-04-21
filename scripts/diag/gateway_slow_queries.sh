#!/bin/bash
# scripts/diag/gateway_slow_queries.sh
# 汇总 gateway `parallel_fetch_slow` 日志，按 wall 时间降序 top-N。
# 用法: bash scripts/diag/gateway_slow_queries.sh [top_n] [since_minutes]
set -e

TOP_N="${1:-10}"
SINCE_MIN="${2:-60}"

echo "=== parallel_fetch_slow top ${TOP_N} (last ${SINCE_MIN}m) ==="
wsl -d Ubuntu -- docker logs aats-gateway --since "${SINCE_MIN}m" 2>&1 |
  grep -oE 'wall=[0-9.]+s queries=[0-9]+ depth=[0-9]+ top5=\[[^]]+\]' |
  sort -rn |
  head -"${TOP_N}"

echo ""
echo "=== 当前 1min 窗口 slow 数量 ==="
COUNT=$(wsl -d Ubuntu -- docker logs aats-gateway --since 1m 2>&1 | grep -c 'parallel_fetch_slow' || true)
echo "last 1m slow events: $COUNT"

echo ""
echo "=== 慢 top 5 panel（按累计出现次数） ==="
wsl -d Ubuntu -- docker logs aats-gateway --since "${SINCE_MIN}m" 2>&1 |
  grep -oE 'top5=\[[^]]+\]' |
  grep -oE '\b[a-z_]+=[0-9]+\.[0-9]+s' |
  awk -F= '{print $1}' |
  sort |
  uniq -c |
  sort -rn |
  head -10
