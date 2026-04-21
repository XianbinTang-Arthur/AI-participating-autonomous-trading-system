#!/bin/bash
# scripts/diag/panel_latency_histogram.sh
# 从 gateway 日志提取 parallel_fetch wall 时间，算 P50/P95/P99/max。
# 用法: bash scripts/diag/panel_latency_histogram.sh [minutes]
set -e

MINUTES="${1:-15}"
AWK_SCRIPT='
BEGIN { sum=0; max=0 }
{ arr[NR]=$1; sum+=$1; if ($1>max) max=$1 }
END {
  if (NR == 0) { print "no data"; exit }
  p50 = arr[int(NR*0.5)+1]
  p95 = arr[int(NR*0.95)+1]
  p99 = arr[int(NR*0.99)+1]
  printf("P50: %.3fs\nP95: %.3fs\nP99: %.3fs\nMax: %.3fs\nAvg: %.3fs\n", p50, p95, p99, max, sum/NR)
}'

echo "=== Panel latency histogram (last ${MINUTES}m) ==="

# WSL-internal pipeline, no Windows path issues
COUNT=$(wsl -d Ubuntu -- bash -c "docker logs aats-gateway --since ${MINUTES}m 2>&1 | grep -c parallel_fetch_slow" || echo 0)
echo "Slow events (wall > 2s): ${COUNT}"

if [ "$COUNT" = "0" ] || [ -z "$COUNT" ]; then
  echo "system 稳定，全部 wall <= 2s (无 slow 日志)"
  exit 0
fi

wsl -d Ubuntu -- bash -c "
  docker logs aats-gateway --since ${MINUTES}m 2>&1 |
    grep parallel_fetch_slow |
    grep -oE 'wall=[0-9.]+' |
    sed 's/wall=//' |
    sort -n
" | awk "$AWK_SCRIPT"
