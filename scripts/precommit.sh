#!/usr/bin/env bash
# Pre-commit guard — reject staged diffs that re-introduce forbidden patterns.
#
# Source: docs/task/rdp_hardening_batch_a_detailed_design.md §9.2
#
# The patterns below were removed in RDP hardening batch A. A PR that
# re-introduces any of them is almost certainly reverting a security fix
# and must be stopped at commit time.
#
# Wiring options:
#   1. Git hook (per-developer):
#        ln -s ../../scripts/precommit.sh .git/hooks/pre-commit
#   2. CI step (authoritative): invoke from GitHub Actions / deploy pipeline.
#   3. Smoke gate: called from scripts/deploy.sh before build.
#
# Exit code:
#   0 — staged diff is clean
#   1 — at least one forbidden pattern found

set -u

forbidden_patterns=(
    "bypassed_frozen"
    "apply-frozen"
    "action_apply_frozen"
    "RDP_PRODUCTION_APPLY_ENABLED"
    "skip_gate=True"
)

# The guard itself and the smoke test necessarily mention these strings —
# exclude them from the scan via git pathspec exclusions (':!').
exclude_paths=(
    ':!scripts/precommit.sh'
    ':!tests/smoke/test_rdp_legacy_scripts_disabled.sh'
    ':!docs/**'
)

# If we're running from a git hook, default to the staged diff. Allow ops to
# also point us at arbitrary refs via argv (e.g. `scripts/precommit.sh HEAD~5 HEAD`).
if [[ $# -gt 0 ]]; then
    diff_cmd=(git diff "$@" -- "${exclude_paths[@]}")
else
    diff_cmd=(git diff --cached -- "${exclude_paths[@]}")
fi

staged_diff="$("${diff_cmd[@]}")"

violations=0
for pattern in "${forbidden_patterns[@]}"; do
    # Only scan the "+" side of the diff (lines being added); skip "+++" file
    # headers so filename matches don't false-positive.
    hits="$(grep -E '^\+[^+]' <<<"${staged_diff}" | grep -F -- "${pattern}" || true)"
    if [[ -n "${hits}" ]]; then
        echo "FORBIDDEN: staged diff re-introduces '${pattern}':" >&2
        echo "${hits}" >&2
        violations=$((violations + 1))
    fi
done

if [[ ${violations} -gt 0 ]]; then
    echo "" >&2
    echo "Pre-commit rejected: ${violations} forbidden pattern(s) found." >&2
    echo "See docs/task/rdp_hardening_batch_a_detailed_design.md §9.2 for rationale." >&2
    exit 1
fi

exit 0
