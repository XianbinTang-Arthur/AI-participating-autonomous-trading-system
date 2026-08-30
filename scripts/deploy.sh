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
# strict 4-role readiness v2 含 55s takeover quarantine，再叠加 10-30s build、
# peer barrier、buffer flush、background start 与 healthcheck 采样。默认预算必须
# 覆盖合法最坏启动，避免标准入口在安全等待即将完成时误报失败。
HEALTH_TIMEOUT=210
APP_STABILITY_WINDOW_SECONDS=40

COMPOSE_OVERLAY=""
ENV_PROFILE=""
ENV_PROFILE_PATH=""
WSL2_ENV_FILE=""
APP_CONTAINERS=""
# Stop verification must cover the union of every app container that any
# supported profile can leave behind. APP_CONTAINERS remains the target
# profile's post-deploy health/evidence contract.
ALL_KNOWN_APP_CONTAINERS="aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon aats-liquidations-daemon aats-microstructure-collector"
NATS_EXPECTED_IMAGE="nats:2.10-alpine@sha256:b83efabe3e7def1e0a4a31ec6e078999bb17c80363f881df35edc70fcb6bb927"
COMPOSE_CMD_ARGS=""
OPERATOR_TLS_ENABLED=false
OPERATOR_HEALTH_SCHEME="http"
OPERATOR_TLS_RUNTIME_DIR=""
OPERATOR_TLS_CERT_WSL=""
OPERATOR_TLS_KEY_WSL=""
OPERATOR_TLS_CERT_CONTAINER=""
OPERATOR_TLS_KEY_CONTAINER=""
DEPLOYMENT_EVIDENCE_PATH=""
APP_HEALTH_BOUNDARY_STARTED_NS=""
APP_HEALTH_BOUNDARY_FINGERPRINT=""
APP_COLLECTOR_HEARTBEAT_ARGS=""
APP_UP_AUTHORIZED_NS=""
LIFECYCLE_MONITOR_CONTROL_DIR=""
LIFECYCLE_MONITOR_TOKEN=""
LIFECYCLE_MONITOR_PID=""
LIFECYCLE_MONITOR_STARTED_NS=""
NATS_CUTOVER_PREFLIGHT_BEFORE_EVIDENCE_PATH=""
NATS_CUTOVER_PREFLIGHT_AFTER_EVIDENCE_PATH=""
NATS_CUTOVER_BOOTSTRAP_MODE=""
NATS_CUTOVER_BASELINE_FINGERPRINT=""
NATS_CUTOVER_VOLUME_FINGERPRINT=""
NATS_TARGET_ENV_SNAPSHOT_PATH=""
NATS_TARGET_MANIFEST_SHA256=""
NATS_TARGET_SNAPSHOT_RUNTIME_DIR="/run/aats-deploy"
RUNTIME_READINESS_GENERATION=""
DEPLOYED_GIT_COMMIT=""
DOCKER_DAEMON_ID=""
DEPLOY_LOCK_FILE="/tmp/aats-standard-deploy.lock"
DEPLOY_LOCK_OVERRIDE_REJECTED=false
if [[ -n "${AATS_DEPLOY_LOCK_FILE:-}" ]]; then
    if [[ "${AATS_DEPLOY_TEST_MODE:-false}" == true \
        && "${BASH_SOURCE[0]}" != "$0" ]]; then
        DEPLOY_LOCK_FILE="$AATS_DEPLOY_LOCK_FILE"
    else
        # A caller-selectable production path would split the supposedly global
        # mutex and allow two standard deploys to mutate one Compose project.
        DEPLOY_LOCK_OVERRIDE_REJECTED=true
    fi
fi
DEPLOY_LOCK_TOKEN=""
DEPLOY_LOCK_SCOPE=""
DEPLOY_LOCK_STALE_SECONDS=12
if [[ -n "${AATS_DEPLOY_TEST_LOCK_STALE_SECONDS:-}" ]]; then
    if [[ "${AATS_DEPLOY_TEST_MODE:-false}" == true \
        && "${BASH_SOURCE[0]}" != "$0" \
        && "$AATS_DEPLOY_TEST_LOCK_STALE_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
        DEPLOY_LOCK_STALE_SECONDS="$AATS_DEPLOY_TEST_LOCK_STALE_SECONDS"
    else
        DEPLOY_LOCK_OVERRIDE_REJECTED=true
    fi
fi
DEPLOY_LOCK_HELD=false
DEPLOY_LOCK_KEEPER_PID=""
DEPLOY_LOCK_HEARTBEAT_PID=""
DEPLOY_LOCK_LEASE_FILE=""
DEPLOY_LOCK_WRITER_FD=""
DEPLOY_WSL_DEFAULT_UID=""
APP_QUIESCENCE_SNAPSHOT=""
DEPLOY_ACTIVE_PROCESS_PID=""
DEPLOY_ACTIVE_PROCESS_CONTEXT=""
DEPLOY_ACTIVE_GATE_DIR=""
DEPLOY_ACTIVE_MARKER_FILE=""
DEPLOY_ACTIVE_SEQUENCE=0
DEPLOY_SUPERVISION_POISONED=false

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
    local local_docker_command io_mode="${2:-capture}"
    local_docker_command="export DOCKER_HOST='unix:///var/run/docker.sock'; unset DOCKER_CONTEXT; $1"
    if [[ "$DEPLOY_LOCK_HELD" == true ]]; then
        run_lock_supervised_wsl "WSL 命令" default "$io_mode" "$local_docker_command"
    else
        wsl -d "$DISTRO" bash -c "$local_docker_command"
    fi
}

wsl_root_run() {
    local local_docker_command io_mode="${2:-capture}"
    local_docker_command="export DOCKER_HOST='unix:///var/run/docker.sock'; unset DOCKER_CONTEXT; $1"
    if [[ "$DEPLOY_LOCK_HELD" == true ]]; then
        run_lock_supervised_wsl "WSL root 命令" root "$io_mode" "$local_docker_command"
    else
        wsl -d "$DISTRO" -u root bash -c "$local_docker_command"
    fi
}

wsl_root_run_script() {
    local script="${1:-}"
    local encoded
    if [[ -z "$script" ]]; then
        log_error "拒绝执行空的 WSL root 脚本"
        return 1
    fi
    if ! encoded="$(printf '%s' "$script" | base64 | tr -d '\r\n')" \
        || [[ ! "$encoded" =~ ^[A-Za-z0-9+/]+={0,2}$ ]]; then
        log_error "无法安全编码 WSL root 脚本"
        return 1
    fi
    # Multiline scripts containing nested quotes and shell variables do not
    # survive the Git-Bash -> wsl.exe -> bash -c boundary reliably.  Transport
    # the exact bytes as base64, then decode into a fresh WSL bash process.
    wsl_root_run "printf '%s' '$encoded' | base64 --decode | bash"
}

windows_path_to_wsl_mount() {
    local path="$1" drive rest
    case "$path" in
        /mnt/[a-zA-Z]/*)
            printf '%s\n' "$path"
            ;;
        /[a-zA-Z]/*)
            drive="${path:1:1}"
            printf '/mnt/%s%s\n' "${drive,,}" "${path:2}"
            ;;
        [a-zA-Z]:*)
            drive="${path:0:1}"
            rest="${path:2}"
            rest="${rest//\\//}"
            printf '/mnt/%s%s\n' "${drive,,}" "$rest"
            ;;
        *)
            return 1
            ;;
    esac
}

build_wsl_checkout_sync_command() {
    local source_path="$1" target_path="$2" source_branch="$3" source_head="$4"
    local source_q target_q branch_q head_q sync_script
    if [[ "$source_path" != /mnt/[a-zA-Z]/* || "$target_path" != /* \
        || "$source_path" == *$'\n'* || "$target_path" == *$'\n'* \
        || ! "$source_head" =~ ^[0-9a-fA-F]{40}$ ]]; then
        return 1
    fi
    if [[ -n "$source_branch" ]] \
        && ! git check-ref-format --branch "$source_branch" >/dev/null 2>&1; then
        return 1
    fi
    printf -v source_q '%q' "$source_path"
    printf -v target_q '%q' "$target_path"
    printf -v branch_q '%q' "$source_branch"
    printf -v head_q '%q' "${source_head,,}"
    printf -v sync_script '%s\n' \
        'set -euo pipefail' \
        "source_path=$source_q" \
        "target_path=$target_q" \
        "source_branch=$branch_q" \
        "source_head=$head_q" \
        'if [[ ! -d "$target_path/.git" ]]; then' \
        '    printf "[ERROR] WSL2 目标不是 Git checkout: %s\n" "$target_path" >&2' \
        '    exit 21' \
        'fi' \
        'if ! git -C "$target_path" diff --quiet --ignore-submodules=none -- || ! git -C "$target_path" diff --cached --quiet --ignore-submodules=none -- || [[ -n "$(git -C "$target_path" ls-files --others --exclude-standard)" ]]; then' \
        '    printf "[ERROR] WSL2 checkout 存在未提交改动，拒绝同步: %s\n" "$target_path" >&2' \
        '    exit 22' \
        'fi' \
        'wsl_branch=$(git -C "$target_path" symbolic-ref --quiet --short HEAD 2>/dev/null || printf "(detached)")' \
        'if [[ -n "$source_branch" && "$wsl_branch" != "$source_branch" ]]; then' \
        '    case "$wsl_branch" in' \
        '        worktree-agent-*|"(detached)")' \
        '            git -C "$target_path" fetch "$source_path" "$source_branch"' \
        '            git -C "$target_path" checkout "$source_branch" 2>/dev/null \
                || git -C "$target_path" checkout -b "$source_branch" FETCH_HEAD' \
        '            ;;' \
        '        *)' \
        '            printf "[ERROR] WSL2 分支 %s 与 Windows 分支 %s 不一致，拒绝覆盖\n" "$wsl_branch" "$source_branch" >&2' \
        '            exit 23' \
        '            ;;' \
        '    esac' \
        'fi' \
        'if [[ -n "$source_branch" ]]; then' \
        '    git -C "$target_path" fetch "$source_path" "$source_branch"' \
        '    if git -C "$target_path" show-ref --verify --quiet "refs/heads/$source_branch"; then' \
        '        git -C "$target_path" checkout "$source_branch"' \
        '    else' \
        '        git -C "$target_path" checkout -b "$source_branch" FETCH_HEAD' \
        '    fi' \
        '    git -C "$target_path" merge --ff-only FETCH_HEAD' \
        'else' \
        '    git -C "$target_path" fetch "$source_path" "$source_head"' \
        '    git -C "$target_path" checkout --detach FETCH_HEAD' \
        'fi' \
        'wsl_head_after=$(git -C "$target_path" rev-parse HEAD)' \
        'if [[ "${wsl_head_after,,}" != "$source_head" ]]; then' \
        '    printf "[ERROR] 同步后 WSL2 HEAD %s 不等于 Windows HEAD %s\n" "$wsl_head_after" "$source_head" >&2' \
        '    exit 24' \
        'fi' \
        'git -C "$target_path" log --oneline -3'
    printf '%s' "$sync_script"
}

abort_deploy_lock_release() {
    local original_status="$1"
    # A non-zero return from an EXIT trap does not replace the shell's pending
    # exit status.  Once lock cleanup becomes ambiguous, terminate explicitly:
    # preserve an existing failure, or turn an otherwise-successful deploy into
    # a deterministic fail-closed status.
    trap - EXIT
    if [[ "$original_status" -ne 0 ]]; then
        exit "$original_status"
    fi
    exit 16
}

release_deploy_lock() {
    local original_status=$?
    # EXIT/HUP/INT/TERM may arrive while a Windows, WSL, Git, sync, or Docker
    # child is still mutating deployment state.  The child must be gone before
    # heartbeat/lease/flock release; otherwise a second deploy could enter while
    # the first child continues outside its ownership window.
    trap - HUP INT TERM
    if ! terminate_active_supervised_process; then
        # A surviving active marker tells both the current WSL flock holder and
        # every successor that a mutation process has not been proven dead.
        # Releasing the keeper here would turn an uncertain cleanup into two
        # concurrent deploy owners, so deliberately leave the lock fail-closed.
        log_error "受监督部署步骤未能确认终止；保留 WSL2 锁与 active marker，禁止自动接管"
        abort_deploy_lock_release "$original_status"
    fi
    if ! assert_no_owned_active_markers "释放部署锁"; then
        # wsl_run is frequently evaluated in command substitutions.  Shell
        # variables set by those subshells do not propagate, but the WSL marker
        # is durable and must still prevent this parent from killing the keeper.
        log_error "子 shell 留下未决 active marker；保留 lease/flock 等待显式恢复"
        abort_deploy_lock_release "$original_status"
    fi
    if ! cleanup_deployment_lifecycle_monitor; then
        # The monitor is read-only and has a bounded self-timeout, so it cannot
        # mutate the stack after lock release.  Still surface an unverified
        # cleanup instead of silently claiming a clean deployment exit.
        log_warn "实时 Docker 生命周期观察器未确认清理；其自动超时仍会终止只读进程"
    fi
    if ! cleanup_nats_target_env_snapshot; then
        # The snapshot contains only eight public capacity values.  A failed
        # cleanup is therefore not a credential leak, but retain a visible
        # warning because stale root-owned deployment inputs should not pile up.
        log_warn "NATS 目标参数只读快照未确认清理；下次标准部署会使用新的唯一快照"
    fi
    if ! assert_no_owned_active_markers "释放部署锁前最终确认"; then
        # Snapshot cleanup above is itself a supervised root WSL mutation.  It
        # can create a new durable marker after the initial release check; do
        # not drop the keeper or report success when its transport is ambiguous.
        log_error "清理阶段留下未决 active marker；保留 lease/flock 等待显式恢复"
        abort_deploy_lock_release "$original_status"
    fi
    if ! assert_deploy_lock_held "释放部署锁前最终确认"; then
        # A completion acknowledgement may have proven the command and removed
        # its marker while final acknowledgement cleanup still failed.  The
        # supervision poison flag must likewise make a pending success fail.
        log_error "清理阶段未保持可信部署锁/监督状态；拒绝报告成功"
        abort_deploy_lock_release "$original_status"
    fi
    if [[ "$DEPLOY_LOCK_HELD" == true ]]; then
        if [[ -n "$DEPLOY_LOCK_HEARTBEAT_PID" ]]; then
            kill "$DEPLOY_LOCK_HEARTBEAT_PID" 2>/dev/null || true
            wait "$DEPLOY_LOCK_HEARTBEAT_PID" 2>/dev/null || true
        fi
        remove_deploy_lock_lease 2>/dev/null || true
        if [[ -n "$DEPLOY_LOCK_WRITER_FD" ]]; then
            eval "exec ${DEPLOY_LOCK_WRITER_FD}>&-"
        fi
        if [[ -n "$DEPLOY_LOCK_KEEPER_PID" ]]; then
            local attempt
            for attempt in {1..25}; do
                if ! kill -0 "$DEPLOY_LOCK_KEEPER_PID" 2>/dev/null; then
                    break
                fi
                sleep 0.2
            done
            kill "$DEPLOY_LOCK_KEEPER_PID" 2>/dev/null || true
            wait "$DEPLOY_LOCK_KEEPER_PID" 2>/dev/null || true
        fi
        DEPLOY_LOCK_HELD=false
        DEPLOY_LOCK_HEARTBEAT_PID=""
        DEPLOY_LOCK_KEEPER_PID=""
        DEPLOY_LOCK_LEASE_FILE=""
        DEPLOY_LOCK_WRITER_FD=""
        DEPLOY_WSL_DEFAULT_UID=""
    fi
    trap - EXIT
    return "$original_status"
}

deploy_signal_exit() {
    exit "$1"
}

touch_deploy_lock_lease() {
    local lease_file_q
    printf -v lease_file_q '%q' "$DEPLOY_LOCK_LEASE_FILE"
    MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "touch -- $lease_file_q" >/dev/null
}

remove_deploy_lock_lease() {
    local lease_file_q
    printf -v lease_file_q '%q' "$DEPLOY_LOCK_LEASE_FILE"
    MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "rm -f -- $lease_file_q" >/dev/null
}

deploy_lock_heartbeat_loop() {
    local parent_pid="$1"
    while kill -0 "$parent_pid" 2>/dev/null; do
        touch_deploy_lock_lease || return 1
        sleep 3
    done
}

acquire_deploy_lock() {
    local lock_file_q lease_file_q lease_glob_q active_glob_q token_q stale_q handshake coproc_reader coproc_writer
    if [[ "$DEPLOY_LOCK_OVERRIDE_REJECTED" == true ]]; then
        log_error "AATS_DEPLOY_LOCK_FILE 仅允许在 AATS_DEPLOY_TEST_MODE=true 的隔离测试中使用"
        log_error "标准部署必须共享固定全局锁: /tmp/aats-standard-deploy.lock"
        exit 18
    fi
    DEPLOY_LOCK_TOKEN="$(date -u +%Y%m%dT%H%M%S)-$$-$RANDOM"
    DEPLOY_LOCK_SCOPE="$(printf '%s' "$DEPLOY_LOCK_FILE" | sha256sum | awk '{print substr($1, 1, 16)}')"
    if [[ ! "$DEPLOY_LOCK_SCOPE" =~ ^[0-9a-f]{16}$ ]]; then
        log_error "无法生成部署锁作用域，拒绝启动"
        exit 18
    fi
    DEPLOY_LOCK_LEASE_FILE="/tmp/aats-standard-deploy-lease-$DEPLOY_LOCK_SCOPE-$DEPLOY_LOCK_TOKEN"
    printf -v lock_file_q '%q' "$DEPLOY_LOCK_FILE"
    printf -v lease_file_q '%q' "$DEPLOY_LOCK_LEASE_FILE"
    printf -v lease_glob_q '%q' "/tmp/aats-standard-deploy-lease-$DEPLOY_LOCK_SCOPE-*"
    printf -v active_glob_q '%q' "/tmp/aats-standard-deploy-active-$DEPLOY_LOCK_SCOPE-*"
    printf -v token_q '%q' "$DEPLOY_LOCK_TOKEN"
    printf -v stale_q '%q' "$DEPLOY_LOCK_STALE_SECONDS"
    coproc AATS_DEPLOY_LOCK_KEEPER {
        export MSYS_NO_PATHCONV=1
        exec wsl -d "$DISTRO" bash -c "set -euo pipefail; umask 077; : >$lease_file_q; chmod 600 -- $lease_file_q; exec 9>>$lock_file_q; if ! flock -n 9; then rm -f -- $lease_file_q; printf 'BUSY\\n'; exit 75; fi; while ! python3 -c 'import glob, os, sys, time; own, lease_glob, active_glob, stale=sys.argv[1:5]; now=time.time(); fresh_other=any(path != own and now - os.stat(path).st_mtime <= float(stale) for path in glob.glob(lease_glob)); sys.exit(1 if fresh_other or glob.glob(active_glob) else 0)' $lease_file_q $lease_glob_q $active_glob_q $stale_q 2>/dev/null; do touch -- $lease_file_q; sleep 0.2; done; touch -- $lease_file_q; printf 'ACQUIRED:%s:%s\\n' $token_q \"\$(id -u)\"; while python3 -c 'import glob, os, sys, time; lease, active_glob, stale=sys.argv[1:4]; lease_fresh=time.time() - os.stat(lease).st_mtime <= float(stale); sys.exit(0 if lease_fresh or glob.glob(active_glob) else 1)' $lease_file_q $active_glob_q $stale_q 2>/dev/null; do sleep 0.2; done; rm -f -- $lease_file_q; exit 77"
    }
    DEPLOY_LOCK_KEEPER_PID="$AATS_DEPLOY_LOCK_KEEPER_PID"
    coproc_reader="${AATS_DEPLOY_LOCK_KEEPER[0]}"
    coproc_writer="${AATS_DEPLOY_LOCK_KEEPER[1]}"
    DEPLOY_LOCK_WRITER_FD="$coproc_writer"
    if ! IFS= read -r -t 30 handshake <&"$coproc_reader" \
        || [[ "${handshake%:*}" != "ACQUIRED:$DEPLOY_LOCK_TOKEN" \
            || ! "${handshake##*:}" =~ ^[0-9]+$ ]]; then
        kill "$DEPLOY_LOCK_KEEPER_PID" 2>/dev/null || true
        wait "$DEPLOY_LOCK_KEEPER_PID" 2>/dev/null || true
        eval "exec ${coproc_writer}>&-" 2>/dev/null || true
        eval "exec ${coproc_reader}<&-" 2>/dev/null || true
        DEPLOY_LOCK_TOKEN=""
        DEPLOY_WSL_DEFAULT_UID=""
        log_error "另一个标准部署正在运行，或 WSL2 长寿命 flock holder 无法建立: $DEPLOY_LOCK_FILE"
        log_error "拒绝并发修改同一 WSL2 模拟栈；不要绕过锁或并行手工启动 Docker 应用容器"
        exit 14
    fi
    DEPLOY_WSL_DEFAULT_UID="${handshake##*:}"
    eval "exec ${coproc_reader}<&-"
    DEPLOY_LOCK_HELD=true
    deploy_lock_heartbeat_loop "$$" &
    DEPLOY_LOCK_HEARTBEAT_PID=$!
    trap release_deploy_lock EXIT
    trap 'deploy_signal_exit 129' HUP
    trap 'deploy_signal_exit 130' INT
    trap 'deploy_signal_exit 143' TERM
    log_ok "已取得 WSL2 全流程标准部署互斥锁"
}

assert_deploy_lock_held() {
    local context="$1" lock_file_q probe
    printf -v lock_file_q '%q' "$DEPLOY_LOCK_FILE"
    if [[ "$DEPLOY_SUPERVISION_POISONED" == true \
        || "$DEPLOY_LOCK_HELD" != true || -z "$DEPLOY_LOCK_KEEPER_PID" ]] \
        || ! kill -0 "$DEPLOY_LOCK_KEEPER_PID" 2>/dev/null \
        || [[ -z "$DEPLOY_LOCK_HEARTBEAT_PID" ]] \
        || ! kill -0 "$DEPLOY_LOCK_HEARTBEAT_PID" 2>/dev/null \
        || ! probe="$(MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "exec 8>>$lock_file_q; if flock -n 8; then printf 'FREE\\n'; else printf 'HELD\\n'; fi" | tr -d '\r')" \
        || [[ "$probe" != "HELD" ]]; then
        log_error "WSL2 标准部署锁 holder 已丢失；拒绝继续 $context"
        return 16
    fi
}

assert_no_owned_active_markers() {
    local context="$1" active_glob active_glob_q probe
    if [[ ! "$DEPLOY_LOCK_SCOPE" =~ ^[0-9a-f]{16}$ \
        || ! "$DEPLOY_LOCK_TOKEN" =~ ^[A-Za-z0-9._:-]+$ ]]; then
        log_error "当前部署缺少有效的 marker scope/token；拒绝确认 $context"
        return 16
    fi
    active_glob="/tmp/aats-standard-deploy-active-$DEPLOY_LOCK_SCOPE-$DEPLOY_LOCK_TOKEN-*"
    printf -v active_glob_q '%q' "$active_glob"
    if ! probe="$(MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
        "python3 -c 'import glob, sys; print(\"ACTIVE\" if glob.glob(sys.argv[1]) else \"CLEAR\")' $active_glob_q" \
        2>/dev/null | tr -d '\r')" || [[ "$probe" != CLEAR ]]; then
        log_error "当前部署仍有未闭合 active marker，拒绝 $context"
        return 16
    fi
}

terminate_active_supervised_process() {
    local process_pid="$DEPLOY_ACTIVE_PROCESS_PID"
    if [[ -n "$DEPLOY_ACTIVE_GATE_DIR" && -d "$DEPLOY_ACTIVE_GATE_DIR" ]]; then
        # Wake a wrapper that has been registered but not yet granted its final
        # launch capability.  Without this cancellation handshake an EXIT trap
        # could wait forever while the wrapper waits for authorization.
        : >"$DEPLOY_ACTIVE_GATE_DIR/cancel" 2>/dev/null || true
    fi
    if [[ ! "$process_pid" =~ ^[1-9][0-9]*$ ]]; then
        DEPLOY_ACTIVE_PROCESS_PID=""
        DEPLOY_ACTIVE_PROCESS_CONTEXT=""
        remove_active_supervision_artifacts
        return $?
    fi
    if kill -0 "$process_pid" 2>/dev/null; then
        # Do not kill a client process and then assume its Docker/WSL daemon-side
        # mutation also stopped.  The wrapper keeps the active marker until the
        # synchronous command really returns; the keeper therefore cannot
        # relinquish flock, even after its parent receives TERM or SIGKILL.
        log_warn "部署退出前等待受监督步骤安全结束: ${DEPLOY_ACTIVE_PROCESS_CONTEXT:-unknown}"
    fi
    # Reap/confirm completion before relinquishing the deployment lock.  wait
    # returns immediately when the child has already exited.
    wait "$process_pid" 2>/dev/null || true
    if kill -0 "$process_pid" 2>/dev/null; then
        log_error "受监督进程仍然存活，不能释放部署锁: $process_pid"
        return 16
    fi
    if ! remove_active_supervision_artifacts; then
        DEPLOY_SUPERVISION_POISONED=true
        return 16
    fi
    DEPLOY_ACTIVE_PROCESS_PID=""
    DEPLOY_ACTIVE_PROCESS_CONTEXT=""
}

remove_proven_completed_active_marker() {
    local marker_file="$1" marker_q attempt
    if [[ ! "$marker_file" =~ ^/tmp/aats-standard-deploy-active-[0-9a-f]{16}-[A-Za-z0-9._:-]+-[1-9][0-9]*$ ]]; then
        return 16
    fi
    printf -v marker_q '%q' "$marker_file"
    # This helper is intentionally callable only by the command guard after
    # its synchronous child returned, or by the wrapper before final launch.
    # A transient wsl.exe failure must not permanently poison the global lock,
    # but the parent still cannot use this helper to erase an ambiguous marker.
    for attempt in {1..10}; do
        if MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
            "rm -f -- $marker_q; test ! -e $marker_q" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.2
    done
    return 16
}

build_wsl_completion_wrapped_command() {
    local remote_command="$1" marker_file="$2" completion_file="$3" io_mode="$4"
    local expected_marker_uid="$5"
    local expected_completion encoded marker_q completion_q wrapped
    if [[ ! "$marker_file" =~ ^/tmp/aats-standard-deploy-active-[0-9a-f]{16}-[A-Za-z0-9._:-]+-[1-9][0-9]*$ \
        || ! "$completion_file" =~ ^/tmp/aats-standard-deploy-completion-[0-9a-f]{16}-[A-Za-z0-9._:-]+-[1-9][0-9]*$ ]]; then
        return 16
    fi
    expected_completion="${marker_file/aats-standard-deploy-active-/aats-standard-deploy-completion-}"
    if [[ "$completion_file" != "$expected_completion" || -z "$remote_command" \
        || ( "$io_mode" != capture && "$io_mode" != quiet && "$io_mode" != stream ) \
        || ! "$expected_marker_uid" =~ ^[0-9]+$ ]]; then
        return 16
    fi
    if ! encoded="$(printf '%s' "$remote_command" | base64 | tr -d '\r\n')" \
        || [[ -z "$encoded" || ! "$encoded" =~ ^[A-Za-z0-9+/=]+$ ]]; then
        return 16
    fi
    printf -v marker_q '%q' "$marker_file"
    printf -v completion_q '%q' "$completion_file"
    # The completion record is written by the WSL-side wrapper only after the
    # real remote command has returned.  In capture mode it also binds the exact
    # byte lengths and SHA-256 digests replayed over wsl.exe.  This is a
    # transport-completion proof inside the current WSL UID trust boundary, not
    # a cryptographic authentication boundary against that same UID.
    printf -v wrapped '%s\n' \
        'set +e' \
        "completion_file=$completion_q" \
        "marker_file=$marker_q" \
        "io_mode='$io_mode'" \
        "expected_marker_uid='$expected_marker_uid'" \
        'stdout_tmp=""' \
        'stderr_tmp=""' \
        'completion_tmp=""' \
        'completion_phase=preflight' \
        'completion_wrapper_cleanup() {' \
        '    local failure_status=$?' \
        '    trap - EXIT' \
        '    printf "[deploy] WSL completion wrapper failed: phase=%s status=%s\n" "$completion_phase" "$failure_status" >&2' \
        '    [[ -z "${stdout_tmp:-}" ]] || rm -f -- "$stdout_tmp" 2>/dev/null || true' \
        '    [[ -z "${stderr_tmp:-}" ]] || rm -f -- "$stderr_tmp" 2>/dev/null || true' \
        '    [[ -z "${completion_tmp:-}" ]] || rm -f -- "$completion_tmp" 2>/dev/null || true' \
        '    exit "$failure_status"' \
        '}' \
        'trap completion_wrapper_cleanup EXIT' \
        'if [[ -e "$completion_file" ]]; then exit 126; fi' \
        '[[ -f "$marker_file" && ! -L "$marker_file" ]] || exit 124' \
        "marker_metadata=\$(stat -c '%u|%a|%s' -- \"\$marker_file\")" \
        '[[ "$marker_metadata" == "$expected_marker_uid|600|0" ]] || exit 124' \
        'completion_phase=decode_command' \
        "remote_command=\$(printf '%s' '$encoded' | base64 --decode)" \
        'decode_status=$?' \
        'if [[ "$decode_status" -ne 0 ]]; then exit 125; fi' \
        'umask 077' \
        'pending_signal_status=0' \
        'record_pending_signal() { if [[ "$pending_signal_status" -eq 0 ]]; then pending_signal_status="$1"; fi; }' \
        "trap 'record_pending_signal 129' HUP" \
        "trap 'record_pending_signal 130' INT" \
        "trap 'record_pending_signal 143' TERM" \
        'completion_phase=run_remote_command' \
        'if [[ "$io_mode" == capture ]]; then' \
        '    stdout_tmp=$(mktemp "${completion_file}.stdout.XXXXXX") || exit 125' \
        '    stderr_tmp=$(mktemp "${completion_file}.stderr.XXXXXX") || exit 125' \
        '    bash -c "$remote_command" >"$stdout_tmp" 2>"$stderr_tmp"' \
        '    remote_status=$?' \
        '    completion_phase=hash_output' \
        '    stdout_size=$(wc -c <"$stdout_tmp" | tr -d "[:space:]")' \
        '    stderr_size=$(wc -c <"$stderr_tmp" | tr -d "[:space:]")' \
        "    stdout_sha256=\$(sha256sum -- \"\$stdout_tmp\" | awk '{print \$1}')" \
        "    stderr_sha256=\$(sha256sum -- \"\$stderr_tmp\" | awk '{print \$1}')" \
        '    [[ "$stdout_size" =~ ^[0-9]+$ && "$stderr_size" =~ ^[0-9]+$ ]] || exit 125' \
        '    [[ "$stdout_sha256" =~ ^[0-9a-f]{64}$ && "$stderr_sha256" =~ ^[0-9a-f]{64}$ ]] || exit 125' \
        'else' \
        '    bash -c "$remote_command"' \
        '    remote_status=$?' \
        '    stdout_size=-' \
        '    stderr_size=-' \
        '    stdout_sha256=-' \
        '    stderr_sha256=-' \
        'fi' \
        'completion_phase=write_completion' \
        'completion_tmp=$(mktemp "${completion_file}.tmp.XXXXXX") || exit 125' \
        "printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' $marker_q \"\$remote_status\" \"\$io_mode\" \"\$stdout_size\" \"\$stdout_sha256\" \"\$stderr_size\" \"\$stderr_sha256\" >\"\$completion_tmp\" || exit 125" \
        'chmod 600 -- "$completion_tmp" || exit 125' \
        'completion_phase=publish_completion' \
        'ln -- "$completion_tmp" "$completion_file" || exit 125' \
        'rm -f -- "$completion_tmp"' \
        'completion_tmp=""' \
        'completion_phase=replay_output' \
        'if [[ "$io_mode" == capture ]]; then' \
        '    cat -- "$stdout_tmp" || exit 123' \
        '    cat -- "$stderr_tmp" >&2 || exit 123' \
        'fi' \
        'rm -f -- "$stdout_tmp" "$stderr_tmp"' \
        'stdout_tmp=""' \
        'stderr_tmp=""' \
        'trap - HUP INT TERM' \
        'transport_status="$pending_signal_status"' \
        'trap - EXIT' \
        'if [[ "$transport_status" -ne 0 ]]; then exit "$transport_status"; fi' \
        'exit 0'
    printf '%s' "$wrapped"
}

run_wsl_completion_transport() {
    local user_mode="$1" wrapped_encoded="$2" transport_script
    local -a wsl_command=(wsl -d "$DISTRO")
    if [[ ( "$user_mode" != default && "$user_mode" != root ) \
        || -z "$wrapped_encoded" \
        || ! "$wrapped_encoded" =~ ^[A-Za-z0-9+/=]+$ ]]; then
        return 125
    fi
    if [[ "$user_mode" == root ]]; then
        wsl_command+=(-u root)
    fi
    # Decode the complete program into a static Python loader before starting
    # it.  Passing the ACK program itself to `bash` over stdin lets
    # Docker/Compose consume the unparsed tail.  The loader reads that tail to
    # EOF first, starts the real wrapper with DEVNULL stdin, and preserves the
    # wrapper's exact status (including shell-style signal status).
    printf -v transport_script '%s\n' \
        'set -o pipefail' \
        "printf '%s' '$wrapped_encoded' | base64 --decode | python3 -c 'import subprocess,sys; script=sys.stdin.read(); script or sys.exit(125); status=subprocess.run([\"bash\",\"-c\",script],stdin=subprocess.DEVNULL).returncode; sys.exit(128-status if status<0 else status)'"
    MSYS2_ARG_CONV_EXCL='*' MSYS_NO_PATHCONV=1 \
        "${wsl_command[@]}" bash -c "$transport_script"
}

finalize_proven_wsl_completion() {
    local marker_file="$1" completion_file="$2" user_mode="$3" io_mode="$4"
    local expected_stdout_size="$5" expected_stdout_sha256="$6"
    local expected_stderr_size="$7" expected_stderr_sha256="$8"
    local expected_marker_uid="$9"
    local expected_completion marker_q completion_q proof_script encoded proof attempt
    local -a wsl_args=(-d "$DISTRO")
    if [[ ! "$marker_file" =~ ^/tmp/aats-standard-deploy-active-[0-9a-f]{16}-[A-Za-z0-9._:-]+-[1-9][0-9]*$ \
        || ! "$completion_file" =~ ^/tmp/aats-standard-deploy-completion-[0-9a-f]{16}-[A-Za-z0-9._:-]+-[1-9][0-9]*$ ]]; then
        return 16
    fi
    expected_completion="${marker_file/aats-standard-deploy-active-/aats-standard-deploy-completion-}"
    if [[ "$completion_file" != "$expected_completion" \
        || ( "$io_mode" != capture && "$io_mode" != quiet && "$io_mode" != stream ) \
        || ! "$expected_marker_uid" =~ ^[0-9]+$ ]]; then
        return 16
    fi
    if [[ "$io_mode" == capture ]]; then
        if [[ ! "$expected_stdout_size" =~ ^[0-9]+$ \
            || ! "$expected_stderr_size" =~ ^[0-9]+$ \
            || ! "$expected_stdout_sha256" =~ ^[0-9a-f]{64}$ \
            || ! "$expected_stderr_sha256" =~ ^[0-9a-f]{64}$ ]]; then
            return 16
        fi
    elif [[ "$expected_stdout_size" != - || "$expected_stdout_sha256" != - \
        || "$expected_stderr_size" != - || "$expected_stderr_sha256" != - ]]; then
        return 16
    fi
    case "$user_mode" in
        default) ;;
        root) wsl_args+=(-u root) ;;
        *) return 16 ;;
    esac
    printf -v marker_q '%q' "$marker_file"
    printf -v completion_q '%q' "$completion_file"
    printf -v proof_script '%s\n' \
        'set -euo pipefail' \
        "marker_file=$marker_q" \
        "completion_file=$completion_q" \
        "expected_io_mode='$io_mode'" \
        "expected_stdout_size='$expected_stdout_size'" \
        "expected_stdout_sha256='$expected_stdout_sha256'" \
        "expected_stderr_size='$expected_stderr_size'" \
        "expected_stderr_sha256='$expected_stderr_sha256'" \
        "expected_marker_uid='$expected_marker_uid'" \
        '[[ -f "$completion_file" && ! -L "$completion_file" && -O "$completion_file" ]]' \
        "metadata=\$(stat -c '%a|%s|%F' -- \"\$completion_file\")" \
        "IFS='|' read -r mode size file_type <<<\"\$metadata\"" \
        '[[ "$mode" == 600 && "$file_type" == "regular file" && "$size" =~ ^[1-9][0-9]{0,3}$ ]]' \
        '(( size <= 1024 ))' \
        'mapfile -t records <"$completion_file"' \
        '[[ "${#records[@]}" -eq 1 ]]' \
        "IFS=\$'\\t' read -r ack_marker ack_status ack_io_mode ack_stdout_size ack_stdout_sha256 ack_stderr_size ack_stderr_sha256 ack_extra <<<\"\${records[0]}\"" \
        '[[ "$ack_marker" == "$marker_file" && -z "$ack_extra" && "$ack_status" =~ ^([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])$ ]]' \
        '[[ "$ack_io_mode" == "$expected_io_mode" ]]' \
        '[[ "$ack_stdout_size" == "$expected_stdout_size" && "$ack_stdout_sha256" == "$expected_stdout_sha256" ]]' \
        '[[ "$ack_stderr_size" == "$expected_stderr_size" && "$ack_stderr_sha256" == "$expected_stderr_sha256" ]]' \
        'if [[ -e "$marker_file" ]]; then' \
        '    [[ -f "$marker_file" && ! -L "$marker_file" ]]' \
        "    marker_metadata=\$(stat -c '%u|%a|%s' -- \"\$marker_file\")" \
        '    [[ "$marker_metadata" == "$expected_marker_uid|600|0" ]]' \
        '    rm -f -- "$marker_file"' \
        'fi' \
        '[[ ! -e "$marker_file" ]]' \
        'printf "%s\n" "$ack_status"'
    if ! encoded="$(printf '%s' "$proof_script" | base64 | tr -d '\r\n')" \
        || [[ -z "$encoded" || ! "$encoded" =~ ^[A-Za-z0-9+/=]+$ ]]; then
        return 16
    fi
    proof=""
    for attempt in {1..10}; do
        if proof="$(MSYS_NO_PATHCONV=1 wsl "${wsl_args[@]}" bash -c \
            "printf '%s' '$encoded' | base64 --decode | bash" 2>/dev/null | tr -d '\r')" \
            && [[ "$proof" =~ ^([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])$ ]]; then
            break
        fi
        proof=""
        sleep 0.2
    done
    if [[ -z "$proof" ]]; then
        return 16
    fi
    # Keep the acknowledgement until its proof has crossed the WSL transport.
    # If that transport is lost after marker removal, the next retry can still
    # re-read the same immutable record instead of guessing completion.
    for attempt in {1..10}; do
        if MSYS_NO_PATHCONV=1 wsl "${wsl_args[@]}" bash -c \
            "rm -f -- $completion_q; test ! -e $completion_q" >/dev/null 2>&1; then
            printf '%s\n' "$proof"
            return 0
        fi
        sleep 0.2
    done
    return 16
}

remove_active_supervision_artifacts() {
    local marker_q
    if [[ -n "$DEPLOY_ACTIVE_MARKER_FILE" ]]; then
        printf -v marker_q '%q' "$DEPLOY_ACTIVE_MARKER_FILE"
        if ! MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
            "test ! -e $marker_q" >/dev/null 2>&1; then
            # Only the command guard may remove a post-launch marker.  The
            # parent cannot distinguish a hard-killed wrapper from a completed
            # daemon-backed mutation, so deleting it here would permit unsafe
            # successor overlap.
            log_error "active marker 仍存在，受监督命令尚未证明结束: $DEPLOY_ACTIVE_MARKER_FILE"
            DEPLOY_SUPERVISION_POISONED=true
            return 16
        fi
        DEPLOY_ACTIVE_MARKER_FILE=""
    fi
    if [[ -n "$DEPLOY_ACTIVE_GATE_DIR" ]]; then
        rm -f -- \
            "$DEPLOY_ACTIVE_GATE_DIR/authorized" \
            "$DEPLOY_ACTIVE_GATE_DIR/cancel" \
            "$DEPLOY_ACTIVE_GATE_DIR/command.stderr" \
            "$DEPLOY_ACTIVE_GATE_DIR/command.stdout" \
            "$DEPLOY_ACTIVE_GATE_DIR/prepared" \
            "$DEPLOY_ACTIVE_GATE_DIR/launch" 2>/dev/null || true
        rmdir -- "$DEPLOY_ACTIVE_GATE_DIR" 2>/dev/null || true
        DEPLOY_ACTIVE_GATE_DIR=""
    fi
}

supervised_wrapper_cleanup() {
    local original_status=$? marker_q cleanup_status=0
    trap - EXIT HUP INT TERM
    if [[ "${SUPERVISED_WRAPPER_CHILD_PID:-}" =~ ^[1-9][0-9]*$ ]] \
        && kill -0 "$SUPERVISED_WRAPPER_CHILD_PID" 2>/dev/null; then
        # An unexpected wrapper exit must not orphan a daemon-backed mutation.
        # Waiting retains the marker/flock exclusion until the command itself
        # has returned.  A hard-killed wrapper leaves the marker behind and
        # therefore fails closed for manual recovery.
        wait "$SUPERVISED_WRAPPER_CHILD_PID" 2>/dev/null || true
    fi
    if [[ "${SUPERVISED_WRAPPER_MARKER_CREATED:-false}" == true \
        && "${SUPERVISED_WRAPPER_COMMAND_LAUNCHED:-false}" != true ]]; then
        # Cancellation before final launch is the only case where the wrapper
        # itself can prove that no mutation ever owned the marker.
        if ! remove_proven_completed_active_marker \
            "$SUPERVISED_WRAPPER_MARKER_FILE"; then
            cleanup_status=16
        fi
    elif [[ "${SUPERVISED_WRAPPER_MARKER_CREATED:-false}" == true ]]; then
        printf -v marker_q '%q' "$SUPERVISED_WRAPPER_MARKER_FILE"
        if ! MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
            "test ! -e $marker_q" >/dev/null 2>&1; then
            cleanup_status=16
        fi
    fi
    rm -f -- \
        "${SUPERVISED_WRAPPER_GATE_DIR:-}/authorized" \
        "${SUPERVISED_WRAPPER_GATE_DIR:-}/cancel" \
        "${SUPERVISED_WRAPPER_GATE_DIR:-}/command.stderr" \
        "${SUPERVISED_WRAPPER_GATE_DIR:-}/command.stdout" \
        "${SUPERVISED_WRAPPER_GATE_DIR:-}/prepared" \
        "${SUPERVISED_WRAPPER_GATE_DIR:-}/launch" 2>/dev/null || true
    rmdir -- "${SUPERVISED_WRAPPER_GATE_DIR:-}" 2>/dev/null || true
    if [[ "$cleanup_status" -ne 0 ]]; then
        exit "$cleanup_status"
    fi
    exit "$original_status"
}

run_supervised_command_guard() {
    local marker_file="$1" completion_mode="$2" completion_file="$3" user_mode="$4"
    local io_mode="$5" gate_dir="$6" status completion_status
    local stdout_size="-" stdout_sha256="-" stderr_size="-" stderr_sha256="-"
    local stdout_file="$gate_dir/command.stdout" stderr_file="$gate_dir/command.stderr"
    shift 6
    # This guard is a distinct process from the authorization wrapper.  If the
    # wrapper is SIGKILLed after launch, the guard remains attached to the real
    # synchronous command and alone clears the marker after that command
    # returns.  If the guard itself hard-crashes, the marker intentionally
    # remains for fail-closed manual recovery.
    set +e
    if [[ "$completion_mode" == wsl-ack ]]; then
        case "$io_mode" in
            capture)
                umask 077
                : >"$stdout_file"
                : >"$stderr_file"
                chmod 600 -- "$stdout_file" "$stderr_file"
                MSYS2_ARG_CONV_EXCL='*' MSYS_NO_PATHCONV=1 \
                    "$@" >"$stdout_file" 2>"$stderr_file"
                status=$?
                ;;
            quiet)
                MSYS2_ARG_CONV_EXCL='*' MSYS_NO_PATHCONV=1 \
                    "$@" >/dev/null 2>&1
                status=$?
                ;;
            stream)
                MSYS2_ARG_CONV_EXCL='*' MSYS_NO_PATHCONV=1 "$@"
                status=$?
                ;;
            *)
                return 16
                ;;
        esac
    else
        "$@"
        status=$?
    fi
    case "$completion_mode" in
        local)
            if ! remove_proven_completed_active_marker "$marker_file"; then
                return 16
            fi
            return "$status"
            ;;
        status-zero)
            # An unclassified client that returns non-zero may have lost its
            # transport after submitting a remote mutation.  Keep the marker
            # poisoned unless completion is positively known.
            if [[ "$status" -ne 0 ]]; then
                return 16
            fi
            if ! remove_proven_completed_active_marker "$marker_file"; then
                return 16
            fi
            return 0
            ;;
        wsl-ack)
            if [[ "$io_mode" == capture ]]; then
                if ! stdout_size="$(wc -c <"$stdout_file" | tr -d '[:space:]')" \
                    || ! stderr_size="$(wc -c <"$stderr_file" | tr -d '[:space:]')" \
                    || ! stdout_sha256="$(sha256sum -- "$stdout_file" | awk '{print $1}')" \
                    || ! stderr_sha256="$(sha256sum -- "$stderr_file" | awk '{print $1}')" \
                    || [[ ! "$stdout_size" =~ ^[0-9]+$ \
                        || ! "$stderr_size" =~ ^[0-9]+$ \
                        || ! "$stdout_sha256" =~ ^[0-9a-f]{64}$ \
                        || ! "$stderr_sha256" =~ ^[0-9a-f]{64}$ ]]; then
                    return 16
                fi
            fi
            if [[ "$status" -ne 0 ]]; then
                # A delayed HUP/INT/TERM can make wsl.exe return non-zero after
                # the remote command has completed and published its immutable
                # acknowledgement.  Verify that proof before deciding whether
                # the marker must remain poisoned.  The deployment still fails
                # with a transport error; only the ambiguity is cleared.
                if completion_status="$(finalize_proven_wsl_completion \
                    "$marker_file" "$completion_file" "$user_mode" "$io_mode" \
                    "$stdout_size" "$stdout_sha256" "$stderr_size" "$stderr_sha256" \
                    "$DEPLOY_WSL_DEFAULT_UID")"; then
                    if [[ "$io_mode" == capture ]]; then
                        cat -- "$stdout_file" || true
                        cat -- "$stderr_file" >&2 || true
                    fi
                    log_error "WSL transport 在有效远端完成确认后非零结束: transport_status=$status; remote_status=$completion_status"
                    return 16
                fi
                if [[ "$io_mode" == capture ]]; then
                    cat -- "$stderr_file" >&2 || true
                fi
                log_error "WSL completion acknowledgement 缺失或校验失败: transport_status=$status"
                return 16
            fi
            if ! completion_status="$(finalize_proven_wsl_completion \
                "$marker_file" "$completion_file" "$user_mode" "$io_mode" \
                "$stdout_size" "$stdout_sha256" "$stderr_size" "$stderr_sha256" \
                "$DEPLOY_WSL_DEFAULT_UID")"; then
                if [[ "$io_mode" == capture ]]; then
                    cat -- "$stderr_file" >&2 || true
                fi
                log_error "WSL completion acknowledgement 缺失或校验失败: transport_status=$status"
                return 16
            fi
            if [[ "$io_mode" == capture ]]; then
                if ! cat -- "$stdout_file"; then
                    return 16
                fi
                if ! cat -- "$stderr_file" >&2; then
                    return 16
                fi
            fi
            return "$completion_status"
            ;;
        *)
            return 16
            ;;
    esac
}

run_supervised_wrapper() {
    local parent_pid="$1" gate_dir="$2" marker_file="$3" context="$4"
    local completion_mode="$5" completion_file="$6" user_mode="$7" io_mode="$8"
    local marker_q status
    shift 8
    SUPERVISED_WRAPPER_CHILD_PID=""
    SUPERVISED_WRAPPER_GATE_DIR="$gate_dir"
    SUPERVISED_WRAPPER_MARKER_FILE="$marker_file"
    SUPERVISED_WRAPPER_MARKER_CREATED=false
    SUPERVISED_WRAPPER_COMMAND_LAUNCHED=false
    SUPERVISED_WRAPPER_PARENT_LOST=false
    trap supervised_wrapper_cleanup EXIT
    trap 'SUPERVISED_WRAPPER_PARENT_LOST=true' HUP INT TERM

    # The authorization file is written only after the parent has registered
    # this wrapper as the active supervised PID.  Before that handshake the
    # wrapper is incapable of running the mutating command.
    while [[ ! -e "$gate_dir/authorized" ]]; do
        if [[ -e "$gate_dir/cancel" ]] || ! kill -0 "$parent_pid" 2>/dev/null; then
            return 16
        fi
        sleep 0.02
    done
    if [[ "${AATS_DEPLOY_TEST_MODE:-false}" == true \
        && -n "${AATS_DEPLOY_TEST_AUTHORIZED_READY:-}" ]]; then
        : >"$AATS_DEPLOY_TEST_AUTHORIZED_READY"
        sleep "${AATS_DEPLOY_TEST_AUTHORIZED_DELAY_SECONDS:-30}"
    fi
    if [[ -e "$gate_dir/cancel" ]] || ! kill -0 "$parent_pid" 2>/dev/null; then
        return 16
    fi

    printf -v marker_q '%q' "$marker_file"
    if ! MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
        "python3 -c 'import os, sys; fd=os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.fchmod(fd, 0o600); os.close(fd)' $marker_q" >/dev/null; then
        return 16
    fi
    SUPERVISED_WRAPPER_MARKER_CREATED=true
    : >"$gate_dir/prepared"
    while [[ ! -e "$gate_dir/launch" ]]; do
        if [[ -e "$gate_dir/cancel" ]] || ! kill -0 "$parent_pid" 2>/dev/null; then
            return 16
        fi
        sleep 0.02
    done
    if [[ -e "$gate_dir/cancel" ]] || ! kill -0 "$parent_pid" 2>/dev/null; then
        return 16
    fi

    run_supervised_command_guard \
        "$marker_file" "$completion_mode" "$completion_file" "$user_mode" \
        "$io_mode" "$gate_dir" "$@" &
    SUPERVISED_WRAPPER_CHILD_PID=$!
    SUPERVISED_WRAPPER_COMMAND_LAUNCHED=true
    if [[ "${AATS_DEPLOY_TEST_MODE:-false}" == true \
        && -n "${AATS_DEPLOY_TEST_WRAPPER_LAUNCHED_READY:-}" ]]; then
        printf '%s\n' "$BASHPID" >"$AATS_DEPLOY_TEST_WRAPPER_LAUNCHED_READY"
    fi
    while kill -0 "$SUPERVISED_WRAPPER_CHILD_PID" 2>/dev/null; do
        if ! kill -0 "$parent_pid" 2>/dev/null; then
            SUPERVISED_WRAPPER_PARENT_LOST=true
        fi
        sleep 0.1
    done
    if wait "$SUPERVISED_WRAPPER_CHILD_PID"; then
        status=0
    else
        status=$?
    fi
    SUPERVISED_WRAPPER_CHILD_PID=""
    if [[ "$SUPERVISED_WRAPPER_PARENT_LOST" == true ]]; then
        return 16
    fi
    if [[ "$status" -ne 0 ]]; then
        return "$status"
    fi
    return 0
}

run_lock_supervised_wsl() {
    local context="$1" user_mode="$2" io_mode="$3" remote_command="$4"
    run_lock_supervised_external \
        "$context" --wsl-completion "$user_mode" "$io_mode" "$remote_command"
}

run_lock_supervised_external() {
    local context="$1" process_pid status gate_dir marker_file
    local completion_mode="status-zero" completion_file="" user_mode="default"
    local io_mode="stream" remote_command="" wrapped_command="" wrapped_encoded=""
    local -a supervised_command=()
    shift
    case "${1:-}" in
        --local-completion)
            completion_mode="local"
            shift
            supervised_command=("$@")
            ;;
        --wsl-completion)
            completion_mode="wsl-ack"
            user_mode="${2:-}"
            io_mode="${3:-}"
            remote_command="${4:-}"
            if [[ "$#" -ne 4 || ( "$user_mode" != default && "$user_mode" != root ) \
                || ( "$io_mode" != capture && "$io_mode" != quiet && "$io_mode" != stream ) \
                || -z "$remote_command" ]]; then
                log_error "无效的 WSL completion acknowledgement 参数: $context"
                return 16
            fi
            ;;
        *)
            supervised_command=("$@")
            ;;
    esac
    if [[ "$completion_mode" != wsl-ack && "${#supervised_command[@]}" -eq 0 ]]; then
        log_error "受监督步骤缺少命令: $context"
        return 16
    fi
    if [[ ! "$DEPLOY_WSL_DEFAULT_UID" =~ ^[0-9]+$ ]]; then
        log_error "标准部署锁未绑定可信 WSL default UID: $context"
        return 16
    fi
    if ! assert_no_owned_active_markers "$context 启动"; then
        return 16
    fi
    if [[ "$DEPLOY_SUPERVISION_POISONED" == true \
        || -n "$DEPLOY_ACTIVE_PROCESS_PID" \
        || -n "$DEPLOY_ACTIVE_MARKER_FILE" \
        || -n "$DEPLOY_ACTIVE_GATE_DIR" ]]; then
        log_error "检测到未闭合或不确定的受监督外部进程，拒绝启动: $context"
        return 16
    fi
    if ! assert_deploy_lock_held "$context 启动前"; then
        return 16
    fi
    gate_dir="$(mktemp -d "${TMPDIR:-/tmp}/aats-deploy-gate.XXXXXX")" || {
        log_error "无法建立受监督步骤授权门: $context"
        return 16
    }
    DEPLOY_ACTIVE_SEQUENCE=$((DEPLOY_ACTIVE_SEQUENCE + 1))
    marker_file="/tmp/aats-standard-deploy-active-$DEPLOY_LOCK_SCOPE-$DEPLOY_LOCK_TOKEN-$DEPLOY_ACTIVE_SEQUENCE"
    if [[ "$completion_mode" == wsl-ack ]]; then
        completion_file="${marker_file/aats-standard-deploy-active-/aats-standard-deploy-completion-}"
        if ! wrapped_command="$(build_wsl_completion_wrapped_command \
            "$remote_command" "$marker_file" "$completion_file" "$io_mode" \
            "$DEPLOY_WSL_DEFAULT_UID")"; then
            log_error "无法构造 WSL completion acknowledgement wrapper: $context"
            return 16
        fi
        if ! wrapped_encoded="$(printf '%s' "$wrapped_command" | base64 | tr -d '\r\n')" \
            || [[ -z "$wrapped_encoded" || ! "$wrapped_encoded" =~ ^[A-Za-z0-9+/=]+$ ]]; then
            log_error "无法编码 WSL completion acknowledgement wrapper: $context"
            return 16
        fi
        supervised_command=(run_wsl_completion_transport \
            "$user_mode" "$wrapped_encoded")
    fi
    DEPLOY_ACTIVE_GATE_DIR="$gate_dir"
    DEPLOY_ACTIVE_MARKER_FILE="$marker_file"
    run_supervised_wrapper "$$" "$gate_dir" "$marker_file" "$context" \
        "$completion_mode" "$completion_file" "$user_mode" "$io_mode" \
        "${supervised_command[@]}" &
    process_pid=$!
    if [[ "${AATS_DEPLOY_TEST_MODE:-false}" == true \
        && "${BASH_SOURCE[0]}" != "$0" \
        && -n "${AATS_DEPLOY_TEST_PRE_REGISTRATION_READY:-}" ]]; then
        : >"$AATS_DEPLOY_TEST_PRE_REGISTRATION_READY"
        sleep "${AATS_DEPLOY_TEST_PRE_REGISTRATION_DELAY_SECONDS:-30}"
    fi
    DEPLOY_ACTIVE_PROCESS_PID="$process_pid"
    DEPLOY_ACTIVE_PROCESS_CONTEXT="$context"
    if ! assert_deploy_lock_held "$context 授权前"; then
        terminate_active_supervised_process
        return 16
    fi
    : >"$gate_dir/authorized"
    while [[ ! -e "$gate_dir/prepared" ]]; do
        if ! kill -0 "$process_pid" 2>/dev/null; then
            break
        fi
        if [[ -z "$DEPLOY_LOCK_KEEPER_PID" ]] \
            || ! kill -0 "$DEPLOY_LOCK_KEEPER_PID" 2>/dev/null \
            || [[ -z "$DEPLOY_LOCK_HEARTBEAT_PID" ]] \
            || ! kill -0 "$DEPLOY_LOCK_HEARTBEAT_PID" 2>/dev/null; then
            terminate_active_supervised_process
            return 16
        fi
        sleep 0.02
    done
    if [[ ! -e "$gate_dir/prepared" ]] || ! assert_deploy_lock_held "$context 最终授权前"; then
        terminate_active_supervised_process
        return 16
    fi
    : >"$gate_dir/launch"
    while kill -0 "$process_pid" 2>/dev/null; do
        if [[ -z "$DEPLOY_LOCK_KEEPER_PID" ]] \
            || ! kill -0 "$DEPLOY_LOCK_KEEPER_PID" 2>/dev/null \
            || [[ -z "$DEPLOY_LOCK_HEARTBEAT_PID" ]] \
            || ! kill -0 "$DEPLOY_LOCK_HEARTBEAT_PID" 2>/dev/null; then
            log_error "WSL2 标准部署锁在活动步骤中丢失；等待受监督步骤完成证明: $context"
            terminate_active_supervised_process
            return 16
        fi
        sleep 0.2
    done
    if wait "$process_pid"; then
        status=0
    else
        status=$?
    fi
    if ! remove_active_supervision_artifacts; then
        DEPLOY_SUPERVISION_POISONED=true
        return 16
    fi
    DEPLOY_ACTIVE_PROCESS_PID=""
    DEPLOY_ACTIVE_PROCESS_CONTEXT=""
    if [[ "$status" -ne 0 ]]; then
        return "$status"
    fi
    assert_deploy_lock_held "$context 完成后"
}

run_locked_step() {
    local step_name="$1"
    shift
    assert_deploy_lock_held "$step_name 前"
    assert_no_owned_active_markers "$step_name 前"
    assert_local_docker_daemon_binding "$step_name 前"
    "$@"
    assert_no_owned_active_markers "$step_name 后"
    assert_local_docker_daemon_binding "$step_name 后"
    assert_deploy_lock_held "$step_name 后"
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
    if [[ ! "$NATS_TARGET_ENV_SNAPSHOT_PATH" =~ ^/run/aats-deploy/nats-target-[A-Za-z0-9._:-]+\.env$ \
        || ! "$NATS_TARGET_MANIFEST_SHA256" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        log_error "NATS 目标参数只读快照尚未生成或格式无效"
        return 1
    fi
    printf "AATS_RUNTIME_READINESS_GENERATION='%s' AATS_DEPLOYED_GIT_COMMIT='%s' AATS_NATS_TARGET_ENV_SNAPSHOT_PATH='%s' AATS_NATS_TARGET_MANIFEST_SHA256='%s'" \
        "$RUNTIME_READINESS_GENERATION" \
        "$DEPLOYED_GIT_COMMIT" \
        "$NATS_TARGET_ENV_SNAPSHOT_PATH" \
        "$NATS_TARGET_MANIFEST_SHA256"
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

assert_nats_target_env_snapshot() {
    local context="$1" metadata verified
    if [[ ! "$NATS_TARGET_ENV_SNAPSHOT_PATH" =~ ^/run/aats-deploy/nats-target-[A-Za-z0-9._:-]+\.env$ \
        || ! "$NATS_TARGET_MANIFEST_SHA256" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        log_error "NATS 目标参数只读快照在 $context 缺失或标识无效"
        return 1
    fi
    if ! metadata="$(wsl_run "stat -Lc '%u:%g:%a:%F' '$NATS_TARGET_ENV_SNAPSHOT_PATH' 2>/dev/null" | tr -d '\r')" \
        || [[ "$metadata" != "0:0:444:regular file" ]]; then
        log_error "NATS 目标参数只读快照在 $context 的 owner/mode/type 不可信"
        return 1
    fi
    if ! verified="$(wsl_run "cd $WSL_PROJECT && ~/aats-venv/bin/python scripts/nats_target_env_snapshot.py verify --snapshot '$NATS_TARGET_ENV_SNAPSHOT_PATH' --expected-sha256 '$NATS_TARGET_MANIFEST_SHA256'" | tr -d '\r')" \
        || [[ "$verified" != "$NATS_TARGET_MANIFEST_SHA256" ]]; then
        log_error "NATS 目标参数只读快照在 $context 未通过严格白名单/摘要校验"
        return 1
    fi
}

prepare_nats_target_env_snapshot() {
    local rendered manifest_sha256 encoded_snapshot extra snapshot_path root_script
    if [[ ! "$DEPLOY_LOCK_TOKEN" =~ ^[A-Za-z0-9._:-]{1,128}$ ]]; then
        log_error "部署锁 token 无法安全派生 NATS 目标参数快照路径"
        return 1
    fi
    if [[ "$NATS_TARGET_SNAPSHOT_RUNTIME_DIR" != "/run/aats-deploy" ]]; then
        log_error "NATS 目标参数快照运行目录偏离受管路径"
        return 1
    fi
    if ! rendered="$(wsl_run "cd $WSL_PROJECT && ~/aats-venv/bin/python scripts/nats_target_env_snapshot.py render --source \"$ENV_PROFILE_PATH\"" | tr -d '\r')" \
        || [[ "$rendered" == *$'\n'* ]]; then
        log_error "无法从 profile 生成 NATS 目标参数白名单快照"
        return 1
    fi
    IFS=$'\t' read -r manifest_sha256 encoded_snapshot extra <<<"$rendered"
    if [[ ! "$manifest_sha256" =~ ^sha256:[0-9a-f]{64}$ \
        || ! "$encoded_snapshot" =~ ^[A-Za-z0-9+/]+={0,2}$ \
        || -n "$extra" ]]; then
        log_error "NATS 目标参数快照生成器返回了无效的安全投影"
        return 1
    fi
    snapshot_path="$NATS_TARGET_SNAPSHOT_RUNTIME_DIR/nats-target-$DEPLOY_LOCK_TOKEN.env"
    printf -v root_script '%s\n' \
        'set -euo pipefail' \
        "runtime_dir='$NATS_TARGET_SNAPSHOT_RUNTIME_DIR'" \
        "target='$snapshot_path'" \
        'test ! -L "$runtime_dir"' \
        'install -d -o root -g root -m 0755 "$runtime_dir"' \
        'test "$(stat -Lc '\''%u:%g:%a:%F'\'' "$runtime_dir")" = '\''0:0:755:directory'\''' \
        'tmp=$(mktemp "$runtime_dir/.nats-target.tmp.XXXXXX")' \
        "trap 'rm -f -- \"\$tmp\"' EXIT" \
        "printf '%s' '$encoded_snapshot' | base64 --decode >\"\$tmp\"" \
        'chown root:root "$tmp"' \
        'chmod 0444 "$tmp"' \
        'mv -fT -- "$tmp" "$target"' \
        'trap - EXIT'
    if ! wsl_root_run_script "$root_script"; then
        log_error "无法创建 root-owned NATS 目标参数只读快照"
        return 1
    fi
    NATS_TARGET_ENV_SNAPSHOT_PATH="$snapshot_path"
    NATS_TARGET_MANIFEST_SHA256="$manifest_sha256"
    assert_nats_target_env_snapshot "生成后"
    log_ok "NATS 目标参数已冻结为受部署锁绑定的白名单快照"
}

cleanup_nats_target_env_snapshot() {
    local target="$NATS_TARGET_ENV_SNAPSHOT_PATH"
    local root_script
    if [[ -z "$target" ]]; then
        return 0
    fi
    if [[ ! "$target" =~ ^/run/aats-deploy/nats-target-[A-Za-z0-9._:-]+\.env$ ]]; then
        return 1
    fi
    printf -v root_script '%s\n' \
        'set -euo pipefail' \
        "target='$target'" \
        'if test -e "$target"; then' \
        '    test ! -L "$target"' \
        '    test "$(stat -Lc '\''%u:%g:%a:%F'\'' "$target")" = '\''0:0:444:regular file'\''' \
        '    rm -f -- "$target"' \
        'fi'
    if ! wsl_root_run_script "$root_script"; then
        return 1
    fi
    NATS_TARGET_ENV_SNAPSHOT_PATH=""
    NATS_TARGET_MANIFEST_SHA256=""
}

assert_wsl_checkout_clean() {
    local context="$1"
    # HEAD alone is not provenance: --skip-sync can otherwise build tracked,
    # staged, or untracked WSL files while evidence claims the clean commit.
    # Check all three classes unconditionally, including ignored-submodule
    # dirtiness, both around the build and immediately before final evidence.
    if ! wsl_run "git -C $WSL_PROJECT diff --quiet --ignore-submodules=none -- && git -C $WSL_PROJECT diff --cached --quiet --ignore-submodules=none -- && test -z \"\$(git -C $WSL_PROJECT ls-files --others --exclude-standard)\""; then
        log_error "WSL2 checkout 非 clean，无法证明镜像代码属于 declared commit；拒绝继续 $context"
        log_error "请在 WSL2 checkout 中提交或移除 tracked/staged/untracked 改动后，重新执行标准部署"
        return 19
    fi
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
    local last_index
    local -a fields

    for c in $APP_CONTAINERS; do
        # Docker only flips Health.Status after `retries` consecutive failures.
        # Keep the current failing streak and every retained ExitCode in the
        # field projection, then require the newest sample itself to succeed.
        # The 2s stability poll therefore catches a one-off failed 15s check
        # even while Docker still reports the container as healthy.
        state="$(wsl_run "docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}} {{.State.Health.FailingStreak}} {{range .State.Health.Log}}{{.ExitCode}} {{end}}{{else}}none -1 -1{{end}}' \"$c\" 2>/dev/null" || true)"
        fields=()
        read -r -a fields <<<"$state"
        if [[ "${#fields[@]}" -lt 4 \
            || "${fields[0]}" != "running" \
            || "${fields[1]}" != "healthy" \
            || "${fields[2]}" != "0" ]]; then
            return 1
        fi
        last_index=$((${#fields[@]} - 1))
        [[ "${fields[$last_index]}" == "0" ]] || return 1
    done

    return 0
}

nats_container_health_ok_since() {
    local boundary_started_ns="$1"
    if [[ ! "$boundary_started_ns" =~ ^[1-9][0-9]{18}$ ]]; then
        return 1
    fi
    # Keep the rolling Health.Log outside the immutable NATS identity
    # fingerprint, but actively reject a non-zero check after the authoritative
    # application-health boundary.  The 2s caller cadence is shorter than the
    # managed NATS 10s health interval, so a failed latest sample cannot recover
    # between observations without first being seen.
    wsl_run "cd $WSL_PROJECT && ~/aats-venv/bin/python scripts/nats_runtime_identity.py health-check --since-ns '$boundary_started_ns'" >/dev/null 2>&1
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

capture_local_docker_daemon_binding() {
    local envelope expected_sha256 encoded extra daemon_id actual_sha256
    # wsl.exe can sporadically inject NUL bytes into redirected stdout after a
    # noisy child (observed after BuildKit).  Strip only transport NUL/CR before
    # shell capture, then verify the WSL-produced SHA-256 over a base64 payload.
    # This tolerates encoding noise without accepting arbitrary byte loss.
    if ! envelope="$(
        wsl_run "set -o pipefail; cd $WSL_PROJECT && ~/aats-venv/bin/python scripts/docker_event_monitor.py daemon-binding-envelope" \
            | tr -d '\000\r'
    )" \
        || [[ -z "$envelope" || "$envelope" == *$'\n'* ]]; then
        return 1
    fi
    IFS=$'\t' read -r expected_sha256 encoded extra <<<"$envelope"
    if [[ ! "$expected_sha256" =~ ^sha256:[0-9a-f]{64}$ \
        || ! "$encoded" =~ ^[A-Za-z0-9+/]+={0,2}$ \
        || -n "$extra" ]]; then
        return 1
    fi
    if ! daemon_id="$(printf '%s' "$encoded" | base64 --decode)" \
        || [[ ! "$daemon_id" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$ ]]; then
        return 1
    fi
    if ! actual_sha256="$(printf '%s' "$daemon_id" | sha256sum | cut -d' ' -f1)" \
        || [[ "sha256:$actual_sha256" != "$expected_sha256" ]]; then
        return 1
    fi
    printf '%s\n' "$daemon_id"
}

establish_local_docker_daemon_binding() {
    local daemon_id
    log_info "绑定标准部署到 WSL2 本地 Docker daemon..."
    if ! daemon_id="$(capture_local_docker_daemon_binding)"; then
        log_error "Docker daemon 不是默认本地 socket，或 CLI/socket 身份不一致"
        return 1
    fi
    if [[ ! "$daemon_id" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$ ]]; then
        log_error "Docker daemon identity 无效"
        return 1
    fi
    DOCKER_DAEMON_ID="$daemon_id"
    log_ok "标准部署已绑定唯一 WSL2 本地 Docker daemon"
}

assert_local_docker_daemon_binding() {
    local context="$1" current_id
    if [[ -z "$DOCKER_DAEMON_ID" ]]; then
        return 0
    fi
    if ! current_id="$(capture_local_docker_daemon_binding)" \
        || [[ "$current_id" != "$DOCKER_DAEMON_ID" ]]; then
        log_error "Docker daemon 绑定在 $context 发生漂移；拒绝继续"
        return 17
    fi
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
            run_lock_supervised_external \
                "Git commit" --local-completion git commit -m "$COMMIT_MSG"
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

    local source_branch source_head source_mount target_path sync_command
    log_info "Step 2/8: 同步代码到 WSL2..."
    source_branch="$(git -C "$PROJECT_ROOT" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
    source_head="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
    if [[ ! "$source_head" =~ ^[0-9a-fA-F]{40}$ ]]; then
        log_error "Windows 源仓库 branch/HEAD 无法形成安全同步输入"
        return 1
    fi
    if [[ -n "$source_branch" ]] \
        && ! git -C "$PROJECT_ROOT" check-ref-format --branch "$source_branch" >/dev/null 2>&1; then
        log_error "Windows 源仓库 branch/HEAD 无法形成安全同步输入"
        return 1
    fi
    if ! source_mount="$(windows_path_to_wsl_mount "$PROJECT_ROOT")"; then
        log_error "无法把 Windows 源路径转换为 WSL mount 路径"
        return 1
    fi
    if ! target_path="$(wsl_run "cd $WSL_PROJECT && pwd -P" | tr -d '\r')" \
        || [[ "$target_path" != /* || "$target_path" == *$'\n'* ]]; then
        log_error "无法解析可信 WSL2 checkout 绝对路径"
        return 1
    fi
    if ! sync_command="$(build_wsl_checkout_sync_command \
        "$source_mount" "$target_path" "$source_branch" "$source_head")"; then
        log_error "无法构造单事务 WSL2 Git 同步命令"
        return 1
    fi
    # The complete Git transaction executes behind one WSL-side completion
    # acknowledgement.  Semantic Git refusals therefore clean their marker;
    # only an ambiguous client/transport completion remains fail-closed.
    wsl_run "$sync_command"
    log_ok "同步完成"
}

app_container_state_is_stopped() {
    case "$1" in
        exited|dead)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

owned_app_container_id() {
    local container="$1" binding container_id project service extra
    if ! binding="$(wsl_run "docker inspect --format '{{.Id}}|{{index .Config.Labels \"com.docker.compose.project\"}}|{{index .Config.Labels \"com.docker.compose.service\"}}' '$container'" | tr -d '\r')"; then
        log_error "无法读取应用容器安全归属；拒绝产生副作用: $container"
        return 1
    fi
    IFS='|' read -r container_id project service extra <<<"$binding"
    if [[ ! "$container_id" =~ ^[0-9a-f]{64}$ \
        || "$project" != "aats-dev" \
        || "$service" != "$container" \
        || -n "$extra" ]]; then
        log_error "同名应用容器不属于受管 aats-dev service；拒绝产生副作用: $container"
        return 1
    fi
    printf '%s\n' "$container_id"
}

capture_app_quiescence_snapshot() {
    local context="$1"
    local existing_containers container details state snapshot
    if ! existing_containers="$(wsl_run "docker ps -a --format '{{.Names}}'" | tr -d '\r')"; then
        log_error "无法枚举应用容器状态；拒绝继续 $context"
        return 1
    fi
    snapshot=""
    for container in $ALL_KNOWN_APP_CONTAINERS; do
        if ! printf '%s\n' "$existing_containers" | grep -Fxq -- "$container"; then
            snapshot+="$container|not-found|-|-|-|-"$'\n'
            continue
        fi
        if ! details="$(wsl_run "docker inspect --format '{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}|{{.State.FinishedAt}}|{{.RestartCount}}' '$container'" | tr -d '\r')"; then
            log_error "应用容器 inspect 失败；拒绝继续 $context: $container"
            return 1
        fi
        state="${details#*|}"
        state="${state%%|*}"
        if ! app_container_state_is_stopped "$state"; then
            log_error "应用容器未处于 exited/dead；拒绝继续 $context: $container ($state)"
            return 1
        fi
        snapshot+="$container|$details"$'\n'
    done
    printf '%s' "$snapshot"
}

capture_new_app_quiescence_boundary() {
    local context="$1"
    if ! APP_QUIESCENCE_SNAPSHOT="$(capture_app_quiescence_snapshot "$context")"; then
        exit 12
    fi
}

assert_app_quiescence_unchanged() {
    local context="$1"
    local current_snapshot
    if [[ -z "$APP_QUIESCENCE_SNAPSHOT" ]]; then
        log_error "应用静止基线缺失；拒绝继续 $context"
        exit 12
    fi
    if ! current_snapshot="$(capture_app_quiescence_snapshot "$context")"; then
        exit 12
    fi
    if [[ "$current_snapshot" != "$APP_QUIESCENCE_SNAPSHOT" ]]; then
        log_error "应用容器静止指纹在发布边界内发生变化；拒绝继续 $context"
        log_error "可能存在并发或人工 start/stop/recreate；必须保留 NATS 状态并重新执行完整标准部署"
        exit 15
    fi
}

capture_nats_cutover_bootstrap_fingerprint() {
    local snapshot fingerprint container_id restart_count volume_fingerprint extra
    if ! snapshot="$(wsl_run "cd $WSL_PROJECT && ~/aats-venv/bin/python scripts/nats_runtime_identity.py snapshot --format tsv")"; then
        log_error "无法通过共享事实投影读取首次 NATS baseline；拒绝继续"
        return 20
    fi
    snapshot="${snapshot//$'\r'/}"
    IFS=$'\t' read -r fingerprint container_id restart_count volume_fingerprint extra <<<"$snapshot"
    if [[ ! "$fingerprint" =~ ^sha256:[0-9a-f]{64}$ \
        || ! "$container_id" =~ ^[0-9a-f]{64}$ \
        || ! "$restart_count" =~ ^[0-9]+$ \
        || ! "$volume_fingerprint" =~ ^sha256:[0-9a-f]{64}$ \
        || -n "$extra" ]]; then
        log_error "共享 NATS baseline 输出不完整；拒绝继续"
        return 20
    fi
    if [[ "$NATS_CUTOVER_BOOTSTRAP_MODE" == "proven_fresh_install" \
        && "$restart_count" != "0" ]]; then
        log_error "fresh NATS bootstrap 已存在重启历史；拒绝伪报 proven fresh"
        return 20
    fi
    NATS_CUTOVER_BASELINE_FINGERPRINT="$fingerprint"
    NATS_CUTOVER_VOLUME_FINGERPRINT="$volume_fingerprint"
}

read_nats_cutover_volume_fingerprint() {
    local fingerprint
    if ! fingerprint="$(wsl_run "cd $WSL_PROJECT && ~/aats-venv/bin/python scripts/nats_runtime_identity.py volume-fingerprint")"; then
        return 20
    fi
    fingerprint="${fingerprint//$'\r'/}"
    if [[ ! "$fingerprint" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        return 20
    fi
    printf '%s\n' "$fingerprint"
}

ensure_nats_cutover_preflight_infra_up() {
    log_info "在不重建旧 NATS 的前提下建立首次只读 cutover baseline..."
    local existing binding container_id state image project service
    local mount_type mount_name mount_rw binding_extra
    local initial_volume_fingerprint final_container_id volumes candidate_volumes
    local fresh_claim claimed_volume_fingerprint final_fresh_claim
    if ! existing="$(wsl_run "docker ps -a --format '{{.Names}}'" | tr -d '\r')"; then
        log_error "无法枚举 NATS 容器；拒绝在首次 baseline 前修改基础设施"
        return 20
    fi

    if printf '%s\n' "$existing" | grep -Fxq -- "aats-nats"; then
        if ! binding="$(wsl_run "docker inspect --format '{{.Id}}|{{.State.Status}}|{{.Config.Image}}|{{index .Config.Labels \"com.docker.compose.project\"}}|{{index .Config.Labels \"com.docker.compose.service\"}}|{{range .Mounts}}{{if eq .Destination \"/data\"}}{{.Type}}|{{.Name}}|{{.RW}}|{{end}}{{end}}' aats-nats" | tr -d '\r')"; then
            log_error "无法原子读取现有 aats-nats 身份、状态和持久卷绑定；拒绝继续"
            return 20
        fi
        IFS='|' read -r container_id state image project service mount_type mount_name mount_rw binding_extra <<<"$binding"
        if [[ ! "$container_id" =~ ^[0-9a-f]{64}$ \
            || "$image" != "$NATS_EXPECTED_IMAGE" \
            || "$project" != "aats-dev" \
            || "$service" != "nats" \
            || "$mount_type" != "volume" \
            || "$mount_name" != "aats-dev_nats_data" \
            || "$mount_rw" != "true" \
            || -n "$binding_extra" ]]; then
            log_error "现有 aats-nats 镜像、Compose 归属或可写标准持久卷不匹配；拒绝启动、重建或建立伪 baseline"
            return 20
        fi
        if ! initial_volume_fingerprint="$(read_nats_cutover_volume_fingerprint)"; then
            log_error "无法固定现有 NATS 持久卷初始身份；拒绝继续"
            return 20
        fi
        case "$state" in
            running)
                ;;
            created|exited)
                log_error "现有 NATS 处于 $state；首次只读 baseline 前禁止自动启动任何已停止容器"
                log_error "请人工核验镜像内容、命令、配置 bind、网络与数据卷后恢复同一容器，再重跑部署"
                return 20
                ;;
            *)
                log_error "现有 aats-nats 状态不可安全保留: $state"
                return 20
                ;;
        esac
        if ! wsl_run "for attempt in \$(seq 1 90); do if [[ \"\$(docker inspect --format '{{.Id}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' '$container_id' 2>/dev/null)\" == '$container_id|running|healthy' ]]; then exit 0; fi; sleep 1; done; exit 1"; then
            log_error "现有 aats-nats 未在 90 秒内恢复健康；保持原容器和持久卷，拒绝 recreate"
            return 20
        fi
        NATS_CUTOVER_BOOTSTRAP_MODE="existing_container_preserved"
        capture_nats_cutover_bootstrap_fingerprint || return $?
        if ! final_container_id="$(wsl_run "docker inspect --format '{{.Id}}' aats-nats" | tr -d '\r')" \
            || [[ "$final_container_id" != "$container_id" ]] \
            || [[ "$NATS_CUTOVER_VOLUME_FINGERPRINT" != "$initial_volume_fingerprint" ]]; then
            log_error "现有 NATS 容器或持久卷在 baseline 固定窗口内被替换；拒绝伪报 preserved"
            return 20
        fi
        log_ok "已保留现有正在运行的 aats-nats；未执行启动或 Compose recreate"
        return 0
    fi

    # Missing container + existing standard volume is UNKNOWN, not a fresh
    # install: auto-creating a new container before the first snapshot could
    # hide a broken detach/config migration or mount the wrong data.
    if ! volumes="$(wsl_run "docker volume ls --format '{{.Name}}'")"; then
        log_error "无法枚举 Docker volumes；拒绝把 UNKNOWN 误判为 fresh install"
        return 20
    fi
    volumes="${volumes//$'\r'/}"
    if printf '%s\n' "$volumes" | grep -Eq -- '(^|_)nats_data$'; then
        log_error "aats-nats 容器缺失但候选 NATS 持久卷仍存在；状态 UNKNOWN，禁止首次 baseline 前自动重建"
        log_error "请人工审查 volume/container 归属并从可验证状态恢复后重跑"
        return 20
    fi

    # Atomically claim the exact Compose volume with this deployment token.
    # `docker volume create` is idempotent by name, so a concurrent/history
    # volume wins the name but cannot acquire our label; the following exact
    # label check therefore turns the TOCTOU race into a closed failure.
    if ! wsl_run "docker volume create --label com.docker.compose.project=aats-dev --label com.docker.compose.volume=nats_data --label com.aats.bootstrap_lock='$DEPLOY_LOCK_TOKEN' aats-dev_nats_data >/dev/null"; then
        log_error "无法原子声明 fresh NATS 持久卷；拒绝继续"
        return 20
    fi
    fresh_claim="$(wsl_run "docker volume inspect --format '{{index .Labels \"com.aats.bootstrap_lock\"}}' aats-dev_nats_data" | tr -d '\r')"
    if [[ "$fresh_claim" != "$DEPLOY_LOCK_TOKEN" ]]; then
        log_error "fresh NATS 持久卷声明 token 不匹配；检测到并发或历史卷，拒绝继续"
        return 20
    fi
    if ! candidate_volumes="$(wsl_run "docker volume ls --format '{{.Name}}'" | tr -d '\r')" \
        || [[ "$(printf '%s\n' "$candidate_volumes" | grep -E '(^|_)nats_data$' || true)" != "aats-dev_nats_data" ]]; then
        log_error "fresh NATS 声明后出现额外候选持久卷；状态 UNKNOWN，拒绝继续"
        return 20
    fi
    if ! claimed_volume_fingerprint="$(read_nats_cutover_volume_fingerprint)"; then
        log_error "无法固定本次 fresh NATS 持久卷声明身份；拒绝继续"
        return 20
    fi

    if ! wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && docker compose -f docker-compose.yml --env-file $WSL2_ENV_FILE up -d --wait --wait-timeout 90 --no-deps nats"; then
        log_error "可证明 fresh install 的 NATS-only bootstrap 失败；终止部署"
        return 20
    fi
    NATS_CUTOVER_BOOTSTRAP_MODE="proven_fresh_install"
    capture_nats_cutover_bootstrap_fingerprint || return $?
    final_fresh_claim="$(wsl_run "docker volume inspect --format '{{index .Labels \"com.aats.bootstrap_lock\"}}' aats-dev_nats_data" | tr -d '\r')"
    if [[ "$final_fresh_claim" != "$DEPLOY_LOCK_TOKEN" \
        || "$NATS_CUTOVER_VOLUME_FINGERPRINT" != "$claimed_volume_fingerprint" ]]; then
        log_error "fresh NATS 持久卷在声明至 baseline 固定窗口内被替换；拒绝伪报 proven fresh"
        return 20
    fi
    log_ok "NATS 容器与持久卷均不存在；已执行 NATS-only fresh bootstrap"
}

cleanup_deployment_lifecycle_monitor() {
    local control_dir="$LIFECYCLE_MONITOR_CONTROL_DIR"
    local token="$LIFECYCLE_MONITOR_TOKEN"
    local pid="$LIFECYCLE_MONITOR_PID"
    local cleanup_status=0
    if [[ -z "$control_dir" && -z "$token" && -z "$pid" ]]; then
        return 0
    fi
    if [[ ! "$control_dir" =~ ^/tmp/aats-docker-event-monitor-[A-Za-z0-9._-]{1,128}$ \
        || ! "$token" =~ ^[A-Za-z0-9._:-]{1,128}$ \
        || ! "$pid" =~ ^[1-9][0-9]*$ ]]; then
        log_error "生命周期观察器清理元数据无效；拒绝对不确定 PID 或路径执行操作"
        return 1
    fi
    if ! MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "
set -euo pipefail
control_dir='$control_dir'
pid='$pid'
token='$token'
if kill -0 \"\$pid\" 2>/dev/null; then
    command_line=\$(tr '\\0' ' ' <\"/proc/\$pid/cmdline\" 2>/dev/null || true)
    case \"\$command_line\" in
        *scripts/docker_event_monitor.py*daemon*--control-dir*\"\$control_dir\"*--token*\"\$token\"*) ;;
        *) exit 17 ;;
    esac
    : >\"\$control_dir/cancel\"
    for _attempt in \$(seq 1 60); do
        kill -0 \"\$pid\" 2>/dev/null || break
        sleep 0.05
    done
    if kill -0 \"\$pid\" 2>/dev/null; then
        kill -TERM \"\$pid\"
        for _attempt in \$(seq 1 40); do
            kill -0 \"\$pid\" 2>/dev/null || break
            sleep 0.05
        done
    fi
    kill -0 \"\$pid\" 2>/dev/null && exit 18
fi
rm -f -- \
    \"\$control_dir/ready.json\" \
    \"\$control_dir/seal-request.json\" \
    \"\$control_dir/sealed.json\" \
    \"\$control_dir/failed.json\" \
    \"\$control_dir/cancel\" \
    \"\$control_dir.log\"
rmdir -- \"\$control_dir\" 2>/dev/null || true
" >/dev/null 2>&1; then
        cleanup_status=1
    fi
    if [[ "$cleanup_status" -eq 0 ]]; then
        LIFECYCLE_MONITOR_CONTROL_DIR=""
        LIFECYCLE_MONITOR_TOKEN=""
        LIFECYCLE_MONITOR_PID=""
        LIFECYCLE_MONITOR_STARTED_NS=""
    fi
    return "$cleanup_status"
}

start_deployment_lifecycle_monitor() {
    log_info "启动跨阶段 Docker 生命周期实时观察窗..."
    if [[ -n "$LIFECYCLE_MONITOR_CONTROL_DIR" \
        || -n "$LIFECYCLE_MONITOR_PID" ]]; then
        log_error "检测到未闭合的生命周期观察器，拒绝重复启动"
        return 1
    fi
    local control_dir token monitor_pid started_ns container_args container
    local max_runtime_seconds launch_output
    control_dir="/tmp/aats-docker-event-monitor-$DEPLOY_LOCK_TOKEN"
    token="$DEPLOY_LOCK_TOKEN"
    max_runtime_seconds=$((HEALTH_TIMEOUT + 180))
    if [[ "$max_runtime_seconds" -gt 3600 ]]; then
        max_runtime_seconds=3600
    fi
    container_args=""
    for container in $ALL_KNOWN_APP_CONTAINERS aats-nats; do
        container_args="$container_args --container '$container'"
    done
    LIFECYCLE_MONITOR_CONTROL_DIR="$control_dir"
    LIFECYCLE_MONITOR_TOKEN="$token"
    if ! launch_output="$(wsl_run "
cd $WSL_PROJECT
umask 077
nohup ~/aats-venv/bin/python scripts/docker_event_monitor.py daemon \
    --control-dir '$control_dir' \
    --token '$token' \
    $container_args \
    --deployment-lock-id '$DEPLOY_LOCK_TOKEN' \
    --runtime-readiness-generation '$RUNTIME_READINESS_GENERATION' \
    --deployed-commit '$DEPLOYED_GIT_COMMIT' \
    --max-runtime-seconds '$max_runtime_seconds' \
    >'$control_dir.log' 2>&1 </dev/null &
printf '%s\\n' \$!
")"; then
        log_error "无法启动 Docker 生命周期实时观察器"
        return 1
    fi
    monitor_pid="$(printf '%s\n' "$launch_output" | tr -d '\r' | tail -1)"
    if [[ ! "$monitor_pid" =~ ^[1-9][0-9]*$ ]]; then
        log_error "生命周期观察器未返回可信 WSL PID"
        return 1
    fi
    LIFECYCLE_MONITOR_PID="$monitor_pid"
    if ! started_ns="$(wsl_run "
cd $WSL_PROJECT
for _attempt in \$(seq 1 150); do
    if ! kill -0 '$monitor_pid' 2>/dev/null; then exit 19; fi
    if ~/aats-venv/bin/python scripts/docker_event_monitor.py ready \
        --control-dir '$control_dir' \
        --token '$token' \
        $container_args \
        --deployment-lock-id '$DEPLOY_LOCK_TOKEN' \
        --runtime-readiness-generation '$RUNTIME_READINESS_GENERATION' \
        --deployed-commit '$DEPLOYED_GIT_COMMIT' 2>/dev/null; then
        exit 0
    fi
    sleep 0.1
done
exit 20
" | tr -d '\r' | tail -1)"; then
        log_error "生命周期观察器未在期限内进入 ready"
        cleanup_deployment_lifecycle_monitor || true
        return 1
    fi
    if [[ ! "$started_ns" =~ ^[1-9][0-9]{18}$ ]]; then
        log_error "生命周期观察器 ready 边界无效"
        cleanup_deployment_lifecycle_monitor || true
        return 1
    fi
    LIFECYCLE_MONITOR_STARTED_NS="$started_ns"
    log_ok "Docker 生命周期实时观察窗已就绪"
}

run_nats_durable_cutover_preflight() {
    local stage="$1" previous_path="${2:-}"
    log_info "执行只读 NATS durable ACK-window cutover preflight: $stage..."
    local preflight_output evidence_path previous_arg bootstrap_arg
    if ! assert_nats_target_env_snapshot "$stage preflight 前"; then
        return 1
    fi
    previous_arg=""
    bootstrap_arg=""
    if [[ -n "$previous_path" ]]; then
        previous_arg=" --previous-preflight '$previous_path'"
    fi
    if [[ "$stage" == "pre_full_down" ]]; then
        if [[ "$NATS_CUTOVER_BOOTSTRAP_MODE" != "existing_container_preserved" \
            && "$NATS_CUTOVER_BOOTSTRAP_MODE" != "proven_fresh_install" ]]; then
            log_error "首次 NATS baseline 缺少可信 bootstrap mode；拒绝继续"
            return 1
        fi
        if [[ ! "$NATS_CUTOVER_BASELINE_FINGERPRINT" =~ ^sha256:[0-9a-f]{64}$ ]]; then
            log_error "首次 NATS baseline 缺少可信容器指纹；拒绝继续"
            return 1
        fi
        if [[ ! "$NATS_CUTOVER_VOLUME_FINGERPRINT" =~ ^sha256:[0-9a-f]{64}$ ]]; then
            log_error "首次 NATS baseline 缺少可信持久卷指纹；拒绝继续"
            return 1
        fi
        bootstrap_arg=" --nats-bootstrap-mode '$NATS_CUTOVER_BOOTSTRAP_MODE' --nats-baseline-fingerprint '$NATS_CUTOVER_BASELINE_FINGERPRINT' --nats-volume-fingerprint '$NATS_CUTOVER_VOLUME_FINGERPRINT'"
    fi
    if ! preflight_output="$(wsl_run "cd $WSL_PROJECT && ~/aats-venv/bin/python scripts/check_nats_durable_cutover.py --generation '$RUNTIME_READINESS_GENERATION' --deployment-lock-id '$DEPLOY_LOCK_TOKEN' --deployed-commit '$DEPLOYED_GIT_COMMIT' --target-env-file '$NATS_TARGET_ENV_SNAPSHOT_PATH' --stage '$stage'$bootstrap_arg$previous_arg")"; then
        evidence_path="$(printf '%s\n' "$preflight_output" | tr -d '\r' | tail -1)"
        if [[ -n "$evidence_path" ]]; then
            log_error "NATS cutover preflight 失败；证据: $evidence_path"
        else
            log_error "NATS cutover preflight 失败，且未返回证据路径"
        fi
        return 1
    fi
    evidence_path="$(printf '%s\n' "$preflight_output" | tr -d '\r' | tail -1)"
    if [[ -z "$evidence_path" ]]; then
        log_error "NATS cutover preflight 未返回证据路径"
        return 1
    fi
    case "$stage" in
        pre_full_down)
            NATS_CUTOVER_PREFLIGHT_BEFORE_EVIDENCE_PATH="$evidence_path"
            ;;
        post_infra_pre_app_up)
            NATS_CUTOVER_PREFLIGHT_AFTER_EVIDENCE_PATH="$evidence_path"
            ;;
        *)
            log_error "未知 NATS cutover preflight stage: $stage"
            return 1
            ;;
    esac
    log_ok "NATS cutover preflight 通过；证据: $evidence_path"
}

require_nats_durable_cutover_preflight() {
    local context="$1" stage="$2" previous_path="${3:-}"
    assert_deploy_lock_held "$context lock 前"
    assert_app_quiescence_unchanged "$context preflight 前"
    if ! run_nats_durable_cutover_preflight "$stage" "$previous_path"; then
        log_error "NATS durable cutover 未取得安全证据；保持 NATS/Redis/Postgres 在线并终止部署"
        log_error "唯一恢复路径：保留证据与 NATS 持久状态；仅 outstanding 阻断时，经人工批准后使用匹配旧版本消费者自然 drain 至 0 再重跑。immutable drift 必须人工 release review；禁止自动 ACK/delete/update/recreate/reset/purge"
        exit 9
    fi
    assert_app_quiescence_unchanged "$context preflight 后"
    assert_deploy_lock_held "$context lock 后"
}

step_down() {
    log_info "Step 4/8: 停止旧服务..."
    local env_prefix
    env_prefix="$(compose_env_prefix)"
    # 按所有受支持 profile 的应用并集停机，而不是只停目标 profile。否则
    # derivatives -> spot 会遗留 collector，令 full-down/cutover 证据失真。
    # 名称是受版本控制的固定 allowlist；健康/evidence 仍使用目标 APP_CONTAINERS。
    local existing_containers container container_id container_ids_to_stop
    if ! existing_containers="$(wsl_run "docker ps -a --format '{{.Names}}'" | tr -d '\r')"; then
        log_error "无法枚举应用容器状态；拒绝关闭协调基础设施"
        exit 10
    fi
    container_ids_to_stop=""
    for container in $ALL_KNOWN_APP_CONTAINERS; do
        if printf '%s\n' "$existing_containers" | grep -Fxq -- "$container"; then
            if ! container_id="$(owned_app_container_id "$container")"; then
                exit 10
            fi
            container_ids_to_stop="$container_ids_to_stop $container_id"
        fi
    done
    if [[ -n "${container_ids_to_stop// /}" ]]; then
        wsl_run "docker stop --time 15 $container_ids_to_stop"
    fi
    capture_new_app_quiescence_boundary "首次 cutover"
    ensure_nats_cutover_preflight_infra_up
    require_nats_durable_cutover_preflight "full-down 前" "pre_full_down"
    assert_deploy_lock_held "full-down 紧前"
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && ${env_prefix:+env $env_prefix }docker compose $COMPOSE_CMD_ARGS down --timeout 5"
    capture_new_app_quiescence_boundary "full-down 后"
    log_ok "旧服务已停止"
}

step_build() {
    log_info "Step 3/8: 构建新镜像${NO_CACHE:+（无缓存）}..."
    local env_prefix
    env_prefix="$(compose_env_prefix)"
    assert_wsl_checkout_clean "镜像构建前"
    wsl_run \
        "cd $WSL_PROJECT/$DEPLOY_DIR && ${env_prefix:+env $env_prefix }docker compose $COMPOSE_CMD_ARGS build $NO_CACHE" \
        stream
    assert_wsl_checkout_clean "镜像构建后"
    log_ok "镜像构建完成"
}

step_prune() {
    log_info "Step 5/8: 清理悬空镜像..."
    local pruned
    # Image-prune failure is non-fatal, but a local supervision/lock failure is
    # never suppressible.  Keep only the remote Docker command best-effort.
    pruned=$(wsl_run "docker image prune -f 2>/dev/null || true")
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

    # Keep credential values inside WSL and stdin; the Windows command line
    # contains only this static program.  wsl_run places the whole mutating
    # child tree under the deployment lock supervisor.
    wsl_run "set -euo pipefail
cd $WSL_PROJECT
PG_USER=\$(grep '^POSTGRES_USER=' \"$WSL2_ENV_FILE\" | cut -d= -f2-)
PG_PW=\$(grep '^POSTGRES_PASSWORD=' \"$WSL2_ENV_FILE\" | cut -d= -f2-)
docker exec -i aats-postgres psql \\
    -v ON_ERROR_STOP=1 \\
    -v pg_user=\"\$PG_USER\" \\
    -v pg_password=\"\$PG_PW\" \\
    -U \"\$PG_USER\" \\
    -d aats >/dev/null 2>&1 <<'SQLEOF'
SET password_encryption = 'scram-sha-256';
ALTER USER :\"pg_user\" PASSWORD :'pg_password';
SQLEOF"

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
    if [[ -z "$LIFECYCLE_MONITOR_PID" \
        || ! "$LIFECYCLE_MONITOR_STARTED_NS" =~ ^[1-9][0-9]{18}$ ]]; then
        log_error "应用启动前缺少已就绪的跨阶段生命周期观察器"
        exit 11
    fi
    if ! assert_nats_target_env_snapshot "app-up 紧前"; then
        exit 11
    fi
    APP_UP_AUTHORIZED_NS="$(wsl_run "date +%s%N" | tr -d '\r')"
    if [[ ! "$APP_UP_AUTHORIZED_NS" =~ ^[1-9][0-9]{18}$ \
        || "$APP_UP_AUTHORIZED_NS" -lt "$LIFECYCLE_MONITOR_STARTED_NS" ]]; then
        log_error "应用启动授权时间边界无效"
        exit 11
    fi
    wsl_run "cd $WSL_PROJECT/$DEPLOY_DIR && ${env_prefix:+env $env_prefix }docker compose $COMPOSE_CMD_ARGS up -d"
    if ! assert_nats_target_env_snapshot "app-up 返回后"; then
        exit 11
    fi
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
            local boundary_started_ns boundary_fingerprint required_args c
            local collector_output collector_args collector_name collector_epoch
            local liquidation_epoch="" microstructure_epoch="" collector_capture_ok=true
            collector_args=""
            if [[ "$PROFILE" == "derivatives" ]]; then
                if ! collector_output="$(wsl_run "cd $WSL_PROJECT && ~/aats-venv/bin/python scripts/capture_deployment_collector_heartbeats.py --profile derivatives" | tr -d '\r')"; then
                    collector_capture_ok=false
                else
                    while IFS='=' read -r collector_name collector_epoch; do
                        if [[ ! "$collector_epoch" =~ ^[1-9][0-9]{0,11}$ ]]; then
                            collector_capture_ok=false
                            break
                        fi
                        case "$collector_name" in
                            aats-liquidations-daemon)
                                [[ -z "$liquidation_epoch" ]] || collector_capture_ok=false
                                liquidation_epoch="$collector_epoch"
                                ;;
                            aats-microstructure-collector)
                                [[ -z "$microstructure_epoch" ]] || collector_capture_ok=false
                                microstructure_epoch="$collector_epoch"
                                ;;
                            *)
                                collector_capture_ok=false
                                ;;
                        esac
                    done <<<"$collector_output"
                    if [[ -z "$liquidation_epoch" || -z "$microstructure_epoch" ]]; then
                        collector_capture_ok=false
                    fi
                    collector_args=" --collector-heartbeat-epoch 'aats-liquidations-daemon=$liquidation_epoch' --collector-heartbeat-epoch 'aats-microstructure-collector=$microstructure_epoch'"
                fi
            fi
            boundary_started_ns=""
            if [[ "$collector_capture_ok" == true ]]; then
                boundary_started_ns="$(wsl_run "date +%s%N" | tr -d '\r')"
            fi
            required_args=""
            for c in $APP_CONTAINERS; do
                required_args="$required_args --required-container '$c'"
            done
            if [[ "$boundary_started_ns" =~ ^[1-9][0-9]{18}$ ]] \
                && boundary_fingerprint="$(wsl_run "cd $WSL_PROJECT && ~/aats-venv/bin/python scripts/capture_deployment_health_boundary.py --profile '$PROFILE' --runtime-readiness-generation '$RUNTIME_READINESS_GENERATION' --deployed-commit '$DEPLOYED_GIT_COMMIT' --nats-target-manifest-sha256 '$NATS_TARGET_MANIFEST_SHA256' $required_args" | tr -d '\r' | tail -1)" \
                && [[ "$boundary_fingerprint" =~ ^sha256:[0-9a-f]{64}$ ]] \
                && nats_container_health_ok_since "$boundary_started_ns" \
                && gateway_health_ok "$port" \
                && all_required_app_containers_healthy; then
                APP_HEALTH_BOUNDARY_STARTED_NS="$boundary_started_ns"
                APP_HEALTH_BOUNDARY_FINGERPRINT="$boundary_fingerprint"
                APP_COLLECTOR_HEARTBEAT_ARGS="$collector_args"
                log_ok "应用健康检查通过并固定连续性边界 (gateway port $port, containers: $APP_CONTAINERS, ${elapsed}s)"
                return 0
            fi
            if [[ "$DEPLOY_SUPERVISION_POISONED" == true ]]; then
                return 16
            fi
            log_warn "健康状态在连续性边界固定期间发生变化；继续等待稳定窗口"
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

observe_app_stability_window() {
    log_info "观察应用稳定窗口（${APP_STABILITY_WINDOW_SECONDS}s）..."
    local port elapsed=0 interval=2
    port=$(wsl_run "grep -h '^AATS_API_PORT=' \"$ENV_PROFILE_PATH\" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '\"'" || echo "")
    port="${port:-8000}"
    while [[ "$elapsed" -lt "$APP_STABILITY_WINDOW_SECONDS" ]]; do
        if ! gateway_health_ok "$port" \
            || ! all_required_app_containers_healthy \
            || ! nats_container_health_ok_since "$APP_HEALTH_BOUNDARY_STARTED_NS"; then
            log_error "应用在正式稳定观察窗口内失去健康状态；拒绝写入成功证据"
            return 17
        fi
        sleep "$interval"
        elapsed=$((elapsed + interval))
    done
    if ! gateway_health_ok "$port" \
        || ! all_required_app_containers_healthy \
        || ! nats_container_health_ok_since "$APP_HEALTH_BOUNDARY_STARTED_NS"; then
        log_error "应用在稳定观察窗口结束时未保持健康；拒绝写入成功证据"
        return 17
    fi
    log_ok "应用已连续通过 ${APP_STABILITY_WINDOW_SECONDS}s 主动健康观察"
}

write_deployment_evidence() {
    log_info "写入模拟部署证据包..."
    assert_wsl_checkout_clean "最终部署证据生成前"
    if ! assert_nats_target_env_snapshot "最终部署证据生成前"; then
        exit 6
    fi

    local required_args=""
    local c
    for c in $APP_CONTAINERS; do
        required_args="$required_args --required-container '$c'"
    done

    if [[ ! "$APP_HEALTH_BOUNDARY_STARTED_NS" =~ ^[1-9][0-9]{18}$ \
        || ! "$APP_HEALTH_BOUNDARY_FINGERPRINT" =~ ^sha256:[0-9a-f]{64}$ \
        || ! "$APP_UP_AUTHORIZED_NS" =~ ^[1-9][0-9]{18}$ \
        || -z "$LIFECYCLE_MONITOR_CONTROL_DIR" \
        || -z "$LIFECYCLE_MONITOR_TOKEN" ]]; then
        log_error "缺少可信应用健康连续性边界；拒绝写入部署成功证据"
        exit 6
    fi
    DEPLOYMENT_EVIDENCE_PATH="$(wsl_run "cd $WSL_PROJECT && ~/aats-venv/bin/python scripts/write_deployment_evidence.py --repo-root $WSL_PROJECT --profile '$PROFILE' --overlay '$COMPOSE_OVERLAY' --schema-job-status passed --runtime-readiness-generation '$RUNTIME_READINESS_GENERATION' --deployment-lock-id '$DEPLOY_LOCK_TOKEN' --deployed-commit '$DEPLOYED_GIT_COMMIT' --lifecycle-monitor-control-dir '$LIFECYCLE_MONITOR_CONTROL_DIR' --lifecycle-monitor-token '$LIFECYCLE_MONITOR_TOKEN' --app-up-authorized-ns '$APP_UP_AUTHORIZED_NS' --health-boundary-started-ns '$APP_HEALTH_BOUNDARY_STARTED_NS' --health-boundary-app-fingerprint '$APP_HEALTH_BOUNDARY_FINGERPRINT'$APP_COLLECTOR_HEARTBEAT_ARGS --nats-cutover-preflight-before '$NATS_CUTOVER_PREFLIGHT_BEFORE_EVIDENCE_PATH' --nats-cutover-preflight-after '$NATS_CUTOVER_PREFLIGHT_AFTER_EVIDENCE_PATH' $required_args" | tr -d '\r' | tail -1)"
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
    acquire_deploy_lock
    run_locked_step "部署预检" preflight

    run_locked_step "代码提交" step_commit
    run_locked_step "代码同步" step_sync
    run_locked_step "Docker daemon 绑定" establish_local_docker_daemon_binding
    run_locked_step "readiness generation 生成" prepare_runtime_readiness_generation
    run_locked_step "NATS 目标参数冻结" prepare_nats_target_env_snapshot
    run_locked_step "镜像构建" step_build
    run_locked_step "WSL2 前置条件" ensure_wsl_runtime_prerequisites
    run_locked_step "RDP artifact 目录" ensure_rdp_artifact_directory
    run_locked_step "旧栈 full-down" step_down
    run_locked_step "镜像清理" step_prune
    run_locked_step "基础设施启动" step_infra_up
    run_locked_step "schema migration" step_schema_migrate
    run_locked_step "生命周期观察器启动" start_deployment_lifecycle_monitor
    run_locked_step "最终 NATS cutover" require_nats_durable_cutover_preflight \
        "最终 app-up 前" "post_infra_pre_app_up" \
        "$NATS_CUTOVER_PREFLIGHT_BEFORE_EVIDENCE_PATH"
    run_locked_step "应用启动" step_app_up
    run_locked_step "健康检查" step_health
    run_locked_step "稳定性观察" observe_app_stability_window
    run_locked_step "部署证据" write_deployment_evidence
    run_locked_step "部署报告" report
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
