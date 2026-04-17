#!/usr/bin/env bash
# Smoke test — A-0.2 Legacy RDP scripts must refuse to run (exit code 2).
#
# Runs every script in the "must disable" list from
# docs/task/rdp_hardening_batch_a_detailed_design.md §3.2 and asserts:
#   - exit code == 2
#   - stderr mentions replacement API path
#
# Usage:
#   bash tests/smoke/test_rdp_legacy_scripts_disabled.sh
#
# Exit code:
#   0 — all 9 scripts correctly disabled
#   1 — at least one script failed the check

set -u

# Locate the repo root regardless of CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}" || exit 1

# Pick a python interpreter: prefer the Windows venv if present, else fall back
# to the system python3 (WSL2 / CI Linux).
if [[ -x ".venv/Scripts/python.exe" ]]; then
    PYTHON=".venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    PYTHON="$(command -v python)"
else
    echo "FAIL: no python interpreter found" >&2
    exit 1
fi

SCRIPTS=(
    apply_active_parameter_set
    approve_recommendation_and_apply
    rdp_apply_approved_recommendation
    rdp_approve_recommendation
    rdp_rollback_active_parameter_set
    rdp_freeze_parameter_set
    rdp_create_parameter_release
    rdp_run_release_cycle
    rdp_update_decision_registry
)

failures=0
for script in "${SCRIPTS[@]}"; do
    stderr_capture="$("${PYTHON}" "scripts/${script}.py" 2>&1 >/dev/null)"
    exit_code=$?
    if [[ ${exit_code} -ne 2 ]]; then
        echo "FAIL: ${script} exit=${exit_code} (expected 2)" >&2
        failures=$((failures + 1))
        continue
    fi
    # Match on an ASCII token to stay encoding-agnostic (Windows Python prints
    # the Chinese body in GBK, WSL2 in UTF-8).
    if ! grep -q "rdp_hardening_batch_a_detailed_design" <<<"${stderr_capture}"; then
        echo "FAIL: ${script} stderr did not reference batch A design doc" >&2
        failures=$((failures + 1))
        continue
    fi
    echo "OK: ${script} correctly disabled (exit 2)"
done

if [[ ${failures} -gt 0 ]]; then
    echo ""
    echo "SMOKE FAILED: ${failures}/${#SCRIPTS[@]} script(s) not disabled correctly" >&2
    exit 1
fi

echo ""
echo "SMOKE PASS: all ${#SCRIPTS[@]} legacy scripts correctly refuse to run"
exit 0
