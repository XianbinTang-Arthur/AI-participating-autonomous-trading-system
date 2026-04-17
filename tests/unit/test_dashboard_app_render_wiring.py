"""Regression guard: app.js must pass every field the view actually reads.

Background — the bug this guards against:

The commit a5218fb (feat: rebuild rdp workbench task flow) added new fields
`rdpWorkbenchOverview / rdpWorkbenchItems / rdpWorkbenchAlerts /
rdpTuningOverview / rdpTuningProposals` to ai-config-view.js and extended
store.js's viewSpec to fetch them into state.data. But the commit forgot to
update the `renderAIConfigView({...})` call site inside app.js, so all five
workbench/tuning panels arrived at the view as `undefined` → fell through to
`|| {}`, which made the view's `Object.keys(rdpWorkbenchOverview).length === 0`
guard fire unconditionally — the live dashboard showed only the placeholder
callout "RDP 数据暂未就绪" no matter what the backend returned.

The existing test_dashboard_ui suite never caught this because it calls
`renderAIConfigView(...)` directly with hand-rolled data; it never runs app.js.
This test fills that gap with a static check: for every `renderXxxView({…})`
call site in app.js, assert that the object literal passed in includes every
top-level key that the corresponding view module reads off `data`.

If the view later starts reading a new key, store.js must fetch it (fails in
test_dashboard_ui), and app.js must pass it through (fails here).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "aats" / "api" / "static"
APP_JS = STATIC_DIR / "app.js"
VIEW_DIR = STATIC_DIR / "modules" / "views"


def _render_call_keys(app_source: str, render_fn: str) -> set[str]:
    """Extract the top-level keys passed to ``renderFnName({...})`` in app.js.

    Handles the shape ``renderFnName({ key1: ..., key2: ... })`` by scanning
    the argument block with brace-balance counting so nested object literals
    don't confuse the parser. Only the OUTERMOST keys are returned — exactly
    what the view's destructure reads from ``data``.
    """
    pattern = re.compile(re.escape(render_fn) + r"\s*\(")
    match = pattern.search(app_source)
    assert match, f"{render_fn} call not found in app.js"
    cursor = match.end()
    # Find the start of the object literal argument.
    while cursor < len(app_source) and app_source[cursor].isspace():
        cursor += 1
    assert app_source[cursor] == "{", f"{render_fn} first arg is not an object literal"
    start = cursor
    depth = 0
    end = -1
    for idx in range(start, len(app_source)):
        char = app_source[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = idx
                break
    assert end > start, f"unterminated object literal for {render_fn}"
    body = app_source[start + 1 : end]

    # Walk the body, collecting identifiers that sit at depth 0 and are
    # followed by ``:``. Depth tracks nested braces / brackets / parens so we
    # don't mistake a value's inner key for an outer key.
    keys: set[str] = set()
    depth = 0
    token = ""
    i = 0
    while i < len(body):
        char = body[i]
        if char in "({[":
            depth += 1
        elif char in ")}]":
            depth -= 1
        elif depth == 0 and char == ":":
            name = token.strip().strip("'").strip('"')
            name = name.split()[-1] if name else ""
            if re.fullmatch(r"\w+", name):
                keys.add(name)
            token = ""
        elif depth == 0 and char == ",":
            token = ""
        else:
            token += char
        i += 1
    return keys


def _view_reads_from_data(view_source: str) -> set[str]:
    """Return top-level keys that the view reads off ``data``.

    Scans for both ``const x = data.key`` / ``data.key ||`` forms and the
    destructuring form ``const { key1, key2 } = data``. Keeps it permissive —
    false positives are fine (the check only fails if app.js forgets a key the
    view is known to read).
    """
    keys: set[str] = set()
    for match in re.finditer(r"\bdata\.(\w+)", view_source):
        keys.add(match.group(1))
    # data?.key is common too
    for match in re.finditer(r"\bdata\?\.(\w+)", view_source):
        keys.add(match.group(1))
    # Destructuring: const { a, b, c } = data
    for match in re.finditer(
        r"\b(?:const|let|var)\s*\{([^}]*)\}\s*=\s*data\b", view_source, re.DOTALL
    ):
        body = match.group(1)
        for raw in body.split(","):
            cleaned = raw.strip().split(":")[0].split("=")[0].strip()
            if re.fullmatch(r"\w+", cleaned):
                keys.add(cleaned)
    return keys


class TestDashboardRenderWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.app_source = APP_JS.read_text(encoding="utf-8")

    def _check(self, render_fn: str, view_filename: str) -> None:
        view_source = (VIEW_DIR / view_filename).read_text(encoding="utf-8")
        passed_keys = _render_call_keys(self.app_source, render_fn)
        read_keys = _view_reads_from_data(view_source)
        missing = sorted(read_keys - passed_keys)
        self.assertFalse(
            missing,
            msg=(
                f"{view_filename} reads data.{missing!r} but app.js's "
                f"{render_fn}(...) call site does not include those keys. "
                f"The view will see them as undefined and fall through to "
                f"whatever placeholder it renders on empty data. Add the "
                f"missing keys to the {render_fn} call in app.js."
            ),
        )

    def test_ai_config_view_receives_all_keys_it_reads(self) -> None:
        """B2 regression: a5218fb added 5 rdp*Workbench*/rdp*Tuning* keys plus
        errors/authProviders to ai-config-view.js but forgot to update the
        call site. The live dashboard showed only the "RDP 数据暂未就绪"
        placeholder because all five panels arrived undefined."""
        self._check("renderAIConfigView", "ai-config-view.js")


if __name__ == "__main__":
    unittest.main()
