#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# This smoke test must run from real Windows Git Bash.  The production lock
# holder itself is a long-lived wsl.exe child; running under WSL would not test
# that cross-boundary lifetime.
if [[ "$(uname -s)" != MINGW* ]]; then
    printf 'SKIP: requires Windows Git Bash -> WSL\n'
    exit 0
fi

# shellcheck source=../../scripts/deploy.sh
source "$PROJECT_ROOT/scripts/deploy.sh"

DEPLOY_LOCK_FILE="/tmp/aats-standard-deploy-smoke.lock"
log_info() { :; }
log_ok() { :; }
log_warn() { :; }
log_error() { :; }

set +e
AATS_DEPLOY_LOCK_FILE="/tmp/aats-standard-deploy-illegal-override.lock" \
AATS_TEST_ROOT="$PROJECT_ROOT" \
    bash -c 'source "$AATS_TEST_ROOT/scripts/deploy.sh"; log_error(){ :; }; acquire_deploy_lock'
override_status=$?
set -e
if [[ "$override_status" -ne 18 ]]; then
    printf 'production lock-path override was not rejected: %s\n' "$override_status" >&2
    exit 90
fi

acquire_deploy_lock
assert_deploy_lock_held "smoke-held"

lock_file_q=""
printf -v lock_file_q '%q' "$DEPLOY_LOCK_FILE"
if MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "exec 8>>$lock_file_q; flock -n 8"; then
    printf 'second WSL process acquired a held deployment lock\n' >&2
    exit 91
fi

set +e
AATS_DEPLOY_TEST_MODE=true AATS_DEPLOY_LOCK_FILE="$DEPLOY_LOCK_FILE" AATS_TEST_ROOT="$PROJECT_ROOT" \
    bash -c 'source "$AATS_TEST_ROOT/scripts/deploy.sh"; log_info(){ :; }; log_ok(){ :; }; log_warn(){ :; }; log_error(){ :; }; PROFILE=spot; acquire_deploy_lock'
second_status=$?
set -e
if [[ "$second_status" -ne 14 ]]; then
    printf 'second standard deploy did not fail with lock-busy status: %s\n' "$second_status" >&2
    exit 93
fi

release_deploy_lock
if ! MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "exec 8>>$lock_file_q; flock -n 8"; then
    printf 'deployment lock remained held after release\n' >&2
    exit 92
fi


acquire_deploy_lock
kill "$DEPLOY_LOCK_KEEPER_PID" 2>/dev/null || true
wait "$DEPLOY_LOCK_KEEPER_PID" 2>/dev/null || true
fast_marker="/tmp/aats-deploy-fast-side-effect-$$-$RANDOM"
rm -f -- "$fast_marker"
set +e
run_lock_supervised_external "pre-spawn-keeper-loss-smoke" \
    bash -c ': > "$1"' _ "$fast_marker"
fast_loss_status=$?
set -e
if [[ "$fast_loss_status" -ne 16 || -e "$fast_marker" ]]; then
    printf 'keeper loss allowed a fast side effect before spawn fencing: status=%s marker=%s\n' \
        "$fast_loss_status" "$([[ -e "$fast_marker" ]] && echo present || echo absent)" >&2
    exit 101
fi
release_deploy_lock
rm -f -- "$fast_marker"


registration_suffix="$$-$RANDOM"
registration_ready="/tmp/aats-deploy-registration-$registration_suffix.ready"
registration_marker="/tmp/aats-deploy-registration-$registration_suffix.side-effect"
rm -f -- "$registration_ready" "$registration_marker"
AATS_TEST_ROOT="$PROJECT_ROOT" \
AATS_DEPLOY_TEST_MODE=true \
AATS_DEPLOY_LOCK_FILE="$DEPLOY_LOCK_FILE" \
AATS_DEPLOY_TEST_PRE_REGISTRATION_READY="$registration_ready" \
AATS_DEPLOY_TEST_PRE_REGISTRATION_DELAY_SECONDS=30 \
AATS_REGISTRATION_MARKER="$registration_marker" \
    bash -c '
        set -euo pipefail
        source "$AATS_TEST_ROOT/scripts/deploy.sh"
        log_info() { :; }
        log_ok() { :; }
        log_warn() { :; }
        log_error() { :; }
        acquire_deploy_lock
        run_lock_supervised_external "pre-registration-signal-smoke" \
            bash -c '\'' : >"$1" '\'' _ "$AATS_REGISTRATION_MARKER"
    ' &
registration_pid=$!
for _ in {1..100}; do
    [[ -e "$registration_ready" ]] && break
    sleep 0.05
done
if [[ ! -e "$registration_ready" ]]; then
    printf 'pre-registration signal smoke did not reach the gated race window\n' >&2
    kill -KILL "$registration_pid" 2>/dev/null || true
    exit 104
fi
set +e
kill -TERM "$registration_pid"
wait "$registration_pid"
registration_status=$?
set -e
sleep 0.2
if [[ "$registration_status" -ne 143 || -e "$registration_marker" ]]; then
    printf 'pre-registration TERM crossed the launch gate: status=%s marker=%s\n' \
        "$registration_status" "$([[ -e "$registration_marker" ]] && echo present || echo absent)" >&2
    exit 105
fi
rm -f -- "$registration_ready" "$registration_marker"


authorized_suffix="$$-$RANDOM"
authorized_ready="/tmp/aats-deploy-authorized-$authorized_suffix.ready"
authorized_marker="/tmp/aats-deploy-authorized-$authorized_suffix.side-effect"
rm -f -- "$authorized_ready" "$authorized_marker"
AATS_TEST_ROOT="$PROJECT_ROOT" \
AATS_DEPLOY_TEST_MODE=true \
AATS_DEPLOY_LOCK_FILE="$DEPLOY_LOCK_FILE" \
AATS_DEPLOY_TEST_AUTHORIZED_READY="$authorized_ready" \
AATS_DEPLOY_TEST_AUTHORIZED_DELAY_SECONDS=1 \
AATS_AUTHORIZED_MARKER="$authorized_marker" \
    bash -c '
        set -euo pipefail
        source "$AATS_TEST_ROOT/scripts/deploy.sh"
        log_info() { :; }
        log_ok() { :; }
        log_warn() { :; }
        log_error() { :; }
        acquire_deploy_lock
        run_lock_supervised_external "authorized-pre-marker-signal-smoke" \
            bash -c '\'' : >"$1" '\'' _ "$AATS_AUTHORIZED_MARKER"
    ' &
authorized_pid=$!
for _ in {1..100}; do
    [[ -e "$authorized_ready" ]] && break
    sleep 0.05
done
if [[ ! -e "$authorized_ready" ]]; then
    printf 'authorized/pre-marker signal smoke did not reach the gated race window\n' >&2
    kill -KILL "$authorized_pid" 2>/dev/null || true
    exit 106
fi
set +e
kill -TERM "$authorized_pid"
wait "$authorized_pid"
authorized_status=$?
set -e
if [[ "$authorized_status" -ne 143 || -e "$authorized_marker" ]]; then
    printf 'authorized/pre-marker TERM crossed the final launch gate: status=%s marker=%s\n' \
        "$authorized_status" "$([[ -e "$authorized_marker" ]] && echo present || echo absent)" >&2
    exit 107
fi
rm -f -- "$authorized_ready" "$authorized_marker"


sigkill_suffix="$$-$RANDOM"
sigkill_ready="/tmp/aats-deploy-sigkill-$sigkill_suffix.ready"
sigkill_old_marker="/tmp/aats-deploy-sigkill-$sigkill_suffix.old"
sigkill_successor_marker="/tmp/aats-deploy-sigkill-$sigkill_suffix.successor"
sigkill_ready_q=""
sigkill_old_marker_q=""
sigkill_successor_marker_q=""
printf -v sigkill_ready_q '%q' "$sigkill_ready"
printf -v sigkill_old_marker_q '%q' "$sigkill_old_marker"
printf -v sigkill_successor_marker_q '%q' "$sigkill_successor_marker"
MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
    "rm -f -- $sigkill_ready_q $sigkill_old_marker_q $sigkill_successor_marker_q"
AATS_TEST_ROOT="$PROJECT_ROOT" \
AATS_DEPLOY_TEST_MODE=true \
AATS_DEPLOY_TEST_LOCK_STALE_SECONDS=2 \
AATS_DEPLOY_LOCK_FILE="$DEPLOY_LOCK_FILE" \
AATS_SIGKILL_READY="$sigkill_ready" \
AATS_SIGKILL_OLD_MARKER="$sigkill_old_marker" \
    bash -c '
        set -euo pipefail
        source "$AATS_TEST_ROOT/scripts/deploy.sh"
        log_info() { :; }
        log_ok() { :; }
        log_warn() { :; }
        log_error() { :; }
        acquire_deploy_lock
        ready_q=""
        old_q=""
        printf -v ready_q "%q" "$AATS_SIGKILL_READY"
        printf -v old_q "%q" "$AATS_SIGKILL_OLD_MARKER"
        run_lock_supervised_external "parent-sigkill-smoke" \
            wsl -d "$DISTRO" bash -c \
            ": >$ready_q; sleep 5; : >$old_q"
    ' &
sigkill_parent_pid=$!
sigkill_ready_seen=false
for _ in {1..100}; do
    if MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "test -e $sigkill_ready_q"; then
        sigkill_ready_seen=true
        break
    fi
    sleep 0.05
done
if [[ "$sigkill_ready_seen" != true ]]; then
    printf 'SIGKILL smoke did not launch the supervised mutation\n' >&2
    kill -KILL "$sigkill_parent_pid" 2>/dev/null || true
    exit 108
fi
set +e
kill -KILL "$sigkill_parent_pid"
wait "$sigkill_parent_pid" 2>/dev/null
sigkill_parent_status=$?
set -e
if [[ "$sigkill_parent_status" -ne 137 ]]; then
    printf 'SIGKILL smoke parent did not exit 137: %s\n' "$sigkill_parent_status" >&2
    exit 109
fi

# The synthetic lease is stale after two seconds, but the five-second mutation
# still owns an active marker.  A successor must remain blocked in that gap.
sleep 2.5
set +e
AATS_TEST_ROOT="$PROJECT_ROOT" \
AATS_DEPLOY_TEST_MODE=true \
AATS_DEPLOY_TEST_LOCK_STALE_SECONDS=2 \
AATS_DEPLOY_LOCK_FILE="$DEPLOY_LOCK_FILE" \
AATS_SIGKILL_SUCCESSOR_MARKER="$sigkill_successor_marker" \
    bash -c '
        source "$AATS_TEST_ROOT/scripts/deploy.sh"
        log_info() { :; }
        log_ok() { :; }
        log_warn() { :; }
        log_error() { :; }
        acquire_deploy_lock
        marker_q=""
        printf -v marker_q "%q" "$AATS_SIGKILL_SUCCESSOR_MARKER"
        MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c ": >$marker_q"
        release_deploy_lock
    '
sigkill_blocked_status=$?
set -e
if [[ "$sigkill_blocked_status" -ne 14 ]] \
    || MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "test -e $sigkill_successor_marker_q"; then
    printf 'successor crossed stale lease while prior SIGKILL mutation was active: %s\n' \
        "$sigkill_blocked_status" >&2
    exit 110
fi

sigkill_successor_status=14
for _ in {1..100}; do
    set +e
    AATS_TEST_ROOT="$PROJECT_ROOT" \
    AATS_DEPLOY_TEST_MODE=true \
    AATS_DEPLOY_TEST_LOCK_STALE_SECONDS=2 \
    AATS_DEPLOY_LOCK_FILE="$DEPLOY_LOCK_FILE" \
    AATS_SIGKILL_SUCCESSOR_MARKER="$sigkill_successor_marker" \
        bash -c '
            source "$AATS_TEST_ROOT/scripts/deploy.sh"
            log_info() { :; }
            log_ok() { :; }
            log_warn() { :; }
            log_error() { :; }
            acquire_deploy_lock
            marker_q=""
            printf -v marker_q "%q" "$AATS_SIGKILL_SUCCESSOR_MARKER"
            MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c ": >$marker_q"
            release_deploy_lock
        '
    sigkill_successor_status=$?
    set -e
    [[ "$sigkill_successor_status" -eq 0 ]] && break
    sleep 0.1
done
if [[ "$sigkill_successor_status" -ne 0 ]] \
    || ! MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
        "test -e $sigkill_old_marker_q && test -e $sigkill_successor_marker_q"; then
    printf 'successor did not enter after prior SIGKILL mutation completed: %s\n' \
        "$sigkill_successor_status" >&2
    exit 111
fi
MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
    "rm -f -- $sigkill_ready_q $sigkill_old_marker_q $sigkill_successor_marker_q"


wrapper_kill_suffix="$$-$RANDOM"
wrapper_pid_file="/tmp/aats-deploy-wrapper-kill-$wrapper_kill_suffix.pid"
wrapper_child_ready="/tmp/aats-deploy-wrapper-kill-$wrapper_kill_suffix.ready"
wrapper_child_done="/tmp/aats-deploy-wrapper-kill-$wrapper_kill_suffix.done"
wrapper_second_marker="/tmp/aats-deploy-wrapper-kill-$wrapper_kill_suffix.second"
wrapper_successor_marker="/tmp/aats-deploy-wrapper-kill-$wrapper_kill_suffix.successor"
wrapper_child_ready_q=""
wrapper_child_done_q=""
wrapper_second_marker_q=""
wrapper_successor_marker_q=""
printf -v wrapper_child_ready_q '%q' "$wrapper_child_ready"
printf -v wrapper_child_done_q '%q' "$wrapper_child_done"
printf -v wrapper_second_marker_q '%q' "$wrapper_second_marker"
printf -v wrapper_successor_marker_q '%q' "$wrapper_successor_marker"
rm -f -- "$wrapper_pid_file"
MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
    "rm -f -- $wrapper_child_ready_q $wrapper_child_done_q $wrapper_second_marker_q $wrapper_successor_marker_q"
AATS_TEST_ROOT="$PROJECT_ROOT" \
AATS_DEPLOY_TEST_MODE=true \
AATS_DEPLOY_TEST_LOCK_STALE_SECONDS=2 \
AATS_DEPLOY_LOCK_FILE="$DEPLOY_LOCK_FILE" \
AATS_DEPLOY_TEST_WRAPPER_LAUNCHED_READY="$wrapper_pid_file" \
AATS_WRAPPER_CHILD_READY="$wrapper_child_ready" \
AATS_WRAPPER_CHILD_DONE="$wrapper_child_done" \
AATS_WRAPPER_SECOND_MARKER="$wrapper_second_marker" \
    bash -c '
        set -euo pipefail
        source "$AATS_TEST_ROOT/scripts/deploy.sh"
        log_info() { :; }
        log_ok() { :; }
        log_warn() { :; }
        log_error() { :; }
        acquire_deploy_lock
        ready_q=""
        done_q=""
        printf -v ready_q "%q" "$AATS_WRAPPER_CHILD_READY"
        printf -v done_q "%q" "$AATS_WRAPPER_CHILD_DONE"
        set +e
        run_lock_supervised_external "wrapper-sigkill-smoke" \
            wsl -d "$DISTRO" bash -c \
            ": >$ready_q; sleep 5; : >$done_q"
        first_status=$?
        set -e
        [[ "$first_status" -eq 16 || "$first_status" -eq 137 ]]
        second_q=""
        printf -v second_q "%q" "$AATS_WRAPPER_SECOND_MARKER"
        run_lock_supervised_external "wrapper-poisoned-second-smoke" \
            wsl -d "$DISTRO" bash -c ": >$second_q"
    ' &
wrapper_parent_pid=$!
wrapper_ready_seen=false
for _ in {1..100}; do
    if [[ -s "$wrapper_pid_file" ]] \
        && MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
            "test -e $wrapper_child_ready_q"; then
        wrapper_ready_seen=true
        break
    fi
    sleep 0.05
done
if [[ "$wrapper_ready_seen" != true ]]; then
    printf 'wrapper SIGKILL smoke did not launch the guarded mutation\n' >&2
    kill -KILL "$wrapper_parent_pid" 2>/dev/null || true
    exit 112
fi
wrapper_pid="$(tr -d '[:space:]' <"$wrapper_pid_file")"
if [[ ! "$wrapper_pid" =~ ^[1-9][0-9]*$ ]]; then
    printf 'wrapper SIGKILL smoke did not publish a valid wrapper PID\n' >&2
    kill -KILL "$wrapper_parent_pid" 2>/dev/null || true
    exit 113
fi
set +e
kill -KILL "$wrapper_pid"
wait "$wrapper_parent_pid"
wrapper_parent_status=$?
set -e
if [[ "$wrapper_parent_status" -ne 16 ]]; then
    printf 'wrapper hard-crash did not fail parent closed: %s\n' \
        "$wrapper_parent_status" >&2
    exit 114
fi
if MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
    "test -e $wrapper_second_marker_q"; then
    printf 'wrapper hard-crash poison allowed a second command to start\n' >&2
    exit 117
fi

# The wrapper is gone but its command guard and active marker must retain
# exclusion until the true WSL mutation completes.
sleep 2.5
set +e
AATS_TEST_ROOT="$PROJECT_ROOT" \
AATS_DEPLOY_TEST_MODE=true \
AATS_DEPLOY_TEST_LOCK_STALE_SECONDS=2 \
AATS_DEPLOY_LOCK_FILE="$DEPLOY_LOCK_FILE" \
AATS_WRAPPER_SUCCESSOR_MARKER="$wrapper_successor_marker" \
    bash -c '
        source "$AATS_TEST_ROOT/scripts/deploy.sh"
        log_info() { :; }
        log_ok() { :; }
        log_warn() { :; }
        log_error() { :; }
        acquire_deploy_lock
        marker_q=""
        printf -v marker_q "%q" "$AATS_WRAPPER_SUCCESSOR_MARKER"
        MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c ": >$marker_q"
        release_deploy_lock
    '
wrapper_blocked_status=$?
set -e
if [[ "$wrapper_blocked_status" -ne 14 ]] \
    || MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
        "test -e $wrapper_successor_marker_q"; then
    printf 'wrapper hard-crash allowed successor overlap: %s\n' \
        "$wrapper_blocked_status" >&2
    exit 115
fi

wrapper_successor_status=14
for _ in {1..100}; do
    set +e
    AATS_TEST_ROOT="$PROJECT_ROOT" \
    AATS_DEPLOY_TEST_MODE=true \
    AATS_DEPLOY_TEST_LOCK_STALE_SECONDS=2 \
    AATS_DEPLOY_LOCK_FILE="$DEPLOY_LOCK_FILE" \
    AATS_WRAPPER_SUCCESSOR_MARKER="$wrapper_successor_marker" \
        bash -c '
            source "$AATS_TEST_ROOT/scripts/deploy.sh"
            log_info() { :; }
            log_ok() { :; }
            log_warn() { :; }
            log_error() { :; }
            acquire_deploy_lock
            marker_q=""
            printf -v marker_q "%q" "$AATS_WRAPPER_SUCCESSOR_MARKER"
            MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c ": >$marker_q"
            release_deploy_lock
        '
    wrapper_successor_status=$?
    set -e
    [[ "$wrapper_successor_status" -eq 0 ]] && break
    sleep 0.1
done
if [[ "$wrapper_successor_status" -ne 0 ]] \
    || ! MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
        "test -e $wrapper_child_done_q && test -e $wrapper_successor_marker_q"; then
    printf 'successor did not enter after guarded wrapper mutation completed: %s\n' \
        "$wrapper_successor_status" >&2
    exit 116
fi
rm -f -- "$wrapper_pid_file"
MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
    "rm -f -- $wrapper_child_ready_q $wrapper_child_done_q $wrapper_second_marker_q $wrapper_successor_marker_q"


acquire_deploy_lock
kill "$DEPLOY_LOCK_KEEPER_PID" 2>/dev/null || true
wait "$DEPLOY_LOCK_KEEPER_PID" 2>/dev/null || true
takeover_marker="/tmp/aats-deploy-takeover-quarantine-$$-$RANDOM"
rm -f -- "$takeover_marker"
AATS_DEPLOY_TEST_MODE=true \
AATS_DEPLOY_LOCK_FILE="$DEPLOY_LOCK_FILE" \
AATS_TEST_ROOT="$PROJECT_ROOT" \
AATS_TAKEOVER_MARKER="$takeover_marker" \
    bash -c 'source "$AATS_TEST_ROOT/scripts/deploy.sh"; log_info(){ :; }; log_ok(){ :; }; log_warn(){ :; }; log_error(){ :; }; acquire_deploy_lock; : > "$AATS_TAKEOVER_MARKER"; release_deploy_lock' &
takeover_pid=$!
sleep 1
if [[ -e "$takeover_marker" ]]; then
    printf 'fresh predecessor lease did not quarantine takeover\n' >&2
    exit 102
fi
release_deploy_lock
set +e
wait "$takeover_pid"
takeover_status=$?
set -e
if [[ "$takeover_status" -ne 0 || ! -e "$takeover_marker" ]]; then
    printf 'takeover did not proceed after predecessor cleanup: status=%s\n' "$takeover_status" >&2
    exit 103
fi
rm -f -- "$takeover_marker"


acquire_deploy_lock
linux_pid_file="/tmp/aats-deploy-supervisor-smoke-$DEPLOY_LOCK_TOKEN.pid"
linux_marker="/tmp/aats-deploy-supervisor-smoke-$DEPLOY_LOCK_TOKEN.survived"
linux_pid_file_q=""
linux_marker_q=""
printf -v linux_pid_file_q '%q' "$linux_pid_file"
printf -v linux_marker_q '%q' "$linux_marker"
MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
    "rm -f -- $linux_pid_file_q $linux_marker_q"
(
    sleep 1
    kill "$DEPLOY_LOCK_KEEPER_PID" 2>/dev/null || true
) &
keeper_killer=$!
set +e
run_lock_supervised_external "keeper-loss-smoke" \
    wsl -d "$DISTRO" bash -c \
    "printf '%s\\n' \"\$BASHPID\" > $linux_pid_file_q; sleep 2; : > $linux_marker_q"
loss_status=$?
set -e
wait "$keeper_killer" 2>/dev/null || true
if [[ "$loss_status" -ne 16 ]]; then
    printf 'mid-step keeper loss did not fail closed: %s\n' "$loss_status" >&2
    exit 94
fi
if ! MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
    "if [[ ! -e $linux_marker_q ]]; then exit 1; fi; if [[ -s $linux_pid_file_q ]]; then child_pid=\$(cat -- $linux_pid_file_q); if kill -0 \"\$child_pid\" 2>/dev/null; then exit 2; fi; fi"; then
    printf 'keeper loss did not retain exclusion until the supervised WSL child finished\n' >&2
    exit 95
fi
MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
    "rm -f -- $linux_pid_file_q $linux_marker_q"
release_deploy_lock


acquire_deploy_lock
assert_deploy_lock_held "smoke-reacquired"
release_deploy_lock


signal_suffix="$$-$RANDOM"
signal_ready="/tmp/aats-deploy-signal-smoke-$signal_suffix.ready"
signal_linux_pid_file="/tmp/aats-deploy-signal-smoke-$signal_suffix.pid"
signal_linux_marker="/tmp/aats-deploy-signal-smoke-$signal_suffix.survived"
signal_linux_pid_file_q=""
signal_linux_marker_q=""
printf -v signal_linux_pid_file_q '%q' "$signal_linux_pid_file"
printf -v signal_linux_marker_q '%q' "$signal_linux_marker"
rm -f -- "$signal_ready"
MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
    "rm -f -- $signal_linux_pid_file_q $signal_linux_marker_q"

AATS_TEST_ROOT="$PROJECT_ROOT" \
AATS_DEPLOY_TEST_MODE=true \
AATS_DEPLOY_LOCK_FILE="$DEPLOY_LOCK_FILE" \
AATS_SIGNAL_READY="$signal_ready" \
AATS_SIGNAL_LINUX_PID_FILE="$signal_linux_pid_file" \
AATS_SIGNAL_LINUX_MARKER="$signal_linux_marker" \
    bash -c '
        set -euo pipefail
        source "$AATS_TEST_ROOT/scripts/deploy.sh"
        log_info() { :; }
        log_ok() { :; }
        log_warn() { :; }
        log_error() { :; }
        acquire_deploy_lock
        : > "$AATS_SIGNAL_READY"
        linux_pid_q=""
        marker_q=""
        printf -v linux_pid_q "%q" "$AATS_SIGNAL_LINUX_PID_FILE"
        printf -v marker_q "%q" "$AATS_SIGNAL_LINUX_MARKER"
        run_lock_supervised_external "signal-exit-smoke" \
            wsl -d "$DISTRO" bash -c \
            "printf \"%s\\n\" \"\$BASHPID\" > $linux_pid_q; sleep 2; : > $marker_q"
    ' &
signal_deploy_pid=$!

signal_ready_seen=false
for _ in {1..100}; do
    if [[ -e "$signal_ready" ]] && MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
        "test -s $signal_linux_pid_file_q"; then
        signal_ready_seen=true
        break
    fi
    sleep 0.1
done
if [[ "$signal_ready_seen" != true ]]; then
    printf 'signal smoke deployment did not become ready\n' >&2
    kill -TERM "$signal_deploy_pid" 2>/dev/null || true
    wait "$signal_deploy_pid" 2>/dev/null || true
    exit 96
fi
if MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "exec 8>>$lock_file_q; flock -n 8"; then
    printf 'signal smoke deployment did not hold the lock before TERM\n' >&2
    kill -TERM "$signal_deploy_pid" 2>/dev/null || true
    wait "$signal_deploy_pid" 2>/dev/null || true
    exit 97
fi

set +e
kill -TERM "$signal_deploy_pid"
wait "$signal_deploy_pid"
signal_status=$?
set -e
if [[ "$signal_status" -ne 143 ]]; then
    printf 'TERM deployment shell did not exit 143: %s\n' "$signal_status" >&2
    exit 98
fi
if ! MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
    "if [[ ! -e $signal_linux_marker_q ]]; then exit 1; fi; child_pid=\$(cat -- $signal_linux_pid_file_q); if kill -0 \"\$child_pid\" 2>/dev/null; then exit 2; fi"; then
    printf 'deployment-shell TERM did not retain exclusion until the supervised WSL child finished\n' >&2
    exit 99
fi
if ! MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "exec 8>>$lock_file_q; flock -n 8"; then
    printf 'deployment lock was not released after TERM child cleanup\n' >&2
    exit 100
fi

rm -f -- "$signal_ready"
MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
    "rm -f -- $signal_linux_pid_file_q $signal_linux_marker_q"
