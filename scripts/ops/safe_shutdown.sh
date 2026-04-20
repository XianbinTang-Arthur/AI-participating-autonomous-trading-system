#!/usr/bin/env bash
# AATS Safe Shutdown — 真金白银系统的可审计停机脚本
#
# 设计文档: docs/operations/safe_shutdown_design_2026_04_20.md
#
# 三阶段:
#   Phase 0  Preflight (read-only, 不可跳过除非 --skip-preflight)
#   Phase 1  App-layer 逆流 graceful shutdown
#   Phase 2  Infra shutdown (Redis BGSAVE / Postgres pg_ctl fast)
#   Phase 3  验证 + snapshot 报告
#
# 用法:
#   bash scripts/ops/safe_shutdown.sh                            # 默认 dry-run
#   bash scripts/ops/safe_shutdown.sh --apply --confirm          # 真执行
#   bash scripts/ops/safe_shutdown.sh --apply --confirm --force-with-money --reason "emergency"
#
# 退出码:
#   0 = 全部 graceful
#   1 = 有 force_kill (部分 kill -9)
#   2 = 部分容器未停
#   3 = preflight abort (open orders/positions 且无 --force-with-money)
#   4 = 参数错误
#   5 = 无 docker 或 Postgres 不可达 (且未 skip preflight)

set -u   # 未定义变量报错; 不用 -e 是因为我们要对每步自己处理错误

# ─────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────

readonly SCRIPT_VERSION="1.0.0-2026_04_20"
readonly WSL_DISTRO="${AATS_WSL_DISTRO:-Ubuntu}"

# Phase 1 app-layer 逆流顺序
readonly -a APP_CONTAINERS=(
    "aats-execution"
    "aats-decision"
    "aats-rdp-daemon"
    "aats-microstructure-collector"
    "aats-liquidations-daemon"
    "aats-market"
    "aats-gateway"
)

# Phase 2 infra 顺序 (Postgres 最后)
readonly -a INFRA_CONTAINERS_EARLY=(
    "aats-grafana"
    "aats-prometheus"
    "aats-promtail"
    "aats-loki"
    "aats-jaeger"
    "aats-redis-exporter"
)

readonly -a INFRA_CONTAINERS_DATA=(
    "aats-redis"
    "aats-nats"
)

readonly POSTGRES_CONTAINER="aats-postgres"

# ─────────────────────────────────────────────────────────────
# 参数解析
# ─────────────────────────────────────────────────────────────

DRY_RUN=1
APPLY=0
CONFIRM=0
FORCE_WITH_MONEY=0
SKIP_PREFLIGHT=0
PRESERVE_POSTGRES=0
REASON="manual_shutdown"
TIMEOUT_APP_LAYER=10
TIMEOUT_POSTGRES=30

usage() {
    cat <<EOF
AATS Safe Shutdown v${SCRIPT_VERSION}

Usage: bash $0 [OPTIONS]

Options:
  --dry-run              (默认) preflight + 打印计划, 不实际停机
  --apply                实际执行
  --confirm              与 --apply 配对, 必须显式加
  --force-with-money     允许在 open orders/positions 非空时继续
  --skip-preflight       跳过 preflight (紧急停机)
  --preserve-postgres    只停 apps + infra, 保留 Postgres
  --reason TEXT          停机理由 (默认 "manual_shutdown")
  --timeout-app-layer N  App-layer 步骤超时秒 (默认 10)
  --timeout-postgres N   Postgres 超时秒 (默认 30)
  --help                 打印此帮助

Examples:
  bash $0 --reason "war_room"
  bash $0 --apply --confirm --reason "war_room"
  bash $0 --apply --confirm --force-with-money --reason "emergency"
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)           DRY_RUN=1; shift ;;
        --apply)             APPLY=1; DRY_RUN=0; shift ;;
        --confirm)           CONFIRM=1; shift ;;
        --force-with-money)  FORCE_WITH_MONEY=1; shift ;;
        --skip-preflight)    SKIP_PREFLIGHT=1; shift ;;
        --preserve-postgres) PRESERVE_POSTGRES=1; shift ;;
        --reason)            REASON="${2:-manual_shutdown}"; shift 2 ;;
        --timeout-app-layer) TIMEOUT_APP_LAYER="${2:-10}"; shift 2 ;;
        --timeout-postgres)  TIMEOUT_POSTGRES="${2:-30}"; shift 2 ;;
        --help|-h)           usage; exit 0 ;;
        *)                   echo "ERROR: 未知参数 $1" >&2; usage; exit 4 ;;
    esac
done

if [[ ${APPLY} -eq 1 && ${CONFIRM} -eq 0 ]]; then
    echo "ERROR: --apply 必须配 --confirm (安全门)" >&2
    exit 4
fi

# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────

NOW_UTC="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
SESSION_TS="$(date -u '+%Y%m%d_%H%M%S')"
SNAPSHOT_DIR="artifacts/shutdown_snapshots"
SNAPSHOT_FILE="${SNAPSHOT_DIR}/${SESSION_TS}_${REASON//[^a-zA-Z0-9_]/_}.json"

FORCE_KILL_LIST=()
FAILED_STOP_LIST=()
WARNINGS=()

log()  { printf '[%s] %s\n' "$(date -u '+%H:%M:%S')" "$*"; }
warn() { printf '[%s] WARN: %s\n' "$(date -u '+%H:%M:%S')" "$*" >&2; WARNINGS+=("$*"); }
err()  { printf '[%s] ERROR: %s\n' "$(date -u '+%H:%M:%S')" "$*" >&2; }
step() { printf '\n═══ %s ═══\n' "$*"; }

wsl_docker() {
    wsl -d "$WSL_DISTRO" -- docker "$@"
}

wsl_psql() {
    # 在 aats-postgres 容器里跑 psql, 返回 row (tuples only)
    local db="$1"; shift
    wsl -d "$WSL_DISTRO" -- docker exec "$POSTGRES_CONTAINER" psql -U admin -d "$db" -tA "$@" 2>/dev/null
}

container_running() {
    wsl_docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$1"
}

stop_container() {
    local name="$1"
    local timeout="$2"
    if ! container_running "$name"; then
        log "  $name: 已经不在 running, 跳过"
        return 0
    fi
    if [[ ${DRY_RUN} -eq 1 ]]; then
        log "  [DRY-RUN] docker stop --time $timeout $name"
        return 0
    fi
    log "  docker stop --time $timeout $name"
    if wsl_docker stop --time "$timeout" "$name" >/dev/null 2>&1; then
        log "    ✓ $name stopped gracefully"
        return 0
    fi
    warn "$name graceful stop 超时/失败, 升级 kill"
    if wsl_docker kill "$name" >/dev/null 2>&1; then
        FORCE_KILL_LIST+=("$name")
        return 0
    fi
    FAILED_STOP_LIST+=("$name")
    err "$name kill 也失败, 需要人工处理"
    return 1
}

# ─────────────────────────────────────────────────────────────
# Phase 0 — Preflight
# ─────────────────────────────────────────────────────────────

preflight_abort_if_money=0
preflight_json=""

run_preflight() {
    step "Phase 0  Preflight (read-only)"

    # 0.1 docker 可用
    if ! wsl_docker ps -q >/dev/null 2>&1; then
        err "docker 不可达 (WSL2: $WSL_DISTRO), 不能继续"
        exit 5
    fi
    log "0.1 docker 可达 ✓"

    # 0.2 Postgres 可达
    local pg_ok=0
    if container_running "$POSTGRES_CONTAINER"; then
        if wsl_psql aats_live_derivatives -c "SELECT 1" >/dev/null 2>&1; then
            pg_ok=1
            log "0.2 Postgres 可达 ✓"
        fi
    fi
    if [[ $pg_ok -eq 0 ]]; then
        if [[ ${SKIP_PREFLIGHT} -eq 1 ]]; then
            warn "0.2 Postgres 不可达, --skip-preflight 强行继续"
        else
            err "0.2 Postgres 不可达; 无法读 OKX 端状态. 用 --skip-preflight 紧急停机 (不推荐)"
            exit 5
        fi
    fi

    # 0.3 容器现状
    step_json_containers="$(wsl_docker ps --format '{{.Names}}|{{.Status}}' 2>/dev/null | grep '^aats-' || true)"
    running_count=$(echo "$step_json_containers" | grep -c '.' || true)
    log "0.3 当前 aats-* running 容器: $running_count 个"
    echo "$step_json_containers" | sed 's/^/    /'

    # 0.4 OKX 端快照 (从 event_store 读最新 account.snapshots)
    local open_orders=0
    local positions_count=0
    local position_notional_usd="0.00"
    local in_flight_orders=0
    local recent_non_hold=0
    local runtime_mode="unknown"

    if [[ $pg_ok -eq 1 ]]; then
        local acc_json
        acc_json="$(wsl_psql aats_live_derivatives \
            -c "SELECT payload::jsonb FROM public.event_store WHERE topic='account.snapshots' ORDER BY event_timestamp DESC LIMIT 1" || true)"
        if [[ -n "$acc_json" ]]; then
            open_orders=$(echo "$acc_json" | python3 -c "import json,sys; d=json.loads(sys.stdin.read() or '{}'); print(len(d.get('open_orders') or []))" 2>/dev/null || echo 0)
            positions_count=$(echo "$acc_json" | python3 -c "import json,sys; d=json.loads(sys.stdin.read() or '{}'); ps=[p for p in (d.get('positions') or []) if float(p.get('quantity',0) or 0) != 0]; print(len(ps))" 2>/dev/null || echo 0)
            position_notional_usd=$(echo "$acc_json" | python3 -c "import json,sys; d=json.loads(sys.stdin.read() or '{}'); ps=(d.get('positions') or []); print(sum(abs(float(p.get('notional_usd',0) or 0)) for p in ps))" 2>/dev/null || echo "0.00")
        fi

        in_flight_orders=$(wsl_psql aats_live_derivatives \
            -c "SELECT COUNT(*) FROM public.execution_orders WHERE state IN ('PENDING','SUBMITTED','PARTIALLY_FILLED')" || echo 0)

        recent_non_hold=$(wsl_psql aats_live_derivatives \
            -c "SELECT COUNT(*) FROM public.event_store WHERE topic='strategy.decision_outcome' AND event_timestamp > NOW() - INTERVAL '5 minutes' AND (payload::jsonb->>'final_action') != 'hold'" || echo 0)

        runtime_mode=$(wsl_psql aats_live_derivatives \
            -c "SELECT payload::jsonb->>'ai_operating_mode' FROM public.event_store WHERE topic='strategy.decision_outcome' ORDER BY event_timestamp DESC LIMIT 1" || echo "unknown")
    fi

    log "0.4 OKX 端快照 (from Postgres cache):"
    log "    open_orders          : $open_orders"
    log "    positions (non-zero) : $positions_count"
    log "    position_notional_usd: $position_notional_usd"
    log "    in_flight_orders     : $in_flight_orders"
    log "    recent_non_hold (5m) : $recent_non_hold"
    log "    ai_operating_mode    : $runtime_mode"

    # 0.5 judgment
    local any_money=0
    if [[ "$open_orders" -gt 0 ]]; then warn "有 $open_orders 个 OKX 挂单 (停机后留在 OKX, 可能被撮合)"; any_money=1; fi
    if [[ "$positions_count" -gt 0 ]]; then warn "有 $positions_count 个持仓 (notional=\$$position_notional_usd USD, 带仓停机)"; any_money=1; fi
    if [[ "$in_flight_orders" -gt 0 ]]; then warn "有 $in_flight_orders 个 in-flight 订单状态"; any_money=1; fi
    if [[ "$recent_non_hold" -gt 0 ]]; then warn "最近 5 分钟有 $recent_non_hold 个非 hold decision (实盘中)"; any_money=1; fi
    if [[ "$runtime_mode" != "baseline_only" && "$runtime_mode" != "unknown" ]]; then warn "runtime mode=$runtime_mode (非 baseline_only, 实盘授权中)"; any_money=1; fi

    if [[ $any_money -eq 1 && ${FORCE_WITH_MONEY} -eq 0 ]]; then
        preflight_abort_if_money=1
    fi

    # 写入 snapshot 数据 (内存中, 最后落盘)
    preflight_json=$(cat <<EOF
{
  "session_ts": "$NOW_UTC",
  "script_version": "$SCRIPT_VERSION",
  "reason": "$REASON",
  "dry_run": $DRY_RUN,
  "force_with_money": $FORCE_WITH_MONEY,
  "skip_preflight": $SKIP_PREFLIGHT,
  "preserve_postgres": $PRESERVE_POSTGRES,
  "preflight": {
    "postgres_reachable": $pg_ok,
    "running_containers": $running_count,
    "open_orders": $open_orders,
    "positions_count": $positions_count,
    "position_notional_usd": "$position_notional_usd",
    "in_flight_orders": $in_flight_orders,
    "recent_non_hold_5m": $recent_non_hold,
    "ai_operating_mode": "$runtime_mode",
    "any_money_at_risk": $any_money
  }
}
EOF
)

    if [[ $preflight_abort_if_money -eq 1 ]]; then
        err "Preflight 发现资金/实盘风险, 且未加 --force-with-money"
        err "若仍要继续: 加 --force-with-money flag"
        err "若要中止: 人工 cancel OKX 挂单 / close 仓位 后再跑"
        exit 3
    fi
}

# ─────────────────────────────────────────────────────────────
# Phase 1 — App graceful shutdown
# ─────────────────────────────────────────────────────────────

run_phase_1() {
    step "Phase 1  App-layer 逆流 graceful shutdown"
    for name in "${APP_CONTAINERS[@]}"; do
        stop_container "$name" "$TIMEOUT_APP_LAYER"
        sleep 2
    done
}

# ─────────────────────────────────────────────────────────────
# Phase 2 — Infra shutdown
# ─────────────────────────────────────────────────────────────

run_phase_2() {
    step "Phase 2  Infra shutdown"

    # 2.1 Early infra
    for name in "${INFRA_CONTAINERS_EARLY[@]}"; do
        stop_container "$name" 5
        sleep 1
    done

    # 2.2 Redis BGSAVE 前置
    if container_running "aats-redis"; then
        if [[ ${DRY_RUN} -eq 1 ]]; then
            log "  [DRY-RUN] docker exec aats-redis redis-cli BGSAVE"
        else
            log "  Redis BGSAVE..."
            wsl_docker exec aats-redis redis-cli BGSAVE >/dev/null 2>&1 || warn "redis BGSAVE 失败"
            sleep 3
        fi
    fi

    # 2.3 Redis / NATS
    for name in "${INFRA_CONTAINERS_DATA[@]}"; do
        stop_container "$name" 10
        sleep 1
    done

    # 2.4 Postgres 最后
    if [[ ${PRESERVE_POSTGRES} -eq 1 ]]; then
        log "  --preserve-postgres: 保留 $POSTGRES_CONTAINER 运行"
        return 0
    fi

    if container_running "$POSTGRES_CONTAINER"; then
        if [[ ${DRY_RUN} -eq 1 ]]; then
            log "  [DRY-RUN] Postgres graceful stop (pg_ctl fast, timeout=${TIMEOUT_POSTGRES}s)"
        else
            log "  Postgres graceful stop (pg_ctl fast, timeout=${TIMEOUT_POSTGRES}s)"
            # pg_ctl fast 模式: 等 active txn 完成
            wsl_docker exec "$POSTGRES_CONTAINER" su -c "pg_ctl stop -m fast -w -t $TIMEOUT_POSTGRES -D /var/lib/postgresql/data" postgres >/dev/null 2>&1 || warn "Postgres pg_ctl stop 失败, 继续 docker stop"
        fi
        stop_container "$POSTGRES_CONTAINER" "$TIMEOUT_POSTGRES"
    fi
}

# ─────────────────────────────────────────────────────────────
# Phase 3 — 验证 + snapshot 报告
# ─────────────────────────────────────────────────────────────

run_phase_3() {
    step "Phase 3  验证 + snapshot"

    local still_running
    still_running=$(wsl_docker ps --format '{{.Names}}' 2>/dev/null | grep '^aats-' || true)
    local still_count=$(echo "$still_running" | grep -c '.' || true)

    if [[ -n "$still_running" ]]; then
        if [[ ${PRESERVE_POSTGRES} -eq 1 && "$still_running" == "$POSTGRES_CONTAINER" ]]; then
            log "  只有 $POSTGRES_CONTAINER 仍运行 (符合 --preserve-postgres)"
        else
            warn "仍在 running 的容器: $still_running"
        fi
    else
        log "  ✓ 所有 aats-* 容器已停"
    fi

    # 写 snapshot
    mkdir -p "$SNAPSHOT_DIR" 2>/dev/null || true
    local force_kill_json="[]"
    local failed_json="[]"
    local warnings_json="[]"
    if [[ ${#FORCE_KILL_LIST[@]} -gt 0 ]]; then
        force_kill_json='["'$(IFS='","'; echo "${FORCE_KILL_LIST[*]}")'"]'
    fi
    if [[ ${#FAILED_STOP_LIST[@]} -gt 0 ]]; then
        failed_json='["'$(IFS='","'; echo "${FAILED_STOP_LIST[*]}")'"]'
    fi
    if [[ ${#WARNINGS[@]} -gt 0 ]]; then
        # Escape quotes in warnings
        local w_escaped=()
        for w in "${WARNINGS[@]}"; do
            w_escaped+=("${w//\"/\\\"}")
        done
        warnings_json='["'$(IFS='","'; echo "${w_escaped[*]}")'"]'
    fi

    cat > "$SNAPSHOT_FILE" 2>/dev/null <<EOF
$preflight_json,
"phase1_2_result": {
    "still_running_after": "$still_running",
    "still_running_count": $still_count,
    "force_kill_list": $force_kill_json,
    "failed_stop_list": $failed_json,
    "warnings": $warnings_json
}
}
EOF

    # 上面 JSON 拼接粗糙, 修正为合法 JSON
    python3 <<PYEOF 2>/dev/null || log "  (snapshot JSON 手工拼接失败, 见 stdout)"
import json, os
head = '''$preflight_json'''
tail = {
    "phase1_2_result": {
        "still_running_after": """$still_running""".strip(),
        "still_running_count": $still_count,
        "force_kill_list": ${force_kill_json},
        "failed_stop_list": ${failed_json},
        "warnings": ${warnings_json}
    }
}
try:
    d = json.loads(head)
    d.update(tail)
    path = "$SNAPSHOT_FILE"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    print(f"  ✓ Snapshot: {path}")
except Exception as e:
    print(f"  WARN snapshot failed: {e}")
PYEOF

    log ""
    log "════════════════════════════════════════"
    log "AATS 停机完成"
    log "════════════════════════════════════════"
    log "Reason         : $REASON"
    log "Dry-run        : $(if [[ $DRY_RUN -eq 1 ]]; then echo yes; else echo no; fi)"
    log "Force-killed   : ${#FORCE_KILL_LIST[@]} 个 (${FORCE_KILL_LIST[*]:-无})"
    log "Failed-stop    : ${#FAILED_STOP_LIST[@]} 个 (${FAILED_STOP_LIST[*]:-无})"
    log "Warnings       : ${#WARNINGS[@]} 条"
    log "Snapshot       : $SNAPSHOT_FILE"
    log "重启命令       : bash scripts/deploy.sh --skip-commit"
    log ""
}

# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

if [[ ${DRY_RUN} -eq 1 ]]; then
    log "*** DRY-RUN 模式 — 不实际停容器. 加 --apply --confirm 真执行 ***"
fi
log "Reason: $REASON"
log "WSL distro: $WSL_DISTRO"

if [[ ${SKIP_PREFLIGHT} -eq 0 ]]; then
    run_preflight
else
    warn "跳过 preflight (--skip-preflight)"
fi

run_phase_1
run_phase_2
run_phase_3

# 退出码
if [[ ${#FAILED_STOP_LIST[@]} -gt 0 ]]; then
    exit 2
fi
if [[ ${#FORCE_KILL_LIST[@]} -gt 0 ]]; then
    exit 1
fi
exit 0
