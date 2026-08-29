#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=../../scripts/deploy.sh
source "$PROJECT_ROOT/scripts/deploy.sh"
log_info() { :; }
log_ok() { :; }
log_warn() { :; }
log_error() { :; }
WSL_PROJECT="/tmp/aats"
DEPLOY_DIR="deploy/wsl2-dev"
WSL2_ENV_FILE="/tmp/aats/.env.wsl2"
DEPLOY_LOCK_TOKEN="smoke-lock-token"

scenario=""
captured_start=""
captured_compose=""
fresh_volume_created=false
mutation_log="$(mktemp)"
trap 'rm -f "$mutation_log"' EXIT
SMOKE_CONTAINER_ID="$(printf '1%.0s' {1..64})"
SMOKE_CONTAINER_FINGERPRINT="sha256:$(printf 'a%.0s' {1..64})"
SMOKE_VOLUME_FINGERPRINT="sha256:$(printf 'b%.0s' {1..64})"
SMOKE_REPLACEMENT_VOLUME_FINGERPRINT="sha256:$(printf 'c%.0s' {1..64})"

is_existing_scenario() {
    case "$scenario" in
        existing_*) return 0 ;;
        *) return 1 ;;
    esac
}

existing_nats_binding() {
    local state="running"
    local image="$NATS_EXPECTED_IMAGE"
    local project="aats-dev"
    local service="nats"
    local mount_type="volume"
    local mount_name="aats-dev_nats_data"
    local mount_rw="true"
    local extra=""
    case "$scenario" in
        existing_stopped) state="exited" ;;
        existing_created) state="created" ;;
        existing_foreign_project) project="foreign-project" ;;
        existing_foreign_service) service="foreign-nats" ;;
        existing_wrong_image) image="nats:untrusted" ;;
        existing_wrong_volume) mount_name="foreign_nats_data" ;;
        existing_read_only_volume) mount_rw="false" ;;
        existing_duplicate_data_mount)
            extra="volume|foreign_nats_data|true"
            ;;
    esac
    printf '%s|%s|%s|%s|%s|%s|%s|%s|%s\n' \
        "$SMOKE_CONTAINER_ID" "$state" "$image" "$project" "$service" \
        "$mount_type" "$mount_name" "$mount_rw" "$extra"
}

wsl_run() {
    local command="$1"
    case "$command" in
        *"docker ps -a --format"*)
            if is_existing_scenario; then
                printf 'aats-nats\n'
            fi
            ;;
        *"docker inspect --format "*".Config.Image"*" aats-nats"*)
            existing_nats_binding
            ;;
        *"docker inspect --format '{{.Id}}' aats-nats"*)
            printf '%s\n' "$SMOKE_CONTAINER_ID"
            ;;
        *"docker start "*)
            captured_start="$command"
            printf '%s\n' "$command" >>"$mutation_log"
            ;;
        *"for attempt in"*)
            return 0
            ;;
        *"scripts/nats_runtime_identity.py snapshot --format tsv"*)
            if [[ "$scenario" == existing_identity_gate_failure ]]; then
                return 72
            fi
            local snapshot_volume_fingerprint="$SMOKE_VOLUME_FINGERPRINT"
            if [[ "$scenario" == fresh_post_claim_replace \
                && -n "$captured_compose" ]]; then
                snapshot_volume_fingerprint="$SMOKE_REPLACEMENT_VOLUME_FINGERPRINT"
            fi
            local snapshot_restart_count=0
            if [[ "$scenario" == fresh_restart_history ]]; then
                snapshot_restart_count=1
            fi
            printf '%s\t%s\t%s\t%s\n' \
                "$SMOKE_CONTAINER_FINGERPRINT" "$SMOKE_CONTAINER_ID" \
                "$snapshot_restart_count" "$snapshot_volume_fingerprint"
            ;;
        *"scripts/nats_runtime_identity.py volume-fingerprint"*)
            printf '%s\n' "$SMOKE_VOLUME_FINGERPRINT"
            ;;
        *"docker volume inspect --format"*"com.aats.bootstrap_lock"*)
            if [[ "$scenario" == fresh_claim_race \
                || ( "$scenario" == fresh_post_claim_replace && -n "$captured_compose" ) ]]; then
                printf 'other-owner\n'
            else
                printf '%s\n' "$DEPLOY_LOCK_TOKEN"
            fi
            ;;
        *"docker volume create"*)
            fresh_volume_created=true
            printf '%s\n' "$command" >>"$mutation_log"
            ;;
        *"docker volume ls"*)
            if [[ "$scenario" == volume_query_failure ]]; then
                return 71
            elif [[ "$scenario" == missing_with_volume ]]; then
                printf 'aats-dev_nats_data\n'
            elif [[ "$scenario" == missing_with_legacy_volume ]]; then
                printf 'legacy-stack_nats_data\n'
            elif [[ "$fresh_volume_created" == true ]]; then
                printf 'aats-dev_nats_data\n'
            fi
            ;;
        *"docker compose"*)
            captured_compose="$command"
            printf '%s\n' "$command" >>"$mutation_log"
            ;;
        *)
            printf 'unexpected command: %s\n' "$command" >&2
            return 120
            ;;
    esac
}

reset_capture() {
    captured_start=""
    captured_compose=""
    fresh_volume_created=false
    : >"$mutation_log"
}

assert_existing_failure_without_mutation() {
    local expected_status
    reset_capture
    set +e
    ensure_nats_cutover_preflight_infra_up
    expected_status=$?
    set -e
    [[ "$expected_status" -eq 20 ]]
    [[ -z "$captured_start" ]]
    [[ -z "$captured_compose" ]]
    [[ "$fresh_volume_created" == false ]]
    [[ ! -s "$mutation_log" ]]
}

# A valid, already-running NATS may be observed in place. The preflight must
# bind the exact image, Compose ownership and sole read-write /data volume,
# without issuing a start or Compose mutation.
scenario=existing_running
reset_capture
ensure_nats_cutover_preflight_infra_up
[[ -z "$captured_start" ]]
[[ -z "$captured_compose" ]]
[[ "$fresh_volume_created" == false ]]
[[ ! -s "$mutation_log" ]]
[[ "$NATS_CUTOVER_BOOTSTRAP_MODE" == "existing_container_preserved" ]]
[[ "$NATS_CUTOVER_BASELINE_FINGERPRINT" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$NATS_CUTOVER_VOLUME_FINGERPRINT" =~ ^sha256:[0-9a-f]{64}$ ]]

# Existing stopped/created containers are UNKNOWN before the first read-only
# baseline. They must never be auto-started, even when their visible projection
# otherwise matches the managed NATS contract.
for scenario in existing_stopped existing_created; do
    assert_existing_failure_without_mutation
done

# A foreign same-name container, wrong pinned image, or ambiguous/non-RW data
# mount must fail before any start, Compose call, or fresh-volume claim.
for scenario in \
    existing_foreign_project \
    existing_foreign_service \
    existing_wrong_image \
    existing_wrong_volume \
    existing_read_only_volume \
    existing_duplicate_data_mount; do
    assert_existing_failure_without_mutation
done

scenario=existing_identity_gate_failure
assert_existing_failure_without_mutation

scenario=missing_with_volume
reset_capture
set +e
ensure_nats_cutover_preflight_infra_up
missing_status=$?
set -e
[[ "$missing_status" -eq 20 ]]
[[ -z "$captured_start" && -z "$captured_compose" ]]

scenario=missing_with_legacy_volume
reset_capture
set +e
ensure_nats_cutover_preflight_infra_up
legacy_volume_status=$?
set -e
[[ "$legacy_volume_status" -eq 20 ]]
[[ -z "$captured_start" && -z "$captured_compose" ]]

scenario=volume_query_failure
reset_capture
set +e
ensure_nats_cutover_preflight_infra_up
volume_failure_status=$?
set -e
[[ "$volume_failure_status" -eq 20 ]]
[[ -z "$captured_start" && -z "$captured_compose" ]]

scenario=fresh_install
reset_capture
ensure_nats_cutover_preflight_infra_up
[[ -z "$captured_start" ]]
[[ "$captured_compose" == *"--no-deps nats"* ]]
[[ "$NATS_CUTOVER_BOOTSTRAP_MODE" == "proven_fresh_install" ]]
[[ "$NATS_CUTOVER_BASELINE_FINGERPRINT" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$NATS_CUTOVER_VOLUME_FINGERPRINT" =~ ^sha256:[0-9a-f]{64}$ ]]

scenario=fresh_claim_race
reset_capture
set +e
ensure_nats_cutover_preflight_infra_up
claim_race_status=$?
set -e
[[ "$claim_race_status" -eq 20 ]]
[[ -z "$captured_start" && -z "$captured_compose" ]]

scenario=fresh_restart_history
reset_capture
set +e
ensure_nats_cutover_preflight_infra_up
fresh_restart_status=$?
set -e
[[ "$fresh_restart_status" -eq 20 ]]
[[ -z "$captured_start" ]]
[[ "$captured_compose" == *"--no-deps nats"* ]]

scenario=fresh_post_claim_replace
reset_capture
set +e
ensure_nats_cutover_preflight_infra_up
post_claim_replace_status=$?
set -e
[[ "$post_claim_replace_status" -eq 20 ]]
[[ -z "$captured_start" ]]
[[ "$captured_compose" == *"--no-deps nats"* ]]
