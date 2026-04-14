#!/usr/bin/env bash
# =============================================================================
# AATS 标准化部署脚本
#
# 一条命令完成：
#   代码提交 -> 同步到 WSL2 -> docker compose build/up -> 健康检查 -> 部署报告
#
# 用法：
#   ./scripts/deploy.sh
#   ./scripts/deploy.sh --profile spot-live
#   ./scripts/deploy.sh --commit "修复策略页布局"
#   ./scripts/deploy.sh --no-cache
#   ./scripts/deploy.sh --skip-sync
#   ./scripts/deploy.sh --skip-commit
#
# 说明：
#   - 默认 profile 为 derivatives-live
#   - 未提交改动不会被同步到 WSL2；如需部署当前 Windows 工作区，请先提交
#   - --skip-sync 只会部署 WSL2 侧当前 checkout，不会带上 Windows 新改动
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${CYAN}[deploy]${NC} $*"; }
log_ok()    { echo -e "${GREEN}[deploy]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[deploy]${NC} $*"; }
log_error() { echo -e "${RED}[deploy]${NC} $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_DIR="deploy/wsl2-dev"

DISTRO="${AATS_WSL2_DISTRO:-Ubuntu}"
WSL_PROJECT="${AATS_WSL2_PROJECT:-\$HOME/aats}"

PROFILE="derivatives-live"
COMMIT_MSG=""
NO_CACHE=""
SKIP_SYNC=false
SKIP_COMMIT=false
HEALTH_TIMEOUT=90

COMPOSE_OVERLAY=""
ENV_PROFILE=""
ENV_PROFILE_PATH=""
WSL2_ENV_FILE=""
APP_CONTAINERS=""
COMPOSE_CMD_ARGS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile|-p)
            PROFILE="$2"; shift 2 ;;
        --commit|-m)
            COMMIT_MSG="$2"; shift 2 ;;
        --no-cache)
            NO_CACHE="--no-cache"; shift ;;
        --skip-sync)
            SKIP_SYNC=true; shift ;;
        --skip-commit)
            SKIP_COMMIT=true; shift ;;
        --timeout)
            HEALTH_TIMEOUT="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,25p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            log_error "未知参数: $1"
            exit 1
            ;;
    esac
done

wsl_run() {
    wsl -d "$DISTRO" bash -c "$1"
}

repo_has_uncommitted_changes() {
    ! git diff --quiet || ! git diff --cached --quiet || [[ -n "$(git ls-files --others --exclude-standard)" ]]
}

required_app_containers_for_profile() {
    local profile="$1"
    case "$profile" in
        derivatives-live-monolith)
            echo "aats-gateway aats-rdp-daemon"
            ;;
        spot|spot-live|derivatives|derivatives-live)
            echo "aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon"
            ;;
        *)
            log_error "不支持的 profile: $profile"
            exit 1
            ;;
    esac
}

resolve_profile() {
    case "$1" in
        spot)
            COMPOSE_OVERLAY="docker-compose.aats.spot.yml"
            ENV_PROFILE="../../.env.spot"
            ;;
        spot-live)
            COMPOSE_OVERLAY="docker-compose.aats.spot-live.yml"
            ENV_PROFILE="../../.env.spot.live"
            ;;
        derivatives)
            COMPOSE_OVERLAY="docker-compose.aats.derivatives.yml"
            ENV_PROFILE="../../.env.derivatives"
            ;;
        derivatives-live)
            COMPOSE_OVERLAY="docker-compose.aats.derivatives-live.yml"
            ENV_PROFILE="../../.env.derivatives.live"
            ;;
        derivatives-live-monolith)
            COMPOSE_OVERLAY="docker-compose.aats.derivatives-live-monolith.yml"
            ENV_PROFILE="../../.env.derivatives.live"
            ;;
        *)
            log_error "不支持的 profile: $1"
            log_error "可选: spot | spot-live | derivatives | derivatives-live | derivatives-live-monolith"
            exit 1
            ;;
    esac

    APP_CONTAINERS="$(required_app_containers_for_profile "$1")"
    COMPOSE_CMD_ARGS="-f docker-compose.yml -f docker-compose.aats.yml -f $COMPOSE_OVERLAY"
}

resolve_wsl2_env_file() {
    if wsl_run "test -f $WSL_PROJECT/.env.wsl2"; then
        WSL2_ENV_FILE="$WSL_PROJECT/.env.wsl2"
    elif wsl_run "test -f $WSL_PROJECT/$DEPLOY_DIR/.env.wsl2"; then
        WSL2_ENV_FILE="$WSL_PROJECT/$DEPLOY_DIR/.env.wsl2"
        log_warn "检测到 legacy .env.wsl2 位置: $WSL2_ENV_FILE；建议迁移到仓库根目录"
    else
        log_error "WSL2 侧缺失 .env.wsl2"
        log_error "优先位置: $WSL_PROJECT/.env.wsl2"
        log_error "兼容旧位置: $WSL_PROJECT/$DEPLOY_DIR/.env.wsl2"
        log_error "请先: cp configs/templates/.env.wsl2.example .env.wsl2 并修改密码"
        exit 1
    fi
}

all_required_app_containers_healthy() {
    local c
    local state

    for c in $APP_CONTAINERS; do
        state="$(wsl_run "docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \"$c\" 2>/dev/null" || true)"
        [[ "$state" == "running healthy" ]] || return 1
    done

    return 0
}

print_required_app_container_states() {
    local c
    local state

    for c in $APP_CONTAINERS; do
        state="$(wsl_run "docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \"$c\" 2>/dev/null" || true)"
        if [[ -n "$state" ]]; then
            printf '%s %s\n' "$c" "$state"
        else
            printf '%s missing\n' "$c"
        fi
    done
}

preflight() {
    log_info "Profile:  $PROFILE"
    log_info "Overlay:  $COMPOSE_OVERLAY"
    log_info "Env:      $ENV_PROFILE"
    echo

    if ! command -v wsl >/dev/null 2>&1; then
        log_error "找不到 wsl 命令，本脚本需要 Windows + WSL2 环境"
        exit 1
    fi

    if ! wsl_run "test -d $WSL_PROJECT/.git"; then
        log_error "WSL2 项目目录 $WSL_PROJECT 不存在，请先运行: ./scripts/sync_to_wsl2.sh init"
        exit 1
    fi

    resolve_wsl2_env_file

    ENV_PROFILE_PATH="$WSL_PROJECT/${ENV_PROFILE#../../}"
    if ! wsl_run "test -f $ENV_PROFILE_PATH"; then
        log_error "WSL2 侧缺失 profile env 文件: $ENV_PROFILE_PATH"
        exit 1
    fi

    if ! wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && test -f $COMPOSE_OVERLAY"; then
        log_error "WSL2 侧缺失 compose overlay: $COMPOSE_OVERLAY"
        exit 1
    fi

    COMPOSE_CMD_ARGS="$COMPOSE_CMD_ARGS --env-file $WSL2_ENV_FILE --env-file $ENV_PROFILE_PATH"
}

step_commit() {
    cd "$PROJECT_ROOT"

    if [[ -n "$COMMIT_MSG" ]]; then
        log_info "Step 1/7: 提交代码..."
        if repo_has_uncommitted_changes; then
            git add -A
            git commit -m "$COMMIT_MSG"
            log_ok "已提交: $(git log --oneline -1)"
        else
            log_warn "工作区干净，无需提交"
        fi
    elif [[ "$SKIP_COMMIT" == true ]]; then
        log_info "Step 1/7: 跳过提交（--skip-commit）"
    fi

    if repo_has_uncommitted_changes; then
        if [[ "$SKIP_SYNC" == true ]]; then
            log_warn "检测到未提交改动，且 --skip-sync 已开启；本次部署不会同步这些 Windows 改动"
            log_warn "以下文件有改动："
            git status --short
            echo
            read -r -p "继续部署 WSL2 侧现有代码？[y/N] " confirm
            if [[ "$confirm" != [yY] ]]; then
                log_info "已取消"
                exit 0
            fi
        else
            log_error "检测到未提交改动；当前同步机制只会部署已提交的 Git HEAD，无法携带工作区改动"
            log_error "请先手动提交，或使用 --commit \"msg\" 自动提交后再部署"
            exit 1
        fi
    fi
}

step_sync() {
    if [[ "$SKIP_SYNC" == true ]]; then
        log_info "Step 2/7: 跳过同步（--skip-sync）"
        return
    fi

    log_info "Step 2/7: 同步代码到 WSL2..."
    "$SCRIPT_DIR/sync_to_wsl2.sh" pull
    log_ok "同步完成"
}

step_down() {
    log_info "Step 3/7: 停止旧服务..."
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && docker compose $COMPOSE_CMD_ARGS down --timeout 5" || {
        log_warn "docker compose down 返回非零，可能当前没有运行中的服务，继续"
    }
    log_ok "旧服务已停止"
}

step_build() {
    log_info "Step 4/7: 构建新镜像${NO_CACHE:+（无缓存）}..."
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && docker compose $COMPOSE_CMD_ARGS build $NO_CACHE"
    log_ok "镜像构建完成"
}

step_prune() {
    log_info "Step 5/7: 清理悬空镜像..."
    local pruned
    pruned=$(wsl_run "docker image prune -f 2>/dev/null" || true)
    if echo "$pruned" | grep -q "Total reclaimed space: 0B"; then
        log_info "无悬空镜像需要清理"
    else
        log_ok "清理完成"
    fi
}

step_infra_up() {
    log_info "Step 6/7: 启动基础设施（Postgres/Redis/NATS/...）..."
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && docker compose -f docker-compose.yml --env-file $WSL2_ENV_FILE up -d"

    local elapsed=0
    while [[ $elapsed -lt 30 ]]; do
        if wsl_run "docker exec aats-postgres pg_isready -q 2>/dev/null"; then
            break
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done

    wsl -d "$DISTRO" bash <<PWEOF
cd $WSL_PROJECT
PG_USER=\$(grep '^POSTGRES_USER=' "$WSL2_ENV_FILE" | cut -d= -f2-)
PG_PW=\$(grep '^POSTGRES_PASSWORD=' "$WSL2_ENV_FILE" | cut -d= -f2-)
docker exec aats-postgres psql -U "\$PG_USER" -d aats -c "SET password_encryption = 'scram-sha-256'; ALTER USER \$PG_USER PASSWORD '\$PG_PW';" >/dev/null 2>&1
PWEOF

    log_ok "基础设施就绪，密码已同步"
}

step_app_up() {
    log_info "Step 7/7: 启动应用服务..."
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && docker compose $COMPOSE_CMD_ARGS up -d"
    log_ok "应用服务已启动"
}

step_health() {
    log_info "健康检查（超时 ${HEALTH_TIMEOUT}s）..."

    local port
    port=$(wsl_run "grep -h '^AATS_API_PORT=' \"$ENV_PROFILE_PATH\" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '\"'" || echo "")
    port="${port:-8000}"

    local elapsed=0
    local interval=3
    while [[ $elapsed -lt $HEALTH_TIMEOUT ]]; do
        if wsl_run "curl -sf http://127.0.0.1:$port/healthz >/dev/null 2>&1" && all_required_app_containers_healthy; then
            log_ok "应用健康检查通过 (gateway port $port, containers: $APP_CONTAINERS, ${elapsed}s)"
            return 0
        fi
        sleep "$interval"
        elapsed=$((elapsed + interval))
    done

    log_error "健康检查超时 (${HEALTH_TIMEOUT}s)，应用容器未全部就绪"
    log_error "当前容器状态："
    print_required_app_container_states
    log_error "查看日志: wsl -d $DISTRO bash -c 'cd $WSL_PROJECT/$DEPLOY_DIR && docker compose $COMPOSE_CMD_ARGS logs --tail 50'"
    exit 3
}

report() {
    echo
    echo "=========================================="
    log_ok "部署完成"
    echo "=========================================="
    log_info "Profile:    $PROFILE"
    log_info "Overlay:    $COMPOSE_OVERLAY"
    log_info "Env file:   $WSL2_ENV_FILE"

    echo
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && docker compose $COMPOSE_CMD_ARGS ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'" || true
    echo

    local head
    head=$(cd "$PROJECT_ROOT" && git log --oneline -1)
    log_info "Git HEAD:   $head"
    echo
}

main() {
    echo
    log_info "============ AATS 部署流水线 ============"
    echo

    resolve_profile "$PROFILE"
    preflight

    step_commit
    step_sync
    step_down
    step_build
    step_prune
    step_infra_up
    step_app_up
    step_health
    report
}

main
