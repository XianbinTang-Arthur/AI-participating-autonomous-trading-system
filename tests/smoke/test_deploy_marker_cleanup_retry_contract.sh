#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export AATS_DEPLOY_TEST_MODE=true

# shellcheck source=scripts/deploy.sh
source "$repo_root/scripts/deploy.sh"

scope="0123456789abcdef"
token="marker-retry-$$-$RANDOM"
marker_uid="$(MSYS_NO_PATHCONV=1 command wsl -d "$DISTRO" id -u | tr -d '\r')"
[[ "$marker_uid" =~ ^[0-9]+$ ]]
attempt_log="$(mktemp)"
guard_gate="$(mktemp -d)"
lease_removed_sentinel=""
declare -a cleanup_paths=()

marker_for() {
    printf '/tmp/aats-standard-deploy-active-%s-%s-%s' "$scope" "$token" "$1"
}

completion_for() {
    local marker_file="$1"
    printf '%s' "${marker_file/aats-standard-deploy-active-/aats-standard-deploy-completion-}"
}

create_marker() {
    local marker_file="$1" marker_q
    printf -v marker_q '%q' "$marker_file"
    MSYS_NO_PATHCONV=1 command wsl -d "$DISTRO" bash -c \
        "umask 077; test ! -e $marker_q; : >$marker_q; chmod 600 -- $marker_q"
}

assert_wsl_path_exists() {
    local path="$1" path_q
    printf -v path_q '%q' "$path"
    MSYS_NO_PATHCONV=1 command wsl -d "$DISTRO" bash -c "test -e $path_q"
}

assert_wsl_path_absent() {
    local path="$1" path_q
    printf -v path_q '%q' "$path"
    if MSYS_NO_PATHCONV=1 command wsl -d "$DISTRO" bash -c "test -e $path_q"; then
        printf 'unexpected WSL path survived: %s\n' "$path" >&2
        exit 1
    fi
}

cleanup() {
    local path path_q
    unset -f wsl 2>/dev/null || true
    for path in "${cleanup_paths[@]}"; do
        printf -v path_q '%q' "$path"
        MSYS_NO_PATHCONV=1 command wsl -d "$DISTRO" bash -c \
            "rm -f -- $path_q" >/dev/null 2>&1 || true
    done
    rm -f -- "$attempt_log"
    if [[ -n "$lease_removed_sentinel" ]]; then
        rm -f -- "$lease_removed_sentinel"
    fi
    rm -f -- "$guard_gate/command.stderr" "$guard_gate/command.stdout"
    rmdir -- "$guard_gate" 2>/dev/null || true
}
trap cleanup EXIT

# A transient failure before the exact cleanup request reaches WSL is retried.
marker_retry="$(marker_for 1)"
cleanup_paths+=("$marker_retry")
create_marker "$marker_retry"
wsl() {
    printf '.\n' >>"$attempt_log"
    if [[ "$(wc -l <"$attempt_log")" -lt 3 ]]; then
        return 1
    fi
    command wsl "$@"
}
remove_proven_completed_active_marker "$marker_retry"
attempts="$(wc -l <"$attempt_log")"
[[ "$attempts" -eq 3 ]]
unset -f wsl
assert_wsl_path_absent "$marker_retry"

if remove_proven_completed_active_marker "/tmp/not-an-aats-active-marker"; then
    echo "invalid marker path unexpectedly accepted" >&2
    exit 1
fi

# Permanent cleanup transport failure is fail-closed and keeps the marker.
marker_cleanup_failure="$(marker_for 2)"
cleanup_paths+=("$marker_cleanup_failure")
create_marker "$marker_cleanup_failure"
wsl() { return 1; }
set +e
remove_proven_completed_active_marker "$marker_cleanup_failure"
cleanup_failure_status=$?
set -e
unset -f wsl
[[ "$cleanup_failure_status" -eq 16 ]]
assert_wsl_path_exists "$marker_cleanup_failure"
remove_proven_completed_active_marker "$marker_cleanup_failure"

# A known-local command preserves its real non-zero status after safe cleanup.
marker_local="$(marker_for 3)"
cleanup_paths+=("$marker_local")
create_marker "$marker_local"
set +e
run_supervised_command_guard \
    "$marker_local" local "" default stream "$guard_gate" bash -c 'exit 7'
local_status=$?
set -e
[[ "$local_status" -eq 7 ]]
assert_wsl_path_absent "$marker_local"

# A client return without a WSL-side acknowledgement remains poisoned.
marker_ambiguous="$(marker_for 4)"
completion_ambiguous="$(completion_for "$marker_ambiguous")"
cleanup_paths+=("$marker_ambiguous" "$completion_ambiguous")
create_marker "$marker_ambiguous"
set +e
ambiguous_diagnostic="$(run_supervised_command_guard \
    "$marker_ambiguous" wsl-ack "$completion_ambiguous" default \
    capture "$guard_gate" bash -c 'exit 1' 2>&1)"
ambiguous_status=$?
set -e
[[ "$ambiguous_status" -eq 16 ]]
[[ "$ambiguous_diagnostic" == *"WSL completion acknowledgement 缺失或校验失败: transport_status=1"* ]]
assert_wsl_path_exists "$marker_ambiguous"
assert_wsl_path_absent "$completion_ambiguous"
DEPLOY_LOCK_SCOPE="$scope"
DEPLOY_LOCK_TOKEN="$token"
if assert_no_owned_active_markers "subshell poison smoke"; then
    echo "parent failed to observe durable active marker" >&2
    exit 1
fi
remove_proven_completed_active_marker "$marker_ambiguous"
assert_no_owned_active_markers "post-recovery smoke"

# A real remote non-zero completion is authoritative only after the WSL-side
# wrapper atomically writes the marker-bound acknowledgement.
marker_remote="$(marker_for 5)"
completion_remote="$(completion_for "$marker_remote")"
cleanup_paths+=("$marker_remote" "$completion_remote")
create_marker "$marker_remote"
wrapped="$(build_wsl_completion_wrapped_command \
    'exit 7' "$marker_remote" "$completion_remote" capture "$marker_uid")"
wrapped_encoded="$(printf '%s' "$wrapped" | base64 | tr -d '\r\n')"
set +e
MSYS_NO_PATHCONV=1 command wsl -d "$DISTRO" bash -c \
    "printf '%s' '$wrapped_encoded' | base64 --decode | bash"
remote_client_status=$?
set -e
[[ "$remote_client_status" -eq 0 ]]
assert_wsl_path_exists "$marker_remote"
assert_wsl_path_exists "$completion_remote"
empty_sha256="$(printf '' | sha256sum | awk '{print $1}')"
proof="$(finalize_proven_wsl_completion \
    "$marker_remote" "$completion_remote" default capture \
    0 "$empty_sha256" 0 "$empty_sha256" "$marker_uid")"
[[ "$proof" -eq 7 ]]
assert_wsl_path_absent "$marker_remote"
assert_wsl_path_absent "$completion_remote"

# Wrapper-internal failures emit only an allowlisted phase and numeric status;
# they do not expose the remote command or captured output.
marker_diagnostic="$(marker_for 8)"
completion_diagnostic="$(completion_for "$marker_diagnostic")"
cleanup_paths+=("$marker_diagnostic" "$completion_diagnostic")
create_marker "$marker_diagnostic"
completion_diagnostic_q=""
printf -v completion_diagnostic_q '%q' "$completion_diagnostic"
MSYS_NO_PATHCONV=1 command wsl -d "$DISTRO" bash -c \
    "umask 077; : >$completion_diagnostic_q; chmod 600 -- $completion_diagnostic_q"
wrapped="$(build_wsl_completion_wrapped_command \
    'printf forbidden-output' "$marker_diagnostic" "$completion_diagnostic" capture "$marker_uid")"
wrapped_encoded="$(printf '%s' "$wrapped" | base64 | tr -d '\r\n')"
set +e
diagnostic_output="$(MSYS_NO_PATHCONV=1 command wsl -d "$DISTRO" bash -c \
    "printf '%s' '$wrapped_encoded' | base64 --decode | bash" 2>&1)"
diagnostic_status=$?
set -e
[[ "$diagnostic_status" -eq 126 ]]
[[ "$diagnostic_output" == *"WSL completion wrapper failed: phase=preflight status=126"* ]]
[[ "$diagnostic_output" != *"forbidden-output"* ]]
assert_wsl_path_exists "$marker_diagnostic"
remove_proven_completed_active_marker "$marker_diagnostic"
MSYS_NO_PATHCONV=1 command wsl -d "$DISTRO" bash -c \
    "rm -f -- $completion_diagnostic_q"

# If the first finalizer transport loses its response after the remote proof
# already removed the marker, the still-present strict acknowledgement makes a
# second proof idempotent instead of permanently poisoning the sequence.
marker_idempotent="$(marker_for 7)"
completion_idempotent="$(completion_for "$marker_idempotent")"
cleanup_paths+=("$marker_idempotent" "$completion_idempotent")
create_marker "$marker_idempotent"
wrapped="$(build_wsl_completion_wrapped_command \
    'exit 7' "$marker_idempotent" "$completion_idempotent" capture "$marker_uid")"
wrapped_encoded="$(printf '%s' "$wrapped" | base64 | tr -d '\r\n')"
MSYS_NO_PATHCONV=1 command wsl -d "$DISTRO" bash -c \
    "printf '%s' '$wrapped_encoded' | base64 --decode | bash"
: >"$attempt_log"
wsl() {
    printf '.\n' >>"$attempt_log"
    if [[ "$(wc -l <"$attempt_log")" -eq 1 ]]; then
        command wsl "$@" >/dev/null
        return 1
    fi
    command wsl "$@"
}
proof="$(finalize_proven_wsl_completion \
    "$marker_idempotent" "$completion_idempotent" default capture \
    0 "$empty_sha256" 0 "$empty_sha256" "$marker_uid")"
unset -f wsl
[[ "$proof" -eq 7 ]]
[[ "$(wc -l <"$attempt_log")" -eq 3 ]]
assert_wsl_path_absent "$marker_idempotent"
assert_wsl_path_absent "$completion_idempotent"

# EXIT-trap cleanup ambiguity must override a pending success with fail-closed
# status 16, while preserving a pre-existing non-zero deployment failure.
set +e
AATS_DEPLOY_TEST_MODE=true REPO_ROOT="$repo_root" bash -c '
    source "$REPO_ROOT/scripts/deploy.sh"
    terminate_active_supervised_process() { return 1; }
    true
    release_deploy_lock
'
release_success_status=$?
AATS_DEPLOY_TEST_MODE=true REPO_ROOT="$repo_root" bash -c '
    source "$REPO_ROOT/scripts/deploy.sh"
    terminate_active_supervised_process() { return 1; }
    set +e
    (exit 9)
    release_deploy_lock
'
release_failure_status=$?
set -e
[[ "$release_success_status" -eq 16 ]]
[[ "$release_failure_status" -eq 9 ]]

# Cleanup can start a new supervised WSL command after release's first marker
# scan.  A marker created in that window must convert pending success to 16 and
# must prevent lease removal.
cleanup_window_token="${token}-cleanup-window"
cleanup_window_marker="/tmp/aats-standard-deploy-active-$scope-$cleanup_window_token-1"
cleanup_paths+=("$cleanup_window_marker")
lease_removed_sentinel="$(mktemp)"
rm -f -- "$lease_removed_sentinel"
set +e
AATS_DEPLOY_TEST_MODE=true \
REPO_ROOT="$repo_root" \
INJECTED_SCOPE="$scope" \
INJECTED_TOKEN="$cleanup_window_token" \
INJECTED_MARKER="$cleanup_window_marker" \
LEASE_REMOVED_SENTINEL="$lease_removed_sentinel" \
bash -c '
    source "$REPO_ROOT/scripts/deploy.sh"
    DEPLOY_LOCK_SCOPE="$INJECTED_SCOPE"
    DEPLOY_LOCK_TOKEN="$INJECTED_TOKEN"
    DEPLOY_LOCK_HELD=true
    terminate_active_supervised_process() { return 0; }
    assert_deploy_lock_held() { return 0; }
    cleanup_deployment_lifecycle_monitor() { return 0; }
    cleanup_nats_target_env_snapshot() {
        local marker_q
        printf -v marker_q "%q" "$INJECTED_MARKER"
        MSYS_NO_PATHCONV=1 command wsl -d "$DISTRO" bash -c \
            "umask 077; : >$marker_q; chmod 600 -- $marker_q"
        return 1
    }
    remove_deploy_lock_lease() { : >"$LEASE_REMOVED_SENTINEL"; }
    true
    release_deploy_lock
'
cleanup_window_status=$?
set -e
[[ "$cleanup_window_status" -eq 16 ]]
assert_wsl_path_exists "$cleanup_window_marker"
if [[ -e "$lease_removed_sentinel" ]]; then
    echo "cleanup ambiguity unexpectedly reached lease removal" >&2
    exit 1
fi
remove_proven_completed_active_marker "$cleanup_window_marker"
rm -f -- "$lease_removed_sentinel"

# Even after completion proof, a failed local replay must never preserve a
# semantic success status.
finalize_proven_wsl_completion() { printf '0\n'; }
cat() { return 1; }
set +e
run_supervised_command_guard \
    "$(marker_for 6)" wsl-ack "$(completion_for "$(marker_for 6)")" \
    default capture "$guard_gate" bash -c 'printf replay-output'
replay_failure_status=$?
set -e
unset -f cat
unset -f finalize_proven_wsl_completion
[[ "$replay_failure_status" -eq 16 ]]
