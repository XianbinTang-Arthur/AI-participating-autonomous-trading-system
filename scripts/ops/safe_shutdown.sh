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

# 2026-04-20 code review C-M2: --skip-preflight 可一步绕过全部资金安全
# guardrail. 加 double-token: 必须配环境变量 AATS_I_KNOW_SKIP_PREFLIGHT_IS_DANGEROUS=true
# 才真正生效. 否则报错提示 operator 自己手动确认.
# 理由: 单 flag 易被脚本/自动化 blindly pass, 要求一个显式 env 确保**人为**
# 输入每次都 explicit 确认; env 不太可能被打包到 cron / automation.
if [[ ${SKIP_PREFLIGHT} -eq 1 ]]; then
    if [[ "${AATS_I_KNOW_SKIP_PREFLIGHT_IS_DANGEROUS:-}" != "true" ]]; then
        echo "ERROR: --skip-preflight 需要显式 AATS_I_KNOW_SKIP_PREFLIGHT_IS_DANGEROUS=true env." >&2
        echo "       该 env 防止自动化脚本 blindly 绕过资金安全 preflight." >&2
        echo "       正确用法: AATS_I_KNOW_SKIP_PREFLIGHT_IS_DANGEROUS=true bash $0 --apply --confirm --skip-preflight --reason 'emergency'" >&2
        exit 4
    fi
    # 若 --skip-preflight 和 --force-with-money 同时出现, 也需要明示理由.
    if [[ ${FORCE_WITH_MONEY} -eq 1 && -z "${REASON// /}" ]]; then
        echo "ERROR: --skip-preflight + --force-with-money 组合必须显式 --reason '<具体理由>'" >&2
        exit 4
    fi
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

    # 导出字段给 Phase 3 的 Python snapshot writer.
    # 不在这里拼 JSON — 避免 bash heredoc 与 Python 双写冲突 (本 session 2026-04-20 P1-1 fix).
    export _PFL_PG_OK="$pg_ok"
    export _PFL_RUNNING_COUNT="$running_count"
    export _PFL_OPEN_ORDERS="$open_orders"
    export _PFL_POSITIONS_COUNT="$positions_count"
    export _PFL_POSITION_NOTIONAL_USD="$position_notional_usd"
    export _PFL_IN_FLIGHT_ORDERS="$in_flight_orders"
    export _PFL_RECENT_NON_HOLD="$recent_non_hold"
    export _PFL_RUNTIME_MODE="$runtime_mode"
    export _PFL_ANY_MONEY="$any_money"

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
        stop_postgres_gracefully
    fi
}

# Postgres 优雅停机 (本 session 2026-04-20 P2-3 fix):
#   旧做法: docker exec ... su -c "pg_ctl stop ..." postgres   ❌
#     问题: 官方 postgres 镜像用 gosu, 没有 su 可用; 命令直接失败
#           导致走 fallback docker stop (SIGTERM → smart shutdown 等 client 断开, 慢).
#   新做法: docker stop --signal=SIGINT --time=$TIMEOUT_POSTGRES
#     PostgreSQL 信号约定:
#       SIGTERM  smart   等所有 active session 退出 (可能卡到 timeout → SIGKILL)
#       SIGINT   fast    等当前 txn 结束, 主动踢 idle session, 安全回滚
#       SIGQUIT  immed   强制退出, WAL 可能不一致
#     SIGINT 是正解: 等当前 txn 但不等 idle 连接; 落 WAL 干净.
#     docker stop 的 --time 超时后自动 SIGKILL, 所以无需我们自己 fallback kill.
stop_postgres_gracefully() {
    if [[ ${DRY_RUN} -eq 1 ]]; then
        log "  [DRY-RUN] docker stop --signal=SIGINT --time=${TIMEOUT_POSTGRES} $POSTGRES_CONTAINER"
        return 0
    fi
    log "  Postgres graceful stop (SIGINT=fast, timeout=${TIMEOUT_POSTGRES}s)"
    if wsl_docker stop --signal=SIGINT --time="$TIMEOUT_POSTGRES" "$POSTGRES_CONTAINER" >/dev/null 2>&1; then
        log "    ✓ $POSTGRES_CONTAINER stopped gracefully (SIGINT fast shutdown)"
        return 0
    fi
    warn "$POSTGRES_CONTAINER SIGINT 停机超时 (${TIMEOUT_POSTGRES}s), 升级 kill"
    if wsl_docker kill "$POSTGRES_CONTAINER" >/dev/null 2>&1; then
        FORCE_KILL_LIST+=("$POSTGRES_CONTAINER")
        return 0
    fi
    FAILED_STOP_LIST+=("$POSTGRES_CONTAINER")
    err "$POSTGRES_CONTAINER kill 也失败, 数据层可能有风险, 需人工检查 WAL"
    return 1
}

# ─────────────────────────────────────────────────────────────
# Phase 3 — 验证 + snapshot 报告
# ─────────────────────────────────────────────────────────────

run_phase_3() {
    step "Phase 3  验证 + snapshot"

    local still_running
    still_running=$(wsl_docker ps --format '{{.Names}}' 2>/dev/null | grep '^aats-' || true)
    local still_count
    still_count=$(printf '%s\n' "$still_running" | grep -c '.' || true)

    if [[ -n "$still_running" ]]; then
        if [[ ${PRESERVE_POSTGRES} -eq 1 && "$still_running" == "$POSTGRES_CONTAINER" ]]; then
            log "  只有 $POSTGRES_CONTAINER 仍运行 (符合 --preserve-postgres)"
        else
            warn "仍在 running 的容器: $still_running"
        fi
    else
        log "  ✓ 所有 aats-* 容器已停"
    fi

    mkdir -p "$SNAPSHOT_DIR" 2>/dev/null || true

    # 本 session 2026-04-20 P1-1 fix:
    #   旧做法: 所有字段通过 export 传给 Python.
    #   问题 1: Windows git-bash 把 bash UTF-8 字符串在 export 时走 ANSI code page (GBK),
    #           python.exe 拿到已经 mojibake 的字节, 写入 JSON 仍是 mojibake.
    #   问题 2: python3 在 Windows git-bash PATH 里叫 python, 找不到会 fallback 失败.
    #
    #   新做法: bash 把字段逐行写入 UTF-8 临时文件 (bash printf 是字节透明),
    #           Python 打开文件时用 encoding='utf-8' → 一次编码, 不经过 env ACP 转换.
    local scratch="$SNAPSHOT_DIR/.scratch_${SESSION_TS}.txt"
    {
        printf 'SNAP_FILE=%s\n'           "$SNAPSHOT_FILE"
        printf 'SESSION_TS=%s\n'          "$NOW_UTC"
        printf 'VERSION=%s\n'             "$SCRIPT_VERSION"
        printf 'REASON=%s\n'              "$REASON"
        printf 'DRY_RUN=%s\n'             "$DRY_RUN"
        printf 'FORCE_WITH_MONEY=%s\n'    "$FORCE_WITH_MONEY"
        printf 'SKIP_PREFLIGHT=%s\n'      "$SKIP_PREFLIGHT"
        printf 'PRESERVE_POSTGRES=%s\n'   "$PRESERVE_POSTGRES"
        printf 'STILL_RUNNING=%s\n'       "$still_running"
        printf 'STILL_COUNT=%s\n'         "$still_count"
        printf 'PFL_PG_OK=%s\n'           "${_PFL_PG_OK:-0}"
        printf 'PFL_RUNNING_COUNT=%s\n'   "${_PFL_RUNNING_COUNT:-0}"
        printf 'PFL_OPEN_ORDERS=%s\n'     "${_PFL_OPEN_ORDERS:-0}"
        printf 'PFL_POSITIONS_COUNT=%s\n' "${_PFL_POSITIONS_COUNT:-0}"
        printf 'PFL_POSITION_NOTIONAL_USD=%s\n' "${_PFL_POSITION_NOTIONAL_USD:-0.00}"
        printf 'PFL_IN_FLIGHT_ORDERS=%s\n' "${_PFL_IN_FLIGHT_ORDERS:-0}"
        printf 'PFL_RECENT_NON_HOLD=%s\n' "${_PFL_RECENT_NON_HOLD:-0}"
        printf 'PFL_RUNTIME_MODE=%s\n'    "${_PFL_RUNTIME_MODE:-unknown}"
        printf 'PFL_ANY_MONEY=%s\n'       "${_PFL_ANY_MONEY:-0}"
        for item in "${FORCE_KILL_LIST[@]:-}"; do
            [[ -z "$item" ]] && continue
            printf 'FORCE_KILL_ITEM=%s\n' "$item"
        done
        for item in "${FAILED_STOP_LIST[@]:-}"; do
            [[ -z "$item" ]] && continue
            printf 'FAILED_STOP_ITEM=%s\n' "$item"
        done
        for item in "${WARNINGS[@]:-}"; do
            [[ -z "$item" ]] && continue
            printf 'WARNING_ITEM=%s\n' "$item"
        done
    } > "$scratch"

    # Python 解析器自动探测 (Windows git-bash: 'python'; WSL2/Linux: 'python3')
    local py_exe=""
    for candidate in python3 python py; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if [[ "$candidate" == "py" ]]; then
                py_exe="py -3"
            else
                py_exe="$candidate"
            fi
            break
        fi
    done
    if [[ -z "$py_exe" ]]; then
        warn "未找到 python/python3, snapshot 仅 stdout 打印"
        cat "$scratch"
        rm -f "$scratch"
        return 0
    fi

    $py_exe - "$scratch" <<'PYEOF'
import json, os, sys

scratch = sys.argv[1]
data = {}
arrays = {"FORCE_KILL_ITEM": [], "FAILED_STOP_ITEM": [], "WARNING_ITEM": []}

with open(scratch, "r", encoding="utf-8") as f:
    for line in f:
        line = line.rstrip("\r\n")
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k in arrays:
            arrays[k].append(v)
        else:
            data[k] = v

def i(k, d=0):
    try:
        return int(data.get(k, d) or d)
    except ValueError:
        return d

snap = {
    "session_ts": data.get("SESSION_TS", ""),
    "script_version": data.get("VERSION", ""),
    "reason": data.get("REASON", ""),
    "dry_run": i("DRY_RUN"),
    "force_with_money": i("FORCE_WITH_MONEY"),
    "skip_preflight": i("SKIP_PREFLIGHT"),
    "preserve_postgres": i("PRESERVE_POSTGRES"),
    "preflight": {
        "postgres_reachable": i("PFL_PG_OK"),
        "running_containers": i("PFL_RUNNING_COUNT"),
        "open_orders": i("PFL_OPEN_ORDERS"),
        "positions_count": i("PFL_POSITIONS_COUNT"),
        "position_notional_usd": data.get("PFL_POSITION_NOTIONAL_USD", "0.00"),
        "in_flight_orders": i("PFL_IN_FLIGHT_ORDERS"),
        "recent_non_hold_5m": i("PFL_RECENT_NON_HOLD"),
        "ai_operating_mode": data.get("PFL_RUNTIME_MODE", "unknown"),
        "any_money_at_risk": i("PFL_ANY_MONEY"),
    },
    "phase1_2_result": {
        "still_running_after": data.get("STILL_RUNNING", "").strip(),
        "still_running_count": i("STILL_COUNT"),
        "force_kill_list": arrays["FORCE_KILL_ITEM"],
        "failed_stop_list": arrays["FAILED_STOP_ITEM"],
        "warnings": arrays["WARNING_ITEM"],
    },
}

path = data.get("SNAP_FILE", "")
if not path:
    print("  WARN: SNAP_FILE missing, dumping snapshot to stdout")
    print(json.dumps(snap, indent=2, ensure_ascii=False))
    sys.exit(0)
try:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2, ensure_ascii=False)
    print(f"  Snapshot: {path}")
except Exception as exc:
    print(f"  WARN snapshot write failed: {exc}")
    print(json.dumps(snap, indent=2, ensure_ascii=False))
PYEOF
    local snap_rc=$?
    rm -f "$scratch"
    if [[ $snap_rc -ne 0 ]]; then
        warn "Snapshot Python writer 异常退出 ($snap_rc)"
    fi

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
