#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# This is a cross-boundary provenance contract: create an isolated WSL Git
# checkout, skip sync, and prove that every class of dirtiness fails closed.
if [[ "$(uname -s)" != MINGW* ]]; then
    printf 'SKIP: requires Windows Git Bash -> WSL\n'
    exit 0
fi

# shellcheck source=../../scripts/deploy.sh
source "$PROJECT_ROOT/scripts/deploy.sh"
log_info() { :; }
log_ok() { :; }
log_warn() { :; }
log_error() { :; }

WSL_PROJECT="$(MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" mktemp -d /tmp/aats-deploy-provenance-smoke.XXXXXX | tr -d '\r')"
case "$WSL_PROJECT" in
    /tmp/aats-deploy-provenance-smoke.*) ;;
    *)
        printf 'unsafe WSL smoke path: %s\n' "$WSL_PROJECT" >&2
        exit 110
        ;;
esac
cleanup() {
    case "$WSL_PROJECT" in
        /tmp/aats-deploy-provenance-smoke.*)
            MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" rm -rf -- "$WSL_PROJECT" 2>/dev/null || true
            ;;
    esac
}
trap cleanup EXIT

MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "set -e; git -C '$WSL_PROJECT' init -q; git -C '$WSL_PROJECT' config user.email smoke@example.invalid; git -C '$WSL_PROJECT' config user.name AATS-Smoke; printf 'baseline\\n' > '$WSL_PROJECT/tracked.txt'; git -C '$WSL_PROJECT' add tracked.txt; git -C '$WSL_PROJECT' commit -qm baseline"
assert_wsl_checkout_clean "clean-smoke"

SKIP_SYNC=true
step_sync
MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "printf 'untracked\\n' > '$WSL_PROJECT/untracked.txt'"
set +e
assert_wsl_checkout_clean "skip-sync-untracked-smoke"
dirty_status=$?
set -e
if [[ "$dirty_status" -ne 19 ]]; then
    printf 'dirty WSL checkout was accepted after --skip-sync: %s\n' "$dirty_status" >&2
    exit 111
fi

MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" bash -c "rm -f -- '$WSL_PROJECT/untracked.txt'; printf 'changed\\n' >> '$WSL_PROJECT/tracked.txt'; git -C '$WSL_PROJECT' add tracked.txt"
set +e
assert_wsl_checkout_clean "skip-sync-staged-smoke"
staged_status=$?
set -e
if [[ "$staged_status" -ne 19 ]]; then
    printf 'staged WSL checkout was accepted after --skip-sync: %s\n' "$staged_status" >&2
    exit 112
fi
