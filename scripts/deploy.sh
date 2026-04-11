#!/usr/bin/env bash
# =============================================================================
# AATS 标准化部署脚本
#
# 一条命令完成：代码提交 → WSL2 同步 → 镜像构建 → 旧镜像清理 → 服务启动 → 健康检查
#
# 设计原则：
#   1. Profile 驱动：自动映射 compose 叠加层 + env 文件，杜绝手动拼错
#   2. 复用已有设施：git + sync_to_wsl2.sh + docker compose
#   3. 安全第一：凭证不打印，下线前确认，任何步骤失败立即停止
#   4. 幂等：重复执行不会产生副作用
#
# 用法：
#   ./scripts/deploy.sh                                          # 默认 derivatives-live-monolith
#   ./scripts/deploy.sh --profile spot-live                      # 指定 profile
#   ./scripts/deploy.sh --commit "修复策略页布局"                  # 先提交再部署
#   ./scripts/deploy.sh --no-cache                               # 不走 docker 缓存
#   ./scripts/deploy.sh --skip-sync                              # 跳过 WSL2 同步
#   ./scripts/deploy.sh --skip-commit                            # 跳过提交（有改动时不报错）
#   ./scripts/deploy.sh --profile spot --commit "更新现货策略"     # 组合
#
# 支持的 Profile：
#   spot                       现货模拟盘（4 进程）
#   spot-live                  现货实盘（4 进程）
#   derivatives                衍生品模拟盘（4 进程）
#   derivatives-live           衍生品实盘（4 进程）
#   derivatives-live-monolith  衍生品实盘（单进程，hedge 模式）  ← 默认
#
# 退出码：
#   0 = 部署成功
#   1 = 参数/配置错误
#   2 = 同步/构建/启动失败
#   3 = 健康检查超时
# =============================================================================

set -euo pipefail

# ─── 颜色输出 ───────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'  # No Color

log_info()  { echo -e "${CYAN}[deploy]${NC} $*"; }
log_ok()    { echo -e "${GREEN}[deploy]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[deploy]${NC} $*"; }
log_error() { echo -e "${RED}[deploy]${NC} $*" >&2; }

# ─── 路径常量 ───────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_DIR="deploy/wsl2-dev"

DISTRO="${AATS_WSL2_DISTRO:-Ubuntu}"
WSL_PROJECT="${AATS_WSL2_PROJECT:-\$HOME/aats}"

# ─── 参数解析 ───────────────────────────────────────────────────────────
PROFILE="derivatives-live-monolith"
COMMIT_MSG=""
NO_CACHE=""
SKIP_SYNC=false
SKIP_COMMIT=false
HEALTH_TIMEOUT=90

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
            sed -n '2,36p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)
            log_error "未知参数: $1"; exit 1 ;;
    esac
done

# ─── Profile → Compose 文件 + Env 文件映射 ──────────────────────────────
#
# 每个 profile 对应：
#   COMPOSE_FILES — docker-compose.yml (infra) + docker-compose.aats.yml (base) + overlay
#   ENV_FILES     — .env.wsl2 (infra 模板变量) + profile env (凭证 + 运行时配置)
#
resolve_profile() {
    local profile="$1"
    local base_compose="docker-compose.yml"
    local aats_compose="docker-compose.aats.yml"
    local deploy="$DEPLOY_DIR"

    case "$profile" in
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
            log_error "不支持的 profile: $profile"
            log_error "可选: spot | spot-live | derivatives | derivatives-live | derivatives-live-monolith"
            exit 1
            ;;
    esac

    COMPOSE_CMD_ARGS="-f $base_compose -f $aats_compose -f $COMPOSE_OVERLAY --env-file .env.wsl2 --env-file $ENV_PROFILE"
}

# ─── WSL2 执行辅助 ─────────────────────────────────────────────────────
wsl_run() {
    wsl -d "$DISTRO" bash -c "$1"
}

# ─── Windows 路径转 WSL 路径 ────────────────────────────────────────────
win_to_wsl_path() {
    local p="$1"
    if [[ "$p" == /mnt/* ]]; then echo "$p"; return; fi
    if [[ "$p" =~ ^/[a-zA-Z]/ ]]; then
        local drive="${p:1:1}"; local rest="${p:2}"
        echo "/mnt/${drive,,}${rest}"; return
    fi
    if [[ "$p" =~ ^[a-zA-Z]: ]]; then
        local drive="${p:0:1}"; local rest="${p:2}"; rest="${rest//\\//}"
        echo "/mnt/${drive,,}${rest}"; return
    fi
    echo "$p"
}

# ─── 预检查 ─────────────────────────────────────────────────────────────
preflight() {
    log_info "Profile:  $PROFILE"
    log_info "Overlay:  $COMPOSE_OVERLAY"
    log_info "Env:      $ENV_PROFILE"
    echo

    # 检查 wsl 命令
    if ! command -v wsl >/dev/null 2>&1; then
        log_error "找不到 wsl 命令，本脚本需要 Windows + WSL2 环境"
        exit 1
    fi

    # 检查 WSL2 项目目录
    if ! wsl_run "test -d $WSL_PROJECT/.git"; then
        log_error "WSL2 项目目录 $WSL_PROJECT 不存在，请先运行: ./scripts/sync_to_wsl2.sh init"
        exit 1
    fi

    # 检查 env 文件存在（在 WSL2 侧）
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && test -f .env.wsl2" || {
        log_error "WSL2 侧缺失 $DEPLOY_DIR/.env.wsl2"
        exit 1
    }
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && test -f $ENV_PROFILE" || {
        log_error "WSL2 侧缺失 env 文件: $ENV_PROFILE (相对于 $DEPLOY_DIR/)"
        exit 1
    }

    # 检查 compose overlay 存在
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && test -f $COMPOSE_OVERLAY" || {
        log_error "WSL2 侧缺失 compose overlay: $COMPOSE_OVERLAY"
        exit 1
    }
}

# ─── Step 1: 提交代码 ──────────────────────────────────────────────────
step_commit() {
    if [[ -n "$COMMIT_MSG" ]]; then
        log_info "Step 1/6: 提交代码..."
        cd "$PROJECT_ROOT"

        # 检查是否有改动
        if git diff --quiet && git diff --cached --quiet && [[ -z "$(git ls-files --others --exclude-standard)" ]]; then
            log_warn "工作区干净，无需提交"
        else
            git add -A
            git commit -m "$COMMIT_MSG"
            log_ok "已提交: $(git log --oneline -1)"
        fi
    elif [[ "$SKIP_COMMIT" == false ]]; then
        # 没传 --commit 也没传 --skip-commit，检查是否有未提交改动
        cd "$PROJECT_ROOT"
        if ! git diff --quiet || ! git diff --cached --quiet || [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
            log_warn "检测到未提交的改动（使用 --commit \"msg\" 自动提交，或 --skip-commit 跳过）"
            log_warn "以下文件有改动："
            git status --short
            echo
            read -r -p "继续部署未提交的代码？[y/N] " confirm
            if [[ "$confirm" != [yY] ]]; then
                log_info "已取消"
                exit 0
            fi
        fi
    else
        log_info "Step 1/6: 跳过提交（--skip-commit）"
    fi
}

# ─── Step 2: 同步到 WSL2 ───────────────────────────────────────────────
step_sync() {
    if [[ "$SKIP_SYNC" == true ]]; then
        log_info "Step 2/6: 跳过同步（--skip-sync）"
        return
    fi

    log_info "Step 2/6: 同步代码到 WSL2..."
    "$SCRIPT_DIR/sync_to_wsl2.sh" pull
    log_ok "同步完成"
}

# ─── Step 3: 停止旧服务 ────────────────────────────────────────────────
step_down() {
    log_info "Step 3/6: 停止旧服务..."
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && docker compose $COMPOSE_CMD_ARGS down --timeout 5" || {
        log_warn "docker compose down 返回非零（可能无正在运行的服务，继续）"
    }
    log_ok "旧服务已停止"
}

# ─── Step 4: 构建新镜像 ────────────────────────────────────────────────
step_build() {
    log_info "Step 4/6: 构建新镜像${NO_CACHE:+（无缓存）}..."
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && docker compose $COMPOSE_CMD_ARGS build $NO_CACHE"
    log_ok "镜像构建完成"
}

# ─── Step 5: 清理悬空镜像 ──────────────────────────────────────────────
step_prune() {
    log_info "Step 5/6: 清理悬空镜像..."
    local pruned
    pruned=$(wsl_run "docker image prune -f 2>/dev/null" || true)
    if echo "$pruned" | grep -q "Total reclaimed space: 0B"; then
        log_info "无悬空镜像需要清理"
    else
        log_ok "清理完成"
    fi
}

# ─── Step 6: 启动新服务 ────────────────────────────────────────────────
step_up() {
    log_info "Step 6/6: 启动服务..."
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && docker compose $COMPOSE_CMD_ARGS up -d"
    log_ok "服务已启动"
}

# ─── 健康检查 ───────────────────────────────────────────────────────────
step_health() {
    log_info "健康检查（超时 ${HEALTH_TIMEOUT}s）..."

    # 从 env 文件读取 API 端口（不泄露其他内容）
    local port
    port=$(wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && grep -h '^AATS_API_PORT=' $ENV_PROFILE .env.wsl2 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '\"'" || echo "")
    port="${port:-8000}"

    local elapsed=0
    local interval=3
    while [[ $elapsed -lt $HEALTH_TIMEOUT ]]; do
        if wsl_run "curl -sf http://127.0.0.1:$port/healthz >/dev/null 2>&1"; then
            log_ok "Gateway 健康检查通过 (port $port, ${elapsed}s)"
            return 0
        fi
        sleep $interval
        elapsed=$((elapsed + interval))
    done

    log_error "健康检查超时 (${HEALTH_TIMEOUT}s)，gateway 未就绪"
    log_error "查看日志: wsl -d $DISTRO bash -c 'cd $WSL_PROJECT/$DEPLOY_DIR && docker compose $COMPOSE_CMD_ARGS logs --tail 50'"
    exit 3
}

# ─── 部署报告 ───────────────────────────────────────────────────────────
report() {
    echo
    echo "=========================================="
    log_ok "部署完成"
    echo "=========================================="
    log_info "Profile:    $PROFILE"
    log_info "Overlay:    $COMPOSE_OVERLAY"

    # 显示运行中的容器
    echo
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && docker compose $COMPOSE_CMD_ARGS ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'" || true
    echo

    # 显示当前 git commit
    local head
    head=$(cd "$PROJECT_ROOT" && git log --oneline -1)
    log_info "Git HEAD:   $head"
    echo
}

# ─── 主流程 ─────────────────────────────────────────────────────────────
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
    step_up
    step_health
    report
}

main
