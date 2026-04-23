#!/usr/bin/env bash
# Route A phase 0 — 7 天观察窗 daily health check
#
# 起算点: 2026-04-22 00:00 UTC (2026-04-23 daily check 发现
#         silver.market_trade_flow_15m / silver.market_orderbook_metrics_15m 在
#         2026-04-21 21:30 / 23:15 / 23:45 UTC 存在 3 个历史 gap; 从
#         2026-04-22 00:00 UTC 起两表连续性洁净, 观察窗据此 reset).
# 目标终点: 2026-04-29 00:00 UTC (起点 + 7 天 = 672 bar, Silver + OHLC 两
#         pipeline 连续无 gap, 才允许启动路线 A phase 0 第一份 evidence 研究).
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
#   # 跳过机读 JSON summary 落盘 (调试用, 例如手跑时不想覆盖当日 JSON):
#   AATS_SKIP_DAILY_CHECK_JSON=true bash scripts/ops/route_a_daily_check.sh
#
# Machine-readable summary:
#   除了 human-readable tee log, 每次跑完自动写
#   artifacts/route_a_observation_window/<YYYY-MM-DD>.json (最近一次运行
#   快照, 同日多次跑会覆盖). 字段见 ops doc §5.
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
readonly WINDOW_START_UTC="2026-04-22T00:00:00Z"
readonly WINDOW_TARGET_UTC="2026-04-29T00:00:00Z"

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

# 记录每个 check 的状态, 用于最后写机读 JSON summary (automation / PM loop
# 可 stable consume, 不用 scrape 终端文本). CURRENT_SECTION 由 step() 更新,
# pass/warn/fail 把 (section, status, message) 塞进 STATUS_ENTRIES.
CURRENT_SECTION=""
STATUS_ENTRIES=()

# JSON string body 转义: 只处理 \ 和 " (pass/warn/fail 消息均为单行, 无需
# 处理换行 / 控制字符; 引入 python/jq 属过度工程).
_json_escape() {
    local s="$1"
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    printf '%s' "$s"
}

_record_status() {
    # $1=status (pass|warn|fail), $2=message
    local status="$1" message="$2"
    local safe_section safe_msg
    safe_section=$(_json_escape "$CURRENT_SECTION")
    safe_msg=$(_json_escape "$message")
    STATUS_ENTRIES+=("{\"section\":\"${safe_section}\",\"status\":\"${status}\",\"message\":\"${safe_msg}\"}")
}

log()   { printf '[%s] %s\n' "$(date -u '+%H:%M:%S')" "$*"; }
pass()  { _record_status pass "$*"; printf '  \033[32m✓ PASS\033[0m  %s\n' "$*"; }
warn()  { _record_status warn "$*"; printf '  \033[33m⚠ WARN\033[0m  %s\n' "$*" >&2; WARN_COUNT=$((WARN_COUNT+1)); }
fail()  { _record_status fail "$*"; printf '  \033[31m✗ FAIL\033[0m  %s\n' "$*" >&2; FAIL_COUNT=$((FAIL_COUNT+1)); }
step()  { CURRENT_SECTION="$*"; printf '\n═══ %s ═══\n' "$*"; }

# infra/query 失败哨兵. psql_q / psql_live 遇到 wsl / docker / psql 任一环节
# 非零退出时在 stdout 输出该值, 同时把 stderr 摘要打到屏幕; 下游调用点用
# is_psql_err 显式识别并进入 FAIL 分支, 避免把 infra 故障折叠成 "空表" 或
# 默认 0, 误判为 PASS / WARN.
readonly PSQL_ERR='__PSQL_ERR__'

is_psql_err() { [[ "${1-}" == "$PSQL_ERR" ]]; }

_psql_run() {
    # 内部实现: $1=db, 其余=SQL.
    local db="$1"; shift
    local out rc err_file
    err_file=$(mktemp 2>/dev/null || echo "/tmp/aats_psql_err.$$")
    out=$(wsl -d "$WSL_DISTRO" -- docker exec aats-postgres \
        psql -U admin -d "$db" -tA -c "$*" 2>"$err_file")
    rc=$?
    if (( rc != 0 )); then
        local err_msg
        err_msg=$(tr '\n' ' ' <"$err_file" 2>/dev/null)
        rm -f "$err_file" 2>/dev/null || true
        printf 'psql 查询失败 (rc=%s, db=%s): %s\n' "$rc" "$db" "${err_msg:-<no stderr>}" >&2
        printf '%s' "$PSQL_ERR"
        return "$rc"
    fi
    rm -f "$err_file" 2>/dev/null || true
    printf '%s' "$out"
}

psql_q()    { _psql_run aats_research "$@"; }
psql_live() { _psql_run aats_live_derivatives "$@"; }

# ─────────────────────────────────────────────────────────────
# 0. 基本 infra
# ─────────────────────────────────────────────────────────────

step "Route A phase 0 daily check · ${CHECK_TS}"
log "观察窗: ${WINDOW_START_UTC} → ${WINDOW_TARGET_UTC}"

# ─────────────────────────────────────────────────────────────
# 1. 16 容器 healthy
# ─────────────────────────────────────────────────────────────

step "[1/7] Container health"
# 用 rc + 行数双保险: wsl / docker 挂或无 aats-* 容器时显式 FAIL, 不能因
# "grep 什么都没匹配到" 走进假 PASS 分支.
docker_ps_err=$(mktemp 2>/dev/null || echo "/tmp/aats_docker_ps.$$")
docker_ps_out=$(wsl -d "$WSL_DISTRO" -- docker ps --format '{{.Names}}\t{{.Status}}' 2>"$docker_ps_err")
docker_ps_rc=$?
if (( docker_ps_rc != 0 )); then
    fail "docker ps 查询失败 (rc=${docker_ps_rc}): $(tr '\n' ' ' <"$docker_ps_err" 2>/dev/null)"
else
    aats_lines=$(printf '%s\n' "$docker_ps_out" | grep '^aats-' || true)
    aats_total=$(printf '%s\n' "$aats_lines" | grep -c '^aats-' || true)
    unhealthy=$(printf '%s\n' "$aats_lines" | grep -v 'healthy' | grep -v '^$' || true)
    if (( aats_total == 0 )); then
        fail "docker ps 未返回任何 aats-* 容器 (wsl/docker 可能不可用)"
    elif [[ -z "$unhealthy" ]]; then
        pass "${aats_total} 个 aats-* 容器全部 healthy"
    else
        fail "以下容器不 healthy:"
        echo "$unhealthy" | sed 's/^/      /'
    fi
fi
rm -f "$docker_ps_err" 2>/dev/null || true

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
    if is_psql_err "$latest"; then
        fail "silver.${tbl}: freshness 查询失败 (infra/数据源不可用, 见上方 stderr)"
        LATEST_EPOCH[$tbl]=0
        continue
    fi
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

failed_24h_raw=$(psql_q "
    SELECT workflow || ':' || COUNT(*)
    FROM governance.rdp_task_queue
    WHERE requested_at > NOW() - INTERVAL '24 hours'
      AND status != 'done'
    GROUP BY workflow
    ORDER BY workflow")

if is_psql_err "$failed_24h_raw"; then
    fail "task queue 24h 查询失败 (infra/数据源不可用, 见上方 stderr)"
else
    failed_24h=$(printf '%s\n' "$failed_24h_raw" | grep -v '^$' || true)
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
fi

# 4b. rolling workflow 末段连续未 done streak (2026-04-23 补)
# 为什么单独看 streak: 上面 4a 只数 24h 总非 done 数, 把 "3 次稀疏失败 + 中间
# 各有 done 自愈" 与 "3 次最近 tick 连续失败" 视为同一情况. 后者意味着 rolling
# 自愈链路 (allow_failure=true → 下一 tick 自动重试) 已失效, 即使 freshness
# (check 2) 还没掉到 30min 阈值, 也很快会掉. 单独追一次 streak 让 operator
# 在风险显化前看到信号.
#
# SQL 取每个 rolling workflow 上次 done 之后 (24h 范围内) 的非 done 数 =
# 末段连续 streak. 因为 streak 是 4a 总数的子集, 永远不会把 4a 的 PASS
# 提级为 FAIL — 只是补充自愈语义 (保守).

contiguous_raw=$(psql_q "
    WITH last_done AS (
        SELECT workflow, MAX(requested_at) AS last_done_at
        FROM governance.rdp_task_queue
        WHERE workflow IN ('microstructure_silver_15m', 'candles_rolling_15m')
          AND status = 'done'
        GROUP BY workflow
    )
    SELECT q.workflow || ':' || COUNT(*)
    FROM governance.rdp_task_queue q
    LEFT JOIN last_done d ON d.workflow = q.workflow
    WHERE q.workflow IN ('microstructure_silver_15m', 'candles_rolling_15m')
      AND q.status != 'done'
      AND (d.last_done_at IS NULL OR q.requested_at > d.last_done_at)
      AND q.requested_at > NOW() - INTERVAL '24 hours'
    GROUP BY q.workflow
    ORDER BY q.workflow")

if is_psql_err "$contiguous_raw"; then
    fail "task queue 24h contiguous streak 查询失败 (infra/数据源不可用, 见上方 stderr)"
else
    contiguous_streak=$(printf '%s\n' "$contiguous_raw" | grep -v '^$' || true)
    if [[ -n "$contiguous_streak" ]]; then
        log "  rolling workflow 末段连续未 done streak (自上次 done 之后, 24h 内):"
        echo "$contiguous_streak" | sed 's/^/      /'
        max_streak=$(echo "$contiguous_streak" | awk -F: 'BEGIN{m=0} {if ($2+0 > m) m=$2+0} END {print m+0}')
        worst_wf=$(echo "$contiguous_streak" | awk -F: -v m="$max_streak" '$2+0 == m {print $1; exit}')
        if [[ $max_streak -ge 3 ]]; then
            fail "rolling workflow ${worst_wf} 末段连续未 done streak=${max_streak} (≥3, 自愈链路断, 观察窗需重置)"
        elif [[ $max_streak -ge 2 ]]; then
            warn "rolling workflow ${worst_wf} 末段连续未 done streak=${max_streak} (≥2, 自愈未生效, 排查 log_tail)"
        fi
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

    if is_psql_err "$gap_count"; then
        fail "silver.${tbl}: gap 查询失败 (infra/数据源不可用, 见上方 stderr)"
    elif [[ -z "$gap_count" || ! "$gap_count" =~ ^[0-9]+$ ]]; then
        # COUNT(*) 必定返回一行非负整数, 空串 / 非数字 = 查询意外异常, 不容落 PASS
        fail "silver.${tbl}: gap 查询返回非预期输出='${gap_count}' (视为 infra 异常)"
    elif [[ "$gap_count" == "0" ]]; then
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
# 注意: 不能用 `${tf_empty:-0}` 把查询失败折叠成 0 — 必须区分 "查询失败 (infra)"
# 与 "查询成功返回 0 (真无饿死)". 前者需 FAIL, 后者才 PASS.
tf_empty=$(psql_q "
    SELECT COUNT(*) FROM silver.market_trade_flow_15m
    WHERE symbol='BTC-USDT-SWAP'
      AND ts > NOW() - INTERVAL '24 hours'
      AND 'trades_no_data' = ANY(quality_flags)")

# orderbook: bbo + books5 必须都 no_data 才算整行饿死 (与 merger 对齐)
ob_empty=$(psql_q "
    SELECT COUNT(*) FROM silver.market_orderbook_metrics_15m
    WHERE symbol='BTC-USDT-SWAP'
      AND ts > NOW() - INTERVAL '24 hours'
      AND 'orderbook_bbo_no_data' = ANY(quality_flags)
      AND 'orderbook_books5_no_data' = ANY(quality_flags)")

for pair in "market_trade_flow_15m:${tf_empty}" "market_orderbook_metrics_15m:${ob_empty}"; do
    tbl=${pair%%:*}
    cnt=${pair##*:}
    if is_psql_err "$cnt"; then
        fail "silver.${tbl}: 24h empty-bar 查询失败 (infra/数据源不可用, 见上方 stderr)"
        continue
    fi
    if [[ -z "$cnt" || ! "$cnt" =~ ^[0-9]+$ ]]; then
        fail "silver.${tbl}: 24h empty-bar 查询返回非预期输出='${cnt}' (视为 infra 异常)"
        continue
    fi
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
if is_psql_err "$mode"; then
    fail "runtime mode 查询失败 (aats_live_derivatives 不可用, 见上方 stderr)"
elif [[ "$mode" == "baseline_only" ]]; then
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
start_epoch=$(date -u -d "$WINDOW_START_UTC" +%s)
target_epoch=$(date -u -d "$WINDOW_TARGET_UTC" +%s)
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
    OVERALL="fail"
    EXIT_CODE=2
elif [[ $WARN_COUNT -gt 0 ]]; then
    OVERALL="pass_with_warn"
    EXIT_CODE=1
else
    OVERALL="pass"
    EXIT_CODE=0
fi

# 机读 summary: 供 automation / PM loop stable consume (不用 scrape 终端).
# 同日多次跑会覆盖, log 文件保留完整历史; 跳过 JSON 落盘可设
# AATS_SKIP_DAILY_CHECK_JSON=true (和 _LOG 对称).
if [[ "${AATS_SKIP_DAILY_CHECK_JSON:-false}" != "true" ]]; then
    _json_dir="artifacts/route_a_observation_window"
    mkdir -p "$_json_dir" 2>/dev/null || true
    _json_file="${_json_dir}/${CHECK_DATE}.json"
    {
        printf '{'
        printf '"generated_at":"%s",' "$CHECK_TS"
        printf '"window_start":"%s",' "$WINDOW_START_UTC"
        printf '"window_target":"%s",' "$WINDOW_TARGET_UTC"
        printf '"overall":"%s",' "$OVERALL"
        printf '"exit_code":%s,' "$EXIT_CODE"
        printf '"warn_count":%s,' "$WARN_COUNT"
        printf '"fail_count":%s,' "$FAIL_COUNT"
        printf '"checks":['
        _first=1
        # bash 4.4+ 下 "${arr[@]}" 对空数组在 set -u 中安全, 无需额外保护.
        for _entry in "${STATUS_ENTRIES[@]}"; do
            if (( _first )); then _first=0; else printf ','; fi
            printf '%s' "$_entry"
        done
        printf ']}'
        printf '\n'
    } >"$_json_file" 2>/dev/null || log "  (warn: JSON summary 写入失败, 不影响 check 结果)"
fi

case "$EXIT_CODE" in
    2) printf '\n\033[31m✗ OVERALL: FAIL\033[0m — 观察窗需重置, 修问题后从当前日重起\n' ;;
    1) printf '\n\033[33m⚠ OVERALL: PASS WITH WARN\033[0m — 观察窗计数 +1 天, 注意 warn\n' ;;
    0) printf '\n\033[32m✓ OVERALL: PASS\033[0m — 观察窗计数 +1 天\n' ;;
esac
exit "$EXIT_CODE"
