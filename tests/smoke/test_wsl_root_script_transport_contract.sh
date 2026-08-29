#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export AATS_DEPLOY_TEST_MODE=true

# shellcheck source=scripts/deploy.sh
source "$repo_root/scripts/deploy.sh"

# Exercise the exact encoded command emitted by wsl_root_run_script without
# requiring Docker or root.  The previous direct multiline transport lost the
# inner variables while crossing Git Bash -> wsl.exe -> bash -c.
if [[ "${AATS_TEST_REAL_WSL_ROOT_TRANSPORT:-false}" != "true" ]]; then
    wsl_root_run() {
        bash -c "$1"
    }
fi

printf -v payload '%s\n' \
    'set -euo pipefail' \
    "runtime_dir='/run/aats-deploy'" \
    'target="$runtime_dir/nats-target-contract.env"' \
    'test "$runtime_dir" = '\''/run/aats-deploy'\''' \
    'test "$target" = '\''/run/aats-deploy/nats-target-contract.env'\''' \
    'printf '\''%s|%s\n'\'' "$runtime_dir" "$target"'
if [[ "${AATS_TEST_REAL_WSL_ROOT_TRANSPORT:-false}" == "true" ]]; then
    wsl_root_run_script "$payload" >/dev/null
else
    observed="$(wsl_root_run_script "$payload")"
    [[ "$observed" == "/run/aats-deploy|/run/aats-deploy/nats-target-contract.env" ]]
fi

if wsl_root_run_script "" >/dev/null 2>&1; then
    echo "empty root script unexpectedly accepted" >&2
    exit 1
fi
