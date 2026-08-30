#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ "$(uname -s)" != MINGW* ]]; then
    printf 'SKIP: requires Windows Git Bash -> WSL\n'
    exit 0
fi

# A first transport failure is recoverable when the next refresh succeeds.
AATS_TEST_ROOT="$PROJECT_ROOT" bash -c '
    set -euo pipefail
    source "$AATS_TEST_ROOT/scripts/deploy.sh"
    DEPLOY_LOCK_STALE_SECONDS=4
    DEPLOY_LOCK_HEARTBEAT_INTERVAL_SECONDS=1
    DEPLOY_LOCK_HEARTBEAT_FAILURE_BUDGET_SECONDS=2
    success_marker="$(mktemp)"
    rm -f -- "$success_marker"
    touch_calls=0
    touch_deploy_lock_lease() {
        touch_calls=$((touch_calls + 1))
        if (( touch_calls == 1 )); then
            return 1
        fi
        : >"$success_marker"
    }
    deploy_lock_heartbeat_loop "$$" &
    heartbeat_pid=$!
    for _ in {1..40}; do
        [[ -e "$success_marker" ]] && break
        sleep 0.1
    done
    if [[ ! -e "$success_marker" ]] || ! kill -0 "$heartbeat_pid" 2>/dev/null; then
        printf "transient heartbeat failure did not recover\n" >&2
        kill "$heartbeat_pid" 2>/dev/null || true
        wait "$heartbeat_pid" 2>/dev/null || true
        rm -f -- "$success_marker"
        exit 81
    fi
    kill "$heartbeat_pid"
    wait "$heartbeat_pid" 2>/dev/null || true
    rm -f -- "$success_marker"
'

# Persistent refresh failure must terminate before the lease can become stale.
AATS_TEST_ROOT="$PROJECT_ROOT" bash -c '
    set -euo pipefail
    source "$AATS_TEST_ROOT/scripts/deploy.sh"
    DEPLOY_LOCK_STALE_SECONDS=4
    DEPLOY_LOCK_HEARTBEAT_INTERVAL_SECONDS=1
    DEPLOY_LOCK_HEARTBEAT_FAILURE_BUDGET_SECONDS=2
    touch_deploy_lock_lease() { return 1; }
    started="$SECONDS"
    deploy_lock_heartbeat_loop "$$" &
    heartbeat_pid=$!
    set +e
    wait "$heartbeat_pid"
    heartbeat_status=$?
    set -e
    elapsed=$((SECONDS - started))
    if [[ "$heartbeat_status" -eq 0 || "$elapsed" -ge "$DEPLOY_LOCK_STALE_SECONDS" ]]; then
        printf "persistent heartbeat failure exceeded stale budget: status=%s elapsed=%s\n" \
            "$heartbeat_status" "$elapsed" >&2
        exit 82
    fi
'

# The refresher may update only the exact, owned 0600 regular lease.  Once the
# keeper removes it, a late heartbeat must fail without recreating the path.
AATS_TEST_ROOT="$PROJECT_ROOT" bash -c '
    set -euo pipefail
    source "$AATS_TEST_ROOT/scripts/deploy.sh"
    DEPLOY_LOCK_SCOPE="$(printf %s heartbeat-no-recreate-$$ | sha256sum | awk "{print substr(\$1, 1, 16)}")"
    DEPLOY_LOCK_TOKEN="heartbeat-no-recreate-$$-$RANDOM"
    DEPLOY_LOCK_LEASE_FILE="/tmp/aats-standard-deploy-lease-$DEPLOY_LOCK_SCOPE-$DEPLOY_LOCK_TOKEN"
    DEPLOY_WSL_DEFAULT_UID="$(MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" id -u | tr -d "\r")"
    lease_q=""
    printf -v lease_q "%q" "$DEPLOY_LOCK_LEASE_FILE"
    MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
        "umask 077; rm -f -- $lease_q; : >$lease_q; chmod 600 -- $lease_q"
    touch_deploy_lock_lease
    MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "rm -f -- $lease_q"
    set +e
    touch_deploy_lock_lease
    touch_status=$?
    set -e
    if [[ "$touch_status" -eq 0 ]] \
        || MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "test -e $lease_q"; then
        printf "removed lease was recreated by heartbeat touch\n" >&2
        MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "rm -f -- $lease_q"
        exit 83
    fi
'

# A successor holding the same flock is not proof that the old deployment
# still owns it.  Even with the old exact lease still fresh, the original
# holder identity is gone and the old deployment must fail closed.
AATS_TEST_ROOT="$PROJECT_ROOT" bash -c '
    set -euo pipefail
    source "$AATS_TEST_ROOT/scripts/deploy.sh"
    log_error() { :; }
    suffix="$$-$RANDOM"
    DEPLOY_LOCK_FILE="/tmp/aats-standard-deploy-successor-$suffix.lock"
    DEPLOY_LOCK_SCOPE="$(printf %s "$DEPLOY_LOCK_FILE" | sha256sum | awk "{print substr(\$1, 1, 16)}")"
    DEPLOY_LOCK_TOKEN="old-deployment-$suffix"
    DEPLOY_LOCK_LEASE_FILE="/tmp/aats-standard-deploy-lease-$DEPLOY_LOCK_SCOPE-$DEPLOY_LOCK_TOKEN"
    DEPLOY_WSL_DEFAULT_UID="$(MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" id -u | tr -d "\r")"
    old_identity_value="$(MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" python3 -c "import os; pid=str(os.getpid()); fields=open(\"/proc/\"+pid+\"/stat\").read().rsplit(\")\", 1)[1].split(); print(pid+\":\"+fields[19])" | tr -d "\r")"
    IFS=: read -r DEPLOY_LOCK_WSL_PID DEPLOY_LOCK_WSL_STARTTIME <<<"$old_identity_value"
    if [[ ! "$DEPLOY_LOCK_WSL_PID" =~ ^[1-9][0-9]*$ \
        || ! "$DEPLOY_LOCK_WSL_STARTTIME" =~ ^[1-9][0-9]*$ ]]; then
        printf "failed to capture exited original holder identity: %s\n" \
            "$old_identity_value" >&2
        exit 86
    fi
    DEPLOY_LOCK_HELD=true
    DEPLOY_LOCK_KEEPER_PID="$$"
    sleep 30 &
    DEPLOY_LOCK_HEARTBEAT_PID=$!
    coproc AATS_SILENT_PROTOCOL { sleep 20; }
    silent_protocol_pid="$AATS_SILENT_PROTOCOL_PID"
    DEPLOY_LOCK_READER_FD="${AATS_SILENT_PROTOCOL[0]}"
    DEPLOY_LOCK_WRITER_FD="${AATS_SILENT_PROTOCOL[1]}"
    lock_q=""
    lease_q=""
    ready="/tmp/aats-standard-deploy-successor-$suffix.ready"
    ready_q=""
    printf -v lock_q "%q" "$DEPLOY_LOCK_FILE"
    printf -v lease_q "%q" "$DEPLOY_LOCK_LEASE_FILE"
    printf -v ready_q "%q" "$ready"
    MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
        "umask 077; rm -f -- $lock_q $lease_q $ready_q; : >$lease_q; chmod 600 -- $lease_q"
    MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c \
        "exec 9>>$lock_q; flock 9; : >$ready_q; sleep 20" &
    successor_pid=$!
    ready_seen=false
    for _ in {1..50}; do
        if MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "test -e $ready_q"; then
            ready_seen=true
            break
        fi
        sleep 0.1
    done
    if [[ "$ready_seen" != true ]]; then
        kill "$successor_pid" 2>/dev/null || true
        wait "$successor_pid" 2>/dev/null || true
        kill "$DEPLOY_LOCK_HEARTBEAT_PID" 2>/dev/null || true
        wait "$DEPLOY_LOCK_HEARTBEAT_PID" 2>/dev/null || true
        kill "$silent_protocol_pid" 2>/dev/null || true
        wait "$silent_protocol_pid" 2>/dev/null || true
        MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "rm -f -- $lock_q $lease_q $ready_q"
        printf "successor did not acquire test flock\n" >&2
        exit 85
    fi
    set +e
    assert_deploy_lock_held "successor ownership smoke"
    assertion_status=$?
    set -e
    kill "$successor_pid" 2>/dev/null || true
    wait "$successor_pid" 2>/dev/null || true
    kill "$DEPLOY_LOCK_HEARTBEAT_PID" 2>/dev/null || true
    wait "$DEPLOY_LOCK_HEARTBEAT_PID" 2>/dev/null || true
    kill "$silent_protocol_pid" 2>/dev/null || true
    wait "$silent_protocol_pid" 2>/dev/null || true
    MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "rm -f -- $lock_q $lease_q $ready_q"
    if [[ "$assertion_status" -ne 16 ]]; then
        printf "old deployment accepted successor-owned flock: %s\n" "$assertion_status" >&2
        exit 84
    fi
'

printf 'PASS: deploy lock heartbeat and ownership contract\n'
