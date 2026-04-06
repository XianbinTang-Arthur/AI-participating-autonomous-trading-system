"""Static checks that aats/api/static/app.js correctly imports every helper
it actually references from sibling modules.

This catches a class of bug that the existing dashboard test suites do NOT
catch: app.js references a name that is exported by another module but never
imported into app.js. The reference parses fine (JS does not validate names
at module load), so syntax checks miss it; the page loads fine until renderShell
hits the offending code path, at which point a ReferenceError aborts the entire
finally block of refreshDashboard / refreshDeferredPanels and the dashboard
gets stuck mid-refresh — refresh chip frozen on "加载中", buttons stuck in
the "正在确认权限" / "is-refresh-locked" pre-render state.

The original incident: `resumeActionHintText()` in app.js called
`operationalStatusCopy(...)` (an export of modules/terms.js) without importing
it. The Python tests for the dashboard rely on FastAPI templates and never
exercise the JS module graph, so they let the bug ship.

These tests parse app.js and verify that every name appearing as a function
call in it that ALSO happens to be exported by a sibling module is in app.js's
import list for that module.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "aats" / "api" / "static"
APP_JS = STATIC_DIR / "app.js"


def _module_exports(module_path: Path) -> set[str]:
    """Run `node` to import the module and list its exports.

    Using node here (instead of regex-scanning the source) keeps us robust
    against re-exports, default exports, etc. The unit test suite already
    depends on node being available — see test_dashboard_refresh_interactivity.
    """
    file_url = module_path.resolve().as_uri()
    result = subprocess.run(
        [
            "node",
            "-e",
            f"import('{file_url}').then(m => console.log(JSON.stringify(Object.keys(m).sort())))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to read exports from {module_path}: {result.stderr.strip()}"
        )
    import json

    return set(json.loads(result.stdout.strip()))


def _imported_names_from(app_source: str, module_relative_path: str) -> set[str]:
    """Parse app.js's `import { … } from "<module_relative_path>"` block.

    Handles both single-line and multi-line import forms.
    """
    pattern = re.compile(
        r"import\s*\{([^}]*)\}\s*from\s*[\"']" + re.escape(module_relative_path) + r"[\"']",
        re.MULTILINE,
    )
    names: set[str] = set()
    for match in pattern.finditer(app_source):
        body = match.group(1)
        for raw in body.split(","):
            cleaned = raw.strip().split(" as ")[0].strip()
            if cleaned:
                names.add(cleaned)
    return names


def _called_names(app_source: str, candidate_names: set[str]) -> set[str]:
    """Return the subset of candidate_names that appear in app_source as a
    function call (`name(`) or as a bare reference inside an argument list.

    The pattern matches `name(` with a word boundary in front, so it won't
    pick up `obj.name(` (which would be a property access on `obj`, not a
    reference to the bare name).
    """
    used: set[str] = set()
    for name in candidate_names:
        # word-boundary, then name, then either `(` (function call) or
        # `,` / `}` / `\n` (use as a value, e.g. callback reference).
        pattern = re.compile(r"(?<![\w$.])" + re.escape(name) + r"\s*\(")
        if pattern.search(app_source):
            used.add(name)
    return used


def _locally_bound_names(app_source: str) -> set[str]:
    """Names defined inside app.js itself.

    Includes function/async function declarations, top-level const/let/var
    bindings, and destructuring assignments (e.g. ``const { a, b } = obj``).
    Such names don't need to be imported even if a sibling module happens to
    export a same-named function — they're shadowed by the local binding.

    This is what protects against false positives like ``isRefreshInFlight``,
    which is destructured out of ``refreshController`` (the controller object
    returned by ``createDashboardRefreshController``) and just happens to be
    a name that ``modules/store.js`` also exports.
    """
    names: set[str] = set()

    # function foo(...) / async function foo(...)
    for match in re.finditer(r"\bfunction\s+(\w+)", app_source):
        names.add(match.group(1))

    # const/let/var foo = ...   (single binding, not destructured)
    for match in re.finditer(r"\b(?:const|let|var)\s+(\w+)\s*=", app_source):
        names.add(match.group(1))

    # const/let/var { foo, bar, baz } = ...   (object destructuring,
    # possibly spanning multiple lines).  We don't try to handle nested
    # patterns — app.js uses only flat destructurings.
    for match in re.finditer(
        r"\b(?:const|let|var)\s*\{([^}]*)\}\s*=", app_source, re.DOTALL
    ):
        body = match.group(1)
        for raw in body.split(","):
            cleaned = raw.strip()
            if not cleaned:
                continue
            # `foo: bar` aliases the property `foo` to local name `bar`.
            if ":" in cleaned:
                cleaned = cleaned.split(":", 1)[1].strip()
            # `foo = defaultValue` — the bound name is still `foo`.
            if "=" in cleaned:
                cleaned = cleaned.split("=", 1)[0].strip()
            if re.fullmatch(r"\w+", cleaned):
                names.add(cleaned)

    return names


class TestDashboardAppImports(unittest.TestCase):
    """Verify app.js does not call any sibling-module export it forgot to
    import.

    Each `test_*` method covers one sibling module — extending coverage to a
    new module is just adding a one-liner sub-test.
    """

    def setUp(self) -> None:
        self.app_source = APP_JS.read_text(encoding="utf-8")
        self.local_names = _locally_bound_names(self.app_source)

    def _check_module(self, module_relative_path: str) -> None:
        module_path = STATIC_DIR / module_relative_path.lstrip("./")
        exports = _module_exports(module_path)
        self.assertTrue(
            exports,
            msg=f"{module_relative_path} has no exports — sanity check failed",
        )
        called = _called_names(self.app_source, exports)
        # A name that is locally bound in app.js (function declaration,
        # const/let/var, or destructured out of a controller object) is
        # shadowed by the local binding and does NOT need to be imported,
        # even if the sibling module happens to export the same name.
        called -= self.local_names
        imported = _imported_names_from(self.app_source, module_relative_path)
        missing = sorted(called - imported)
        self.assertFalse(
            missing,
            msg=(
                f"app.js calls {missing!r} but does not import them from "
                f"{module_relative_path}. This produces a runtime "
                f"ReferenceError the moment the offending code path runs, "
                f"which (e.g. for resumeActionHintText) is inside renderShell "
                f"and aborts every subsequent refresh. Add the missing names "
                f"to the import block."
            ),
        )

    def test_terms_module_imports_are_complete(self) -> None:
        """The original incident: operationalStatusCopy was called by
        resumeActionHintText() but never imported, freezing every renderShell
        call on first paint."""
        self._check_module("./modules/terms.js")

    def test_formatters_module_imports_are_complete(self) -> None:
        self._check_module("./modules/formatters.js")

    def test_flash_module_imports_are_complete(self) -> None:
        self._check_module("./modules/flash.js")

    def test_view_router_module_imports_are_complete(self) -> None:
        self._check_module("./modules/view-router.js")

    def test_navigation_state_module_imports_are_complete(self) -> None:
        self._check_module("./modules/navigation-state.js")

    def test_store_module_imports_are_complete(self) -> None:
        self._check_module("./modules/store.js")


if __name__ == "__main__":
    unittest.main()
