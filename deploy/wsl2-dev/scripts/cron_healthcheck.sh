#!/usr/bin/env bash
# =============================================================================
# AATS cron 定时任务健康检查
#
# 每次执行输出一段简报,包含:
#   - cron 服务状态
#   - Docker 容器状态 (postgres / nats / redis)
#   - 5 条定时任务各自: 日志最后修改时间 + 距今多久 + 最近一次结果
#   - 最新 workflow_runs/*.json 报告的 overall_status
#
# 设计为 cron 定期调用, append 到单一日志文件, 用户只需 tail 这一个文件。
#
# 用法:
#   ./deploy/wsl2-dev/scripts/cron_healthcheck.sh
#
# cron 示例 (每小时):
#   0 * * * * cd ~/aats && ./deploy/wsl2-dev/scripts/cron_healthcheck.sh \
#       >> logs/rdp/healthcheck.log 2>&1
# =============================================================================

set -uo pipefail

AATS_ROOT="${AATS_ROOT:-/home/arthur/aats}"
LOG_DIR="${AATS_ROOT}/logs/rdp"
REPORT_DIR="${AATS_ROOT}/artifacts/operations/workflow_runs"

# ── 颜色 (日志文件里不需要,但 tty 友好) ──────────────────────────────
if [ -t 1 ]; then
    GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; NC='\033[0m'
else
    GREEN=''; RED=''; YELLOW=''; NC=''
fi

NOW=$(date +%s)
SEPARATOR="────────────────────────────────────────────────────────"

log() { echo "[$(date '+%F %T %Z')] $*"; }
ok()   { echo -e "  ${GREEN}[OK]${NC}   $*"; }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $*"; }
fail() { echo -e "  ${RED}[FAIL]${NC} $*"; }

# ── 时间距离可读化 ────────────────────────────────────────────────────
human_age() {
    local secs=$1
    if [ "$secs" -lt 60 ]; then
        echo "${secs}s"
    elif [ "$secs" -lt 3600 ]; then
        echo "$((secs / 60))m"
    elif [ "$secs" -lt 86400 ]; then
        echo "$((secs / 3600))h$((secs % 3600 / 60))m"
    else
        echo "$((secs / 86400))d$((secs % 86400 / 3600))h"
    fi
}

# ── 检查单个日志文件 ──────────────────────────────────────────────────
# 参数: $1=任务名  $2=日志文件路径  $3=最大允许间隔(秒)
check_log() {
    local name="$1" logfile="$2" max_age="$3"

    if [ ! -f "$logfile" ]; then
        warn "$name — 日志文件不存在 (尚未执行过)"
        return
    fi

    local mtime age age_str last_line
    mtime=$(stat -c %Y "$logfile" 2>/dev/null || echo 0)
    age=$((NOW - mtime))
    age_str=$(human_age $age)

    # 取最后一行非空内容
    last_line=$(tail -20 "$logfile" | grep -v '^$' | tail -1)

    if [ "$age" -gt "$max_age" ]; then
        warn "$name — 最后更新 ${age_str} 前 (超过阈值 $(human_age $max_age))  最后: ${last_line:0:100}"
    else
        ok "$name — 最后更新 ${age_str} 前  最后: ${last_line:0:100}"
    fi
}

# ── 检查最新 workflow 执行报告 ────────────────────────────────────────
check_latest_report() {
    local name="$1"

    if [ ! -d "$REPORT_DIR" ]; then
        return
    fi

    # 找到该 workflow 最新的报告
    local latest
    latest=$(ls -t "$REPORT_DIR"/run_*.json 2>/dev/null | head -20 | while read -r f; do
        if grep -q "\"workflow\": \"$name\"" "$f" 2>/dev/null; then
            echo "$f"
            break
        fi
    done)

    if [ -z "$latest" ]; then
        return  # 没有报告, 不额外输出
    fi

    # 用 python 解析 JSON (WSL2 上一定有 python3)
    local status succeeded failed
    status=$(python3 -c "import json,sys; d=json.load(open('$latest')); print(d.get('overall_status','?'))" 2>/dev/null || echo "?")
    succeeded=$(python3 -c "import json,sys; d=json.load(open('$latest')); print(d.get('succeeded',0))" 2>/dev/null || echo "?")
    failed=$(python3 -c "import json,sys; d=json.load(open('$latest')); print(d.get('failed',0))" 2>/dev/null || echo "?")

    local report_name
    report_name=$(basename "$latest")

    case "$status" in
        success)  ok "  └─ 最新报告: ${report_name}  status=${status}  ok=${succeeded} fail=${failed}" ;;
        partial)  warn "  └─ 最新报告: ${report_name}  status=${status}  ok=${succeeded} fail=${failed}" ;;
        *)        fail "  └─ 最新报告: ${report_name}  status=${status}  ok=${succeeded} fail=${failed}" ;;
    esac
}

# ═════════════════════════════════════════════════════════════════════════
# 主逻辑
# ═════════════════════════════════════════════════════════════════════════

echo ""
echo "$SEPARATOR"
log "AATS cron 健康检查"
echo "$SEPARATOR"

# ── 1. cron 服务 ──
echo ""
log "[基础设施]"
if systemctl is-active cron >/dev/null 2>&1; then
    ok "cron 服务 active"
else
    fail "cron 服务未运行!"
fi

# ── 2. Docker 关键容器 ──
for container in aats-postgres aats-nats aats-redis; do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${container}$"; then
        ok "容器 ${container} running"
    else
        fail "容器 ${container} 未运行!"
    fi
done

# ── 3. 五条定时任务日志检查 ──
echo ""
log "[定时任务]"

#                     任务名              日志文件                             最大间隔(秒)
check_log "data_maintenance" "${LOG_DIR}/data_maintenance.log"  90000   # 25h (每天 1 次)
check_latest_report "data_maintenance"

check_log "governance_cycle" "${LOG_DIR}/governance_cycle.log"  90000   # 25h
check_latest_report "governance_cycle"

check_log "research_cycle"   "${LOG_DIR}/research_cycle.log"    640000  # ~7.5d (每周 1 次)
check_latest_report "research_cycle"

check_log "decision_cycle"   "${LOG_DIR}/decision_cycle.log"    640000  # ~7.5d
check_latest_report "decision_cycle"

check_log "backup_postgres"  "${LOG_DIR}/backup.log"            2400    # 40min (每 30min 1 次)

# ── 4. 磁盘 / 备份 ──
echo ""
log "[磁盘]"

backup_dir="${AATS_ROOT}/backups/wsl2-postgres"
if [ -d "$backup_dir" ]; then
    backup_count=$(find "$backup_dir" -name 'aats_*.dump' -type f 2>/dev/null | wc -l)
    latest_backup=$(ls -t "$backup_dir"/aats_*.dump 2>/dev/null | head -1)
    if [ -n "$latest_backup" ]; then
        bk_size=$(du -h "$latest_backup" | cut -f1)
        bk_mtime=$(stat -c %Y "$latest_backup")
        bk_age=$(human_age $((NOW - bk_mtime)))
        ok "Postgres 备份: ${backup_count} 个, 最新 ${bk_age} 前 (${bk_size})"
    else
        warn "Postgres 备份目录存在但无 dump 文件"
    fi
else
    warn "Postgres 备份目录不存在 (${backup_dir})"
fi

# 日志目录大小
if [ -d "$LOG_DIR" ]; then
    log_size=$(du -sh "$LOG_DIR" 2>/dev/null | cut -f1)
    ok "日志目录: ${log_size}"
fi

echo "$SEPARATOR"
log "检查完毕"
echo ""
