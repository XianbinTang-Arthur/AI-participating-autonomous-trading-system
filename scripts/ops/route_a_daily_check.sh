#!/usr/bin/env bash
# Route A phase 0 — 7 天观察窗 daily health check
#
# 起算点: 2026-04-20 14:15 UTC (P0-a/b/c 全落地 + deploy 后第一次 candles
#         和 microstructure 同 cadence 对齐的 tick).
# 目标终点: 2026-04-27 14:15 UTC (Silver + OHLC 两 pipeline 连续 7×96=672 bar
#         无 gap, 才允许启动路线 A phase 0 第一份 evidence 研究).
#
# 设计原则:
#   - 只查询, 不改. 绝不触发任何 research / order / config 修改
#   - 5 分钟内跑完, 全程只读 Postgres / docker ps / 简单 grep
#   - 输出结构化 Pass/Fail, 便于逐日归档
#
# 用法:
#   # 推荐 (自动归档): 默认 tee 到 artifacts/route_a_observation_window/<UTC-date>.log
#   bash scripts/ops/route_a_daily_check.sh
#
#   # 仅看 stdout, 不落盘 (调试用):
#   AATS_SKIP_DAILY_CHECK_LOG=true bash scripts/ops/route_a_daily_check.sh
#
# Exit codes:
#   0 = 全部 check 通过 (观察窗计数 +1)
#   1 = 有 WARN (观察窗不重置, 但需要 operator 注意)
#   2 = 有 FAIL (观察窗重置, 起算点延后到问题解决日)
#
# 建议: 每日 22:00 Shanghai (~14:00 UTC, 15min tick 刚过) 跑一次.
# 脚本自动 tee 结果到 artifacts/route_a_observation_window/<YYYY-MM-DD>.log,
# 不需要 operator 记得手动 | tee (C-L4 code review fix).

set -u

readonly WSL_DISTRO="${AATS_WSL_DISTRO:-Ubuntu}"
readonly CHECK_DATE="$(date -u '+%Y-%m-%d')"
readonly CHECK_TS="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# 观察窗起点 (固定, 不能改)
readonly WINDOW_START_UTC="2026-04-20T14:15:00Z"
readonly WINDOW_TARGET_UTC="2026-04-27T14:15:00Z"

# 2026-04-20 code review C-L4 fix: 自动 tee 到 artifacts/, operator 不用
# 记得手动 `| tee -a`. AATS_SKIP_DAILY_CHECK_LOG=true 可关 (调试场景).
if [[ "${AATS_SKIP_DAILY_CHECK_LOG:-false}" != "true" ]]; then
    _log_dir="artifacts/route_a_observation_window"
    mkdir -p "$_log_dir" 2>/dev/null || true
    _log_file="${_log_dir}/${CHECK_DATE}.log"
    # 重定向全部后续 stdout/stderr 同时到屏幕和 log 文件 (append).
    # 注: 本 tee 在此行之后生效, 上面已 echo 的行不会被记; 对日志完整性够.
    exec > >(tee -a "$_log_file") 2>&1
    printf '\n──── daily check run @ %s ────\n' "$CHECK_TS"
fi

WARN_COUNT=0
FAIL_COUNT=0

log()   { printf '[%s] %s\n' "$(date -u '+%H:%M:%S')" "$*"; }
pass()  { printf '  \033[32m✓ PASS\033[0m  %s\n' "$*"; }
warn()  { printf '  \033[33m⚠ WARN\033[0m  %s\n' "$*" >&2; WARN_COUNT=$((WARN_COUNT+1)); }
fail()  { printf '  \033[31m✗ FAIL\033[0m  %s\n' "$*" >&2; FAIL_COUNT=$((FAIL_COUNT+1)); }
step()  { printf '\n═══ %s ═══\n' "$*"; }

psql_q() {
    wsl -d "$WSL_DISTRO" -- docker exec aats-postgres \
        psql -U admin -d aats_research -tA -c "$*" 2>/dev/null
}

# 专门查 live_derivatives DB (event_store / runtime snapshots 在那边)
psql_live() {
    wsl -d "$WSL_DISTRO" -- docker exec aats-postgres \
        psql -U admin -d aats_live_derivatives -tA -c "$*" 2>/dev/null
}

# ─────────────────────────────────────────────────────────────
# 0. 基本 infra
# ─────────────────────────────────────────────────────────────

step "Route A phase 0 daily check · ${CHECK_TS}"
log "观察窗: ${WINDOW_START_UTC} → ${WINDOW_TARGET_UTC}"

# ─────────────────────────────────────────────────────────────
# 1. 16 容器 healthy
# ─────────────────────────────────────────────────────────────

step "[1/7] Container health"
unhealthy=$(wsl -d "$WSL_DISTRO" -- docker ps --format '{{.Names}}\t{{.Status}}' 2>/dev/null \
    | grep '^aats-' | grep -v 'healthy' || true)
if [[ -z "$unhealthy" ]]; then
    pass "16 个 aats-* 容器全部 healthy"
else
    fail "以下容器不 healthy:"
    echo "$unhealthy" | sed 's/^/      /'
fi

# ─────────────────────────────────────────────────────────────
# 2. Silver 依赖链最新 bar 在 30min 内
# ─────────────────────────────────────────────────────────────
# 观察对象 (2026-04-23 升级):
#   - silver.market_trade_flow_15m       ← microstructure silver runner 的
#                                          watermark 基准 (见
#                                          scripts/rdp_build_microstructure_silver.py
#                                          _detect_trade_flow_watermark); 若断档
#                                          整个 silver backfill 链停摆
#   - silver.market_orderbook_metrics_15m ← microstructure silver 另一主表
#   - silver.market_swap_candles_15m      ← 独立 OHLC pipeline
# 三者必须都新鲜, 否则观察窗的"连续产出"前提不成立.

step "[2/7] Silver freshness (依赖链三表)"

# 用 epoch 方式比较, 避免 bash 日期解析复杂度
now_epoch=$(date -u +%s)

declare -A LATEST_EPOCH
for tbl in market_trade_flow_15m market_orderbook_metrics_15m market_swap_candles_15m; do
    latest=$(psql_q "SELECT EXTRACT(EPOCH FROM (MAX(ts) AT TIME ZONE 'UTC'))::bigint FROM silver.${tbl} WHERE symbol='BTC-USDT-SWAP'")
    if [[ -z "$latest" || "$latest" == "0" ]]; then
        fail "silver.${tbl}: 无数据"
        LATEST_EPOCH[$tbl]=0
        continue
    fi
    LATEST_EPOCH[$tbl]=$latest
    age=$((now_epoch - latest))
    age_min=$((age / 60))
    latest_str=$(date -u -d "@$latest" '+%Y-%m-%d %H:%M UTC')
    if [[ $age_min -le 30 ]]; then
        pass "silver.${tbl}: latest=${latest_str} (${age_min}min ago)"
    elif [[ $age_min -le 60 ]]; then
        warn "silver.${tbl}: latest=${latest_str} (${age_min}min ago, >30min)"
    else
        fail "silver.${tbl}: latest=${latest_str} (${age_min}min ago, >60min, 断档)"
    fi
done

# ─────────────────────────────────────────────────────────────
# 3. 依赖链三表同 cadence 对齐 (极差 ≤ 1 bar)
# ─────────────────────────────────────────────────────────────
# trade_flow 是 silver watermark 基准, 落后 orderbook_metrics 意味着下一 tick
# backfill 会被 watermark 拉回去补 — 短期可自愈, 长期说明 trades bronze 异常.
# candles 是独立 pipeline, 落后 = OKX candles REST/WS 异常.

step "[3/7] Cadence alignment (trade_flow / orderbook / candles 三表)"

tf_max=${LATEST_EPOCH[market_trade_flow_15m]:-0}
ob_max=${LATEST_EPOCH[market_orderbook_metrics_15m]:-0}
sw_max=${LATEST_EPOCH[market_swap_candles_15m]:-0}

if [[ "$tf_max" == "0" || "$ob_max" == "0" || "$sw_max" == "0" ]]; then
    warn "cadence 对齐跳过 (某表无数据, 见 [2/7])"
else
    max_ts=$tf_max
    min_ts=$tf_max
    for v in "$ob_max" "$sw_max"; do
        [[ $v -gt $max_ts ]] && max_ts=$v
        [[ $v -lt $min_ts ]] && min_ts=$v
    done
    diff_sec=$((max_ts - min_ts))
    diff_min=$((diff_sec / 60))

    # 找出最落后的表名方便 operator 诊断
    lag_tbl="unknown"
    for tbl in market_trade_flow_15m market_orderbook_metrics_15m market_swap_candles_15m; do
        if [[ "${LATEST_EPOCH[$tbl]}" == "$min_ts" ]]; then
            lag_tbl=$tbl
            break
        fi
    done

    if [[ $diff_min -le 15 ]]; then
        pass "三表 cadence 极差 ${diff_min}min (≤ 1 bar)"
    else
        warn "三表 cadence 极差 ${diff_min}min (> 1 bar, 最落后: silver.${lag_tbl})"
    fi
fi

# ─────────────────────────────────────────────────────────────
# 4. 最近 24h rdp_task_queue failed / timeout 统计
# ─────────────────────────────────────────────────────────────

step "[4/7] Task queue last 24h"

failed_24h=$(psql_q "
    SELECT workflow || ':' || COUNT(*)
    FROM governance.rdp_task_queue
    WHERE requested_at > NOW() - INTERVAL '24 hours'
      AND status != 'done'
    GROUP BY workflow
    ORDER BY workflow" | grep -v '^$' || true)

if [[ -z "$failed_24h" ]]; then
    pass "24h 内全部 task done"
else
    log "  24h 内非 done task 统计:"
    echo "$failed_24h" | sed 's/^/      /'
    # rolling 类工作流 ≤ 2 次不 done 算 warn, > 2 算 fail
    rolling_fails=$(echo "$failed_24h" | grep -E "microstructure_silver_15m|candles_rolling_15m" | awk -F: '{sum+=$2} END {print sum+0}')
    if [[ $rolling_fails -gt 2 ]]; then
        fail "rolling workflow 24h 非 done 数=${rolling_fails} (>2, 连续失败风险)"
    elif [[ $rolling_fails -gt 0 ]]; then
        warn "rolling workflow 24h 非 done 数=${rolling_fails} (≤2, 可接受偶发)"
    fi
fi

# ─────────────────────────────────────────────────────────────
# 5. 观察窗区间 Silver 连续性 (无 gap)
# ─────────────────────────────────────────────────────────────

step "[5/7] Gap detection in observation window (依赖链三表)"

for tbl in market_trade_flow_15m market_orderbook_metrics_15m market_swap_candles_15m; do
    gap_count=$(psql_q "
        WITH observed AS (
            SELECT ts FROM silver.${tbl}
            WHERE symbol='BTC-USDT-SWAP'
              AND ts >= '${WINDOW_START_UTC}'::timestamptz
        ),
        expected AS (
            SELECT generate_series(
                GREATEST('${WINDOW_START_UTC}'::timestamptz, (SELECT MIN(ts) FROM observed)),
                (SELECT MAX(ts) FROM observed),
                INTERVAL '15 minutes'
            ) AS ts
        )
        SELECT COUNT(*) FROM expected e
        LEFT JOIN observed o ON e.ts = o.ts
        WHERE o.ts IS NULL")

    if [[ "$gap_count" == "0" ]]; then
        pass "silver.${tbl}: 观察窗内零 gap"
    elif [[ "$gap_count" -le 1 ]]; then
        warn "silver.${tbl}: 观察窗内 ${gap_count} 个 gap (允许 ≤ 1, 偶发可接受)"
    else
        fail "silver.${tbl}: 观察窗内 ${gap_count} 个 gap (>1, 观察窗需重置)"
    fi
done

# ─────────────────────────────────────────────────────────────
# 6. Microstructure 24h empty-bar / no-data 饿死检测
# ─────────────────────────────────────────────────────────────
# 为什么需要这个 check:
#   freshness/cadence/gap 看的是 "silver row 是否按时落地".
#   但 microstructure silver runner 遇到 bronze 全空时仍会 commit
#   一行 NULL/0 指标 + quality_flags=['trades_no_data' / 'orderbook_*_no_data']
#   并把 watermark 推到本 bar (commit c331e2b COMMITTED_BUT_EMPTY).
#   —— 于是 freshness/cadence/gap 三项全绿, 其实 input 已饿死, 观察窗
#   "连续产出" 前提被挖空.
#
# 本 check 用 quality_flags 数组直接扫最近 24h 的饿死 bar:
#   - trade_flow: 'trades_no_data' = ANY(quality_flags)
#       → bronze.market_trades 断了, 同时也会拖 volume_profile NULL
#   - orderbook:  bbo_no_data AND books5_no_data 双命中
#       → 和 merger 的 _TABLE_NO_DATA_TRIGGERS['orderbook'] 对齐, 只在
#         两档 books 都断时才算整张 orderbook row 完全饿死;
#         单 source 缺只算 partial, 不触发本检查以免噪音.
#
# 阈值: 24h = 96 bar. 0 PASS / 1-4 WARN / >4 FAIL.
#   - 1-4 bar (≤ 4%, ≤ 1h) 容忍 collector 重启 / WS 断线瞬时自愈;
#   - > 4 bar (> 1h 连续) 说明 bronze 上游系统性中断, 即使 watermark
#     在推, row 也只是空壳, 观察窗必须 reset.

step "[6/7] Microstructure empty-bar / no-data 24h"

# trade_flow: 单 flag 即代表整张表 row 饿死
tf_empty=$(psql_q "
    SELECT COUNT(*) FROM silver.market_trade_flow_15m
    WHERE symbol='BTC-USDT-SWAP'
      AND ts > NOW() - INTERVAL '24 hours'
      AND 'trades_no_data' = ANY(quality_flags)")
tf_empty=${tf_empty:-0}

# orderbook: bbo + books5 必须都 no_data 才算整行饿死 (与 merger 对齐)
ob_empty=$(psql_q "
    SELECT COUNT(*) FROM silver.market_orderbook_metrics_15m
    WHERE symbol='BTC-USDT-SWAP'
      AND ts > NOW() - INTERVAL '24 hours'
      AND 'orderbook_bbo_no_data' = ANY(quality_flags)
      AND 'orderbook_books5_no_data' = ANY(quality_flags)")
ob_empty=${ob_empty:-0}

for pair in "market_trade_flow_15m:${tf_empty}" "market_orderbook_metrics_15m:${ob_empty}"; do
    tbl=${pair%%:*}
    cnt=${pair##*:}
    if [[ "$cnt" == "0" ]]; then
        pass "silver.${tbl}: 24h 内 0 个 COMMITTED_BUT_EMPTY bar"
    elif [[ $cnt -le 4 ]]; then
        warn "silver.${tbl}: 24h 内 ${cnt} 个 empty/no_data bar (≤4, 偶发可接受; 查 bronze collector 健康)"
    else
        fail "silver.${tbl}: 24h 内 ${cnt} 个 empty/no_data bar (>4, bronze 系统性饿死; 观察窗需重置)"
    fi
done

# ─────────────────────────────────────────────────────────────
# 7. Runtime mode 仍是 baseline_only (不允许期间切换)
# ─────────────────────────────────────────────────────────────

step "[7/7] Runtime mode guard"

mode=$(psql_live "SELECT payload::jsonb->>'ai_operating_mode' FROM public.event_store WHERE topic='strategy.decision_outcome' ORDER BY event_timestamp DESC LIMIT 1")
if [[ "$mode" == "baseline_only" ]]; then
    pass "ai_operating_mode=baseline_only (符合观察窗纪律)"
elif [[ -z "$mode" ]]; then
    warn "无 decision_outcome 记录 (可能 decision 没在跑, 注意)"
else
    fail "ai_operating_mode=${mode} (期间不应切换! 观察窗需重置)"
fi

# ─────────────────────────────────────────────────────────────
# 观察窗进度 (informational, 不计入 check 统计)
# ─────────────────────────────────────────────────────────────

step "进度"
start_epoch=$(date -u -d "2026-04-20T14:15:00Z" +%s)
target_epoch=$(date -u -d "2026-04-27T14:15:00Z" +%s)
elapsed=$((now_epoch - start_epoch))
total=$((target_epoch - start_epoch))
pct=$((elapsed * 100 / total))
days_elapsed=$(awk "BEGIN {printf \"%.2f\", ${elapsed}/86400}")

if [[ $now_epoch -lt $start_epoch ]]; then
    log "  观察窗尚未开始 (还有 $(( (start_epoch - now_epoch) / 3600 ))h)"
elif [[ $now_epoch -ge $target_epoch ]]; then
    log "  观察窗已达 7 天 (${days_elapsed}d elapsed / 100%+)"
    log "  若所有 check 通过, 可启动路线 A phase 0 第一份 evidence 研究"
else
    log "  观察窗已跑 ${days_elapsed} 天 / 7 天 (${pct}%)"
fi

# ─────────────────────────────────────────────────────────────
# 总结
# ─────────────────────────────────────────────────────────────

step "SUMMARY"
log "Warnings : ${WARN_COUNT}"
log "Fails    : ${FAIL_COUNT}"

if [[ $FAIL_COUNT -gt 0 ]]; then
    printf '\n\033[31m✗ OVERALL: FAIL\033[0m — 观察窗需重置, 修问题后从当前日重起\n'
    exit 2
elif [[ $WARN_COUNT -gt 0 ]]; then
    printf '\n\033[33m⚠ OVERALL: PASS WITH WARN\033[0m — 观察窗计数 +1 天, 注意 warn\n'
    exit 1
else
    printf '\n\033[32m✓ OVERALL: PASS\033[0m — 观察窗计数 +1 天\n'
    exit 0
fi
