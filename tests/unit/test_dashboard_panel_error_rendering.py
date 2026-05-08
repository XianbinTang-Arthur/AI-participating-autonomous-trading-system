from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_module_script(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


class TestDashboardPanelErrorRendering(unittest.TestCase):
    def test_strategy_decision_history_shows_panel_error_instead_of_empty_state(self) -> None:
        script = r"""
import { renderStrategySections } from './aats/api/static/modules/views/strategy-view.js';

const html = renderStrategySections({
  errors: { recentDecisions: '当前操作需要先登录。' },
}).strategyHistory;

console.log(JSON.stringify({
  hasErrorTitle: html.includes('决策记录读取失败'),
  hasErrorCopy: html.includes('当前操作需要先登录。'),
  hasEmptyState: html.includes('当前暂无决策记录。'),
}));
"""
        result = _run_node_module_script(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["hasErrorTitle"])
        self.assertTrue(payload["hasErrorCopy"])
        self.assertFalse(payload["hasEmptyState"])

    def test_execution_recent_orders_and_fills_show_panel_errors_instead_of_empty_states(self) -> None:
        script = r"""
import { renderExecutionSections } from './aats/api/static/modules/views/execution-view.js';

const sections = renderExecutionSections({
  errors: {
    recentOrders: '委托接口读取失败。',
    recentFills: '成交接口读取失败。',
  },
});

console.log(JSON.stringify({
  ordersHasErrorTitle: sections.executionOrders.includes('委托记录读取失败'),
  ordersHasErrorCopy: sections.executionOrders.includes('委托接口读取失败。'),
  ordersHasEmptyState: sections.executionOrders.includes('当前暂无委托记录。'),
  fillsHasErrorTitle: sections.executionFills.includes('成交记录读取失败'),
  fillsHasErrorCopy: sections.executionFills.includes('成交接口读取失败。'),
  fillsHasEmptyState: sections.executionFills.includes('当前暂无成交记录。'),
}));
"""
        result = _run_node_module_script(script)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ordersHasErrorTitle"])
        self.assertTrue(payload["ordersHasErrorCopy"])
        self.assertFalse(payload["ordersHasEmptyState"])
        self.assertTrue(payload["fillsHasErrorTitle"])
        self.assertTrue(payload["fillsHasErrorCopy"])
        self.assertFalse(payload["fillsHasEmptyState"])
