#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=../../scripts/deploy.sh
source "$PROJECT_ROOT/scripts/deploy.sh"

RUNTIME_READINESS_GENERATION="candidate-20260828T000000Z-1-1"
DEPLOYED_GIT_COMMIT="0123456789abcdef0123456789abcdef01234567"
NATS_TARGET_ENV_SNAPSHOT_PATH="/run/aats-deploy/nats-target-smoke-lock.env"
NATS_TARGET_MANIFEST_SHA256="sha256:$(printf '3%.0s' {1..64})"
WSL_PROJECT="/tmp/aats"
DEPLOY_DIR="deploy/wsl2-dev"
COMPOSE_CMD_ARGS="-f docker-compose.yml -f docker-compose.aats.yml -f target.yml"
ALL_KNOWN_APP_CONTAINERS="aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon aats-liquidations-daemon aats-microstructure-collector"
LIQUIDATIONS_ID="$(printf '1%.0s' {1..64})"
MICROSTRUCTURE_ID="$(printf '2%.0s' {1..64})"
scenario="managed_collectors"
command_log="$(mktemp)"
trap 'rm -f "$command_log"' EXIT

log_info() { :; }
log_ok() { :; }
log_error() { :; }
assert_deploy_lock_held() { :; }
capture_new_app_quiescence_boundary() { :; }
ensure_nats_cutover_preflight_infra_up() { :; }
require_nats_durable_cutover_preflight() { :; }

wsl_run() {
    local command="$1"
    case "$command" in
        *"docker ps -a --format"*)
            if [[ "$scenario" == managed_collectors ]]; then
                printf '%s\n' \
                    "aats-liquidations-daemon" \
                    "aats-microstructure-collector"
            else
                printf 'aats-liquidations-daemon\n'
            fi
            ;;
        *"docker inspect --format "*"com.docker.compose.project"*)
            if [[ "$command" == *"'aats-liquidations-daemon'"* ]]; then
                if [[ "$scenario" == foreign_project_same_name ]]; then
                    printf '%s|foreign-project|aats-liquidations-daemon\n' "$LIQUIDATIONS_ID"
                elif [[ "$scenario" == foreign_service_same_name ]]; then
                    printf '%s|aats-dev|foreign-liquidations\n' "$LIQUIDATIONS_ID"
                else
                    printf '%s|aats-dev|aats-liquidations-daemon\n' "$LIQUIDATIONS_ID"
                fi
            elif [[ "$command" == *"'aats-microstructure-collector'"* ]]; then
                printf '%s|aats-dev|aats-microstructure-collector\n' "$MICROSTRUCTURE_ID"
            else
                printf 'unexpected inspect target: %s\n' "$command" >&2
                return 96
            fi
            ;;
        *"docker stop --time 15"*)
            printf '%s\n' "$command" >>"$command_log"
            ;;
        *"docker compose"*" down --timeout 5"*)
            printf '%s\n' "$command" >>"$command_log"
            ;;
        *)
            printf 'unexpected wsl command: %s\n' "$command" >&2
            return 97
            ;;
    esac
}

# Managed same-name containers are first bound to their exact project/service
# identities and then stopped only by immutable 64-hex container IDs.
: >"$command_log"
scenario=managed_collectors
step_down
captured_stop="$(grep -F 'docker stop --time 15' "$command_log")"
captured_down="$(grep -F 'docker compose' "$command_log")"
[[ "$captured_stop" == *"$LIQUIDATIONS_ID"* ]]
[[ "$captured_stop" == *"$MICROSTRUCTURE_ID"* ]]
[[ "$captured_stop" != *"aats-liquidations-daemon"* ]]
[[ "$captured_stop" != *"aats-microstructure-collector"* ]]
[[ "$captured_down" == *"AATS_RUNTIME_READINESS_GENERATION='$RUNTIME_READINESS_GENERATION'"* ]]
[[ "$captured_down" == *"AATS_DEPLOYED_GIT_COMMIT='$DEPLOYED_GIT_COMMIT'"* ]]
[[ "$captured_down" == *"AATS_NATS_TARGET_ENV_SNAPSHOT_PATH='$NATS_TARGET_ENV_SNAPSHOT_PATH'"* ]]
[[ "$captured_down" == *"AATS_NATS_TARGET_MANIFEST_SHA256='$NATS_TARGET_MANIFEST_SHA256'"* ]]
[[ "$captured_down" == *"docker compose $COMPOSE_CMD_ARGS down --timeout 5"* ]]

# A foreign container reusing a managed name is never stopped, and the later
# Compose/NATS mutation path is not reached.
for scenario in foreign_project_same_name foreign_service_same_name; do
    : >"$command_log"
    set +e
    ( step_down )
    foreign_status=$?
    set -e
    [[ "$foreign_status" -eq 10 ]]
    if [[ -s "$command_log" ]]; then
        printf 'foreign same-name container caused a mutation:\n' >&2
        cat "$command_log" >&2
        exit 98
    fi
done
