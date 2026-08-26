#!/usr/bin/env bash
# =============================================================================
# AATS 标准化部署脚本
#
# 一条命令完成：
#   代码提交 -> 同步到 WSL2 -> docker compose build/up -> 健康检查 -> 部署报告
#
# 用法：
#   ./scripts/deploy.sh --profile spot
#   ./scripts/deploy.sh --profile derivatives
#   ./scripts/deploy.sh --commit "修复策略页布局"
#   ./scripts/deploy.sh --no-cache
#   ./scripts/deploy.sh --skip-sync
#   ./scripts/deploy.sh --skip-commit
#   ./scripts/deploy.sh --yes            # 非交互：--skip-sync 且有未提交改动时默认继续
#
# 说明：
#   - --profile 必填；当前审计 NO-GO 期间，所有 live profile 在副作用前被拒绝
#   - 未提交改动不会被同步到 WSL2；如需部署当前 Windows 工作区，请先提交
#   - --skip-sync 只会部署 WSL2 侧当前 checkout，不会带上 Windows 新改动
#   - --commit 只提交已经精确暂存的文件；不会自动 git add -A
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

PROFILE=""
COMMIT_MSG=""
NO_CACHE=""
SKIP_SYNC=false
SKIP_COMMIT=false
ASSUME_YES=false
HEALTH_TIMEOUT=90

COMPOSE_OVERLAY=""
ENV_PROFILE=""
ENV_PROFILE_PATH=""
WSL2_ENV_FILE=""
APP_CONTAINERS=""
COMPOSE_CMD_ARGS=""
OPERATOR_TLS_ENABLED=false
OPERATOR_HEALTH_SCHEME="http"
OPERATOR_TLS_RUNTIME_DIR=""
OPERATOR_TLS_CERT_WSL=""
OPERATOR_TLS_KEY_WSL=""
OPERATOR_TLS_CERT_CONTAINER=""
OPERATOR_TLS_KEY_CONTAINER=""
DEPLOYMENT_EVIDENCE_PATH=""
RUNTIME_READINESS_GENERATION=""
DEPLOYED_GIT_COMMIT=""

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
        --yes|-y)
            ASSUME_YES=true; shift ;;
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

wsl_root_run() {
    wsl -d "$DISTRO" -u root bash -c "$1"
}

repo_has_uncommitted_changes() {
    ! git diff --quiet || ! git diff --cached --quiet || [[ -n "$(git ls-files --others --exclude-standard)" ]]
}

repo_has_staged_changes() {
    ! git diff --cached --quiet
}

repo_has_unstaged_or_untracked_changes() {
    ! git diff --quiet || [[ -n "$(git ls-files --others --exclude-standard)" ]]
}

windows_head_oneline() {
    cd "$PROJECT_ROOT" && git log --oneline -1
}

wsl_head_oneline() {
    wsl_run "git -C $WSL_PROJECT log --oneline -1 2>/dev/null" | tr -d '\r'
}

windows_head_rev() {
    cd "$PROJECT_ROOT" && git rev-parse HEAD
}

wsl_head_rev() {
    wsl_run "git -C $WSL_PROJECT rev-parse HEAD 2>/dev/null" | tr -d '\r'
}

required_app_containers_for_profile() {
    local profile="$1"
    case "$profile" in
        derivatives-live-monolith)
            echo "aats-gateway aats-rdp-daemon aats-liquidations-daemon aats-microstructure-collector"
            ;;
        derivatives-live)
            echo "aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon aats-liquidations-daemon aats-microstructure-collector"
            ;;
        derivatives)
            echo "aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon aats-liquidations-daemon aats-microstructure-collector"
            ;;
        spot|spot-live)
            echo "aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon"
            ;;
        *)
            log_error "不支持的 profile: $profile"
            exit 1
            ;;
    esac
}

require_explicit_non_live_profile() {
    if [[ -z "$PROFILE" ]]; then
        log_error "必须显式指定 --profile；本地测试使用 spot 或 derivatives"
        log_error "当前真实资金生产结论为 NO-GO，脚本没有默认 profile"
        exit 2
    fi

    if is_live_profile "$PROFILE"; then
        log_error "真实资金 profile '$PROFILE' 已由 FS-007 安全门禁禁用"
        log_error "当前审计结论为 REAL-MONEY PRODUCTION: NO-GO；--yes 不能绕过"
        log_error "请先使用 spot/derivatives 完成本地测试，并关闭全部上线 gate 与未知项"
        exit 5
    fi
}

is_live_profile() {
    case "$1" in
        spot-live|derivatives-live|derivatives-live-monolith)
            return 0
            ;;
        *)
            return 1
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

compose_env_prefix() {
    if [[ -z "$RUNTIME_READINESS_GENERATION" ]]; then
        log_error "runtime readiness generation 尚未生成"
        return 1
    fi
    if [[ ! "$DEPLOYED_GIT_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
        log_error "deployed Git commit 尚未生成或格式无效"
        return 1
    fi
    printf "AATS_RUNTIME_READINESS_GENERATION='%s' AATS_DEPLOYED_GIT_COMMIT='%s'" \
        "$RUNTIME_READINESS_GENERATION" \
        "$DEPLOYED_GIT_COMMIT"
    if [[ "$OPERATOR_TLS_ENABLED" == true ]]; then
        printf " AATS_OPERATOR_TLS_CERT_FILE='%s' AATS_OPERATOR_TLS_KEY_FILE='%s'" \
            "$OPERATOR_TLS_CERT_CONTAINER" \
            "$OPERATOR_TLS_KEY_CONTAINER"
    fi
}

prepare_runtime_readiness_generation() {
    local deployed_rev
    local deployed_short
    local generated_at
    deployed_rev="$(wsl_head_rev)"
    if [[ ! "$deployed_rev" =~ ^[0-9a-fA-F]{40}$ ]]; then
        log_error "无法为 runtime readiness 生成合法 deployed revision"
        exit 1
    fi
    DEPLOYED_GIT_COMMIT="${deployed_rev,,}"
    deployed_short="${deployed_rev:0:12}"
    generated_at="$(date -u +%Y%m%dT%H%M%SZ)"
    RUNTIME_READINESS_GENERATION="${deployed_short}-${generated_at}-$$-${RANDOM}"
    log_info "Runtime readiness generation: $RUNTIME_READINESS_GENERATION"
}

ensure_operator_tls_assets() {
    if ! is_live_profile "$PROFILE"; then
        return
    fi

    if ! wsl_run "command -v openssl >/dev/null 2>&1"; then
        log_error "live profile 需要 openssl 生成 operator HTTPS 证书，请先在 WSL2 中安装 openssl"
        exit 1
    fi

    OPERATOR_TLS_ENABLED=true
    OPERATOR_HEALTH_SCHEME="https"
    OPERATOR_TLS_RUNTIME_DIR="$WSL_PROJECT/$DEPLOY_DIR/runtime/operator-tls/$PROFILE"
    OPERATOR_TLS_CERT_WSL="$OPERATOR_TLS_RUNTIME_DIR/operator.crt"
    OPERATOR_TLS_KEY_WSL="$OPERATOR_TLS_RUNTIME_DIR/operator.key"
    OPERATOR_TLS_CERT_CONTAINER="/app/deploy/wsl2-dev/runtime/operator-tls/$PROFILE/operator.crt"
    OPERATOR_TLS_KEY_CONTAINER="/app/deploy/wsl2-dev/runtime/operator-tls/$PROFILE/operator.key"

    wsl_run "mkdir -p '$OPERATOR_TLS_RUNTIME_DIR'"

    if ! wsl_run "test -f '$OPERATOR_TLS_CERT_WSL' && test -f '$OPERATOR_TLS_KEY_WSL'"; then
        log_info "为 live profile 生成本地 operator HTTPS 证书..."
        wsl_run "openssl req -x509 -nodes -newkey rsa:2048 \
            -keyout '$OPERATOR_TLS_KEY_WSL' \
            -out '$OPERATOR_TLS_CERT_WSL' \
            -days 365 \
            -subj '/CN=localhost' \
            -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' >/dev/null 2>&1"
        wsl_run "chmod 600 '$OPERATOR_TLS_KEY_WSL' && chmod 644 '$OPERATOR_TLS_CERT_WSL'"
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

gateway_health_ok() {
    local port="$1"
    if [[ "$OPERATOR_HEALTH_SCHEME" == "https" ]]; then
        wsl_run "curl -kfs https://127.0.0.1:$port/healthz >/dev/null 2>&1"
    else
        wsl_run "curl -fs http://127.0.0.1:$port/healthz >/dev/null 2>&1"
    fi
}

required_app_container_states_compact() {
    local c
    local state
    local parts=()

    for c in $APP_CONTAINERS; do
        state="$(wsl_run "docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \"$c\" 2>/dev/null" || true)"
        if [[ -n "$state" ]]; then
            parts+=("$c=$state")
        else
            parts+=("$c=missing")
        fi
    done

    local joined=""
    local part
    for part in "${parts[@]}"; do
        if [[ -n "$joined" ]]; then
            joined="$joined, "
        fi
        joined="$joined$part"
    done

    printf '%s\n' "$joined"
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
    ensure_operator_tls_assets

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
        log_info "Step 1/8: 提交代码..."
        if repo_has_staged_changes; then
            if repo_has_unstaged_or_untracked_changes; then
                log_error "--commit 只提交已精确暂存的文件；检测到未暂存或未跟踪改动"
                log_error "请先用 git add <files> 精确暂存本次部署文件，或清理无关改动"
                git status --short
                exit 1
            fi
            git commit -m "$COMMIT_MSG"
            log_ok "已提交: $(git log --oneline -1)"
        elif repo_has_unstaged_or_untracked_changes; then
            log_error "--commit 不再自动执行 git add -A，避免把无关改动发布到 live"
            log_error "请先用 git add <files> 精确暂存本次部署文件后重试"
            git status --short
            exit 1
        else
            log_warn "工作区干净，无需提交"
        fi
    elif [[ "$SKIP_COMMIT" == true ]]; then
        log_info "Step 1/8: 跳过提交（--skip-commit）"
    fi

    if repo_has_uncommitted_changes; then
        if [[ "$SKIP_SYNC" == true ]]; then
            log_warn "检测到未提交改动，且 --skip-sync 已开启；本次部署不会同步这些 Windows 改动"
            log_warn "以下文件有改动："
            git status --short
            echo
            if [[ "$ASSUME_YES" == true ]]; then
                log_info "--yes 已指定，继续部署 WSL2 侧现有代码"
            elif [[ -t 0 ]]; then
                read -r -p "继续部署 WSL2 侧现有代码？[y/N] " confirm
                if [[ "$confirm" != [yY] ]]; then
                    log_info "已取消"
                    exit 0
                fi
            else
                log_error "非交互环境检测到未提交改动；请显式传 --yes 或先提交/同步"
                exit 4
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
        log_info "Step 2/8: 跳过同步（--skip-sync）"
        return
    fi

    log_info "Step 2/8: 同步代码到 WSL2..."
    "$SCRIPT_DIR/sync_to_wsl2.sh" pull
    log_ok "同步完成"
}

step_down() {
    log_info "Step 4/8: 停止旧服务..."
    local env_prefix
    env_prefix="$(compose_env_prefix)"
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && ${env_prefix:+env $env_prefix }docker compose $COMPOSE_CMD_ARGS down --timeout 5"
    log_ok "旧服务已停止"
}

step_build() {
    log_info "Step 3/8: 构建新镜像${NO_CACHE:+（无缓存）}..."
    local env_prefix
    env_prefix="$(compose_env_prefix)"
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && ${env_prefix:+env $env_prefix }docker compose $COMPOSE_CMD_ARGS build $NO_CACHE"
    log_ok "镜像构建完成"
}

step_prune() {
    log_info "Step 5/8: 清理悬空镜像..."
    local pruned
    pruned=$(wsl_run "docker image prune -f 2>/dev/null" || true)
    if echo "$pruned" | grep -q "Total reclaimed space: 0B"; then
        log_info "无悬空镜像需要清理"
    else
        log_ok "清理完成"
    fi
}

ensure_wsl_runtime_prerequisites() {
    log_info "检查 WSL2 宿主运行前置条件..."

    local overcommit
    overcommit="$(wsl_run "sysctl -n vm.overcommit_memory 2>/dev/null" | tr -d '\r')"
    if [[ "$overcommit" != "1" ]]; then
        # Redis fork/BGSAVE 在 vm.overcommit_memory=0 时可能即使内存充足也失败。
        # WSL 重启会重置该运行时值，所以标准部署每次幂等校正，不修改发行版文件。
        wsl_root_run "sysctl -q -w vm.overcommit_memory=1"
        overcommit="$(wsl_run "sysctl -n vm.overcommit_memory 2>/dev/null" | tr -d '\r')"
    fi
    if [[ "$overcommit" != "1" ]]; then
        log_error "无法设置 vm.overcommit_memory=1；Redis 持久化安全前置条件不满足"
        exit 7
    fi

    log_ok "WSL2 运行前置条件满足（vm.overcommit_memory=1）"
}

ensure_rdp_artifact_directory() {
    log_info "准备持久化 RDP artifact 目录..."
    # 应用镜像固定以 UID/GID 1000 的 aats 用户运行。host 侧运维命令可能以
    # root 创建新证据，因此每次标准部署都只对仓库内这个精确目录校正归属，
    # 不触碰数据库卷或仓库之外的路径。
    wsl_root_run "install -d -o 1000 -g 1000 '$WSL_PROJECT/artifacts' && chown -R 1000:1000 '$WSL_PROJECT/artifacts'"
    log_ok "RDP artifact 持久目录已就绪"
}

step_infra_up() {
    log_info "Step 6/8: 启动基础设施（Postgres/Redis/NATS/...）..."
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && docker compose -f docker-compose.yml --env-file $WSL2_ENV_FILE up -d --wait --wait-timeout 90"

    wsl -d "$DISTRO" bash <<PWEOF
set -euo pipefail
cd $WSL_PROJECT
PG_USER=\$(grep '^POSTGRES_USER=' "$WSL2_ENV_FILE" | cut -d= -f2-)
PG_PW=\$(grep '^POSTGRES_PASSWORD=' "$WSL2_ENV_FILE" | cut -d= -f2-)
docker exec -i aats-postgres psql \
    -v ON_ERROR_STOP=1 \
    -v pg_user="\$PG_USER" \
    -v pg_password="\$PG_PW" \
    -U "\$PG_USER" \
    -d aats >/dev/null 2>&1 <<'SQLEOF'
SET password_encryption = 'scram-sha-256';
ALTER USER :"pg_user" PASSWORD :'pg_password';
SQLEOF
PWEOF

    log_ok "基础设施就绪，密码已同步"
}

step_schema_migrate() {
    log_info "Step 7/8: 执行主交易 + RDP schema 迁移与校验..."
    local env_prefix
    env_prefix="$(compose_env_prefix)"
    # 复用 gateway 的合法 managed process_role 与双数据库连接环境；命令覆写后
    # 不会启动 FastAPI，也不会接触交易所。rdp-daemon 的自定义 role 不能传给
    # AATSSettings.load_settings()，因此不能作为此 one-shot job 的宿主。
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && ${env_prefix:+env $env_prefix }docker compose $COMPOSE_CMD_ARGS run --rm --no-deps aats-gateway python scripts/compose_entrypoint.py python scripts/apply_schema_migrations.py"
    log_ok "Schema migration contract 已应用并校验"
}

step_app_up() {
    log_info "Step 8/8: 启动应用服务..."
    local env_prefix
    env_prefix="$(compose_env_prefix)"
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && ${env_prefix:+env $env_prefix }docker compose $COMPOSE_CMD_ARGS up -d"
    log_info "应用服务启动命令已返回，等待健康检查确认"
}

step_health() {
    log_info "健康检查（超时 ${HEALTH_TIMEOUT}s）..."

    local port
    port=$(wsl_run "grep -h '^AATS_API_PORT=' \"$ENV_PROFILE_PATH\" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '\"'" || echo "")
    port="${port:-8000}"

    local elapsed=0
    local interval=3
    local last_progress=""
    while [[ $elapsed -lt $HEALTH_TIMEOUT ]]; do
        local gateway_state="未就绪"
        if gateway_health_ok "$port"; then
            gateway_state="已就绪"
        fi

        if [[ "$gateway_state" == "已就绪" ]] && all_required_app_containers_healthy; then
            log_ok "应用健康检查通过 (gateway port $port, containers: $APP_CONTAINERS, ${elapsed}s)"
            return 0
        fi

        local container_states
        container_states="$(required_app_container_states_compact)"
        local progress="gateway=${gateway_state}; 容器=${container_states}"
        if [[ "$progress" != "$last_progress" || $elapsed -eq 0 || $((elapsed % 15)) -eq 0 ]]; then
            log_info "健康检查进度 ${elapsed}s/${HEALTH_TIMEOUT}s: ${progress}"
            last_progress="$progress"
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

write_deployment_evidence() {
    log_info "写入模拟部署证据包..."

    local required_args=""
    local c
    for c in $APP_CONTAINERS; do
        required_args="$required_args --required-container '$c'"
    done

    DEPLOYMENT_EVIDENCE_PATH="$(wsl_run "cd $WSL_PROJECT && python3 scripts/write_deployment_evidence.py --repo-root $WSL_PROJECT --profile '$PROFILE' --overlay '$COMPOSE_OVERLAY' --schema-job-status passed --runtime-readiness-generation '$RUNTIME_READINESS_GENERATION' $required_args" | tr -d '\r' | tail -1)"
    if [[ -z "$DEPLOYMENT_EVIDENCE_PATH" ]]; then
        log_error "模拟部署证据包路径为空，拒绝报告成功"
        exit 6
    fi
    log_ok "模拟部署证据包已写入: $DEPLOYMENT_EVIDENCE_PATH"
}

report() {
    echo
    echo "=========================================="
    log_ok "模拟栈基础检查通过（不是 trading-ready 或生产放行）"
    echo "=========================================="
    log_info "Profile:    $PROFILE"
    log_info "Overlay:    $COMPOSE_OVERLAY"
    log_info "Env file:   $WSL2_ENV_FILE"
    log_info "Evidence:   $DEPLOYMENT_EVIDENCE_PATH"

    echo
    local env_prefix
    env_prefix="$(compose_env_prefix)"
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && ${env_prefix:+env $env_prefix }docker compose $COMPOSE_CMD_ARGS ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'" || true
    echo

    local win_head
    local win_rev
    local wsl_head
    local wsl_rev
    win_head="$(windows_head_oneline)"
    win_rev="$(windows_head_rev)"
    wsl_head="$(wsl_head_oneline || true)"
    wsl_rev="$(wsl_head_rev || true)"
    log_info "Windows HEAD: $win_head"
    if [[ -n "$wsl_head" ]]; then
        log_info "WSL HEAD:     $wsl_head"
    else
        log_warn "WSL HEAD:     无法读取"
    fi
    if [[ -n "$wsl_rev" && "$wsl_rev" != "$win_rev" ]]; then
        log_warn "Windows HEAD 与 WSL deployed HEAD 不一致；本次报告以 WSL HEAD 为实际部署版本"
    fi
    if [[ "$OPERATOR_TLS_ENABLED" == true ]]; then
        local port
        port=$(wsl_run "grep -h '^AATS_API_PORT=' \"$ENV_PROFILE_PATH\" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '\"'" || echo "")
        port="${port:-8000}"
        log_info "Operator:   https://127.0.0.1:$port (self-signed)"
    fi
    echo
}

main() {
    require_explicit_non_live_profile

    echo
    log_info "============ AATS 部署流水线 ============"
    echo

    resolve_profile "$PROFILE"
    preflight

    step_commit
    step_sync
    prepare_runtime_readiness_generation
    step_build
    step_down
    step_prune
    ensure_wsl_runtime_prerequisites
    ensure_rdp_artifact_directory
    step_infra_up
    step_schema_migrate
    step_app_up
    step_health
    write_deployment_evidence
    report
}

main
