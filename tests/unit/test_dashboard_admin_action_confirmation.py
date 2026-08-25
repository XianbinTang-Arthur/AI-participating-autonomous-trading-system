from __future__ import annotations

import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_sensitive_operator_account_actions_require_confirmation() -> None:
    script = r"""
import { createAdminActions } from './aats/api/static/modules/actions/admin-actions.js';

const confirmations = [];
const requests = [];
const elements = {
  changeRoleUsername: { value: 'operator-a' },
  changeRoleValue: { value: 'viewer' },
  resetPasswordUsername: { value: 'operator-a' },
  resetPasswordValue: { value: 'dummy-password-for-test' },
};
const handlers = createAdminActions({
  beginAction: () => { throw new Error('canceled action must not begin'); },
  documentRef: { getElementById: (id) => elements[id] || null },
  renderBanners: () => {},
  refreshDashboard: async () => {},
  requestJson: async (...args) => { requests.push(args); },
  state: {
    actionInFlight: false,
    refreshInFlight: false,
    data: { operatorUsers: { users: [{ username: 'operator-a', role: 'admin', enabled: true }] } },
  },
  windowRef: {
    confirm: (message) => {
      confirmations.push(message);
      return false;
    },
  },
});

await handlers.actionHandlers['toggle-user']('operator-a');
await handlers.actionHandlers['confirm-change-user-role']();
await handlers.actionHandlers['confirm-reset-user-password']();

console.log(JSON.stringify({ confirmations, requestCount: requests.length }));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["requestCount"] == 0
    assert len(payload["confirmations"]) == 3
    assert "立即改变该账号的登录与访问权限" in payload["confirmations"][0]
    assert "权限变更将立即生效" in payload["confirmations"][1]
    assert "原密码将立即失效" in payload["confirmations"][2]
    assert "dummy-password-for-test" not in "".join(payload["confirmations"])
