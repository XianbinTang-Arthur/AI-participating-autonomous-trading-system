from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class TestDashboardRefreshInteractivity(unittest.TestCase):
    def test_sync_refresh_disabled_buttons_locks_and_restores_scope_buttons(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import { syncRefreshDisabledButtons } from './aats/api/static/modules/refresh-interactivity.js';

function createButton({ disabled = false, title = null } = {}) {
  const attributes = new Map();
  if (title !== null) attributes.set('title', title);
  return {
    disabled,
    dataset: {},
    getAttribute(name) {
      return attributes.has(name) ? attributes.get(name) : null;
    },
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
    removeAttribute(name) {
      attributes.delete(name);
    },
  };
}

function createRoot(buttons) {
  return {
    querySelectorAll(selector) {
      return selector === 'button' ? buttons : [];
    },
  };
}

const pageButton = createButton({ title: '页面按钮' });
const drawerButton = createButton();
const alreadyDisabledButton = createButton({ disabled: true, title: '原本就不可点' });

syncRefreshDisabledButtons({
  roots: [createRoot([pageButton]), createRoot([drawerButton, alreadyDisabledButton])],
  refreshing: true,
  reason: '当前区域正在刷新，请等待刷新完成后再操作。',
});

const lockedState = {
  pageDisabled: pageButton.disabled === true,
  pageTitle: pageButton.getAttribute('title') === '当前区域正在刷新，请等待刷新完成后再操作。',
  drawerDisabled: drawerButton.disabled === true,
  drawerTitle: drawerButton.getAttribute('title') === '当前区域正在刷新，请等待刷新完成后再操作。',
  preservedDisabled: alreadyDisabledButton.disabled === true,
  preservedTitle: alreadyDisabledButton.getAttribute('title') === '原本就不可点',
};

syncRefreshDisabledButtons({
  roots: [createRoot([pageButton]), createRoot([drawerButton, alreadyDisabledButton])],
  refreshing: false,
});

console.log(JSON.stringify({
  ...lockedState,
  pageRestored: pageButton.disabled === false && pageButton.getAttribute('title') === '页面按钮',
  drawerRestored: drawerButton.disabled === false && drawerButton.getAttribute('title') === null,
  preservedStillDisabled: alreadyDisabledButton.disabled === true,
  preservedStillTitle: alreadyDisabledButton.getAttribute('title') === '原本就不可点',
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('"pageDisabled":true', result.stdout)
        self.assertIn('"pageTitle":true', result.stdout)
        self.assertIn('"drawerDisabled":true', result.stdout)
        self.assertIn('"drawerTitle":true', result.stdout)
        self.assertIn('"preservedDisabled":true', result.stdout)
        self.assertIn('"preservedTitle":true', result.stdout)
        self.assertIn('"pageRestored":true', result.stdout)
        self.assertIn('"drawerRestored":true', result.stdout)
        self.assertIn('"preservedStillDisabled":true', result.stdout)
        self.assertIn('"preservedStillTitle":true', result.stdout)


if __name__ == "__main__":
    unittest.main()
