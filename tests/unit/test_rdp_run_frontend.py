from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _node(script: str) -> str:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_rdp_run_center_renders_truthful_wait_progress_and_actions() -> None:
    output = _node(
        """
import { renderRdpControlPanelV2 } from './aats/api/static/modules/views/rdp-control-panel.js';

const html = renderRdpControlPanelV2({
  rdpRuns: {
    items: [
      {
        run_id: 'run_manual_1',
        workflow: 'research_cycle',
        status: 'queued',
        trigger_kind: 'manual',
        eligible_at: '2020-01-01T00:00:00Z',
        created_at: new Date().toISOString(),
        completed_steps: 0,
        total_steps: 0,
      },
      {
        run_id: 'run_active_2',
        workflow: 'governance_cycle',
        status: 'running',
        trigger_kind: 'schedule',
        current_step_key: 'quality_monitor',
        heartbeat_at: new Date().toISOString(),
        completed_steps: 1,
        total_steps: 3,
      },
    ],
  },
  rdpControl: {},
  rdpWorkbenchOverview: {},
  rdpWorkbenchItems: {},
  rdpWorkbenchAlerts: {},
  canAdmin: true,
});

console.log(JSON.stringify({
  hasCenter: html.includes('运行中心'),
  truthfulQueue: html.includes('正在等待执行槽') && html.includes('优先于尚未启动的定时任务'),
  runningStep: html.includes('当前步骤：quality_monitor') && html.includes('1/3 步'),
  actions: html.includes('data-action="rdp-open-run"') && html.includes('data-action="rdp-cancel-run"'),
}));
"""
    )
    assert '"hasCenter":true' in output
    assert '"truthfulQueue":true' in output
    assert '"runningStep":true' in output
    assert '"actions":true' in output


def test_rdp_trigger_uses_v2_and_reuses_one_idempotency_key() -> None:
    output = _node(
        """
import { createRdpActionHandlers } from './aats/api/static/modules/actions/rdp-actions.js';

const requests = [];
const refreshStates = [];
const state = { actionInFlight: false, flash: null, data: {}, ui: { rdp: { idempotencyKeys: {} } } };
const handlers = createRdpActionHandlers({
  beginAction: () => {
    state.actionInFlight = true;
    return () => { state.actionInFlight = false; };
  },
  renderBanners: () => {},
  refreshDashboard: async () => {},
  refreshPanels: async (panels) => refreshStates.push({ panels, actionInFlight: state.actionInFlight }),
  requestJson: async (path, options = {}) => {
    requests.push({ path, headers: options.headers || {}, body: options.body || {} });
    return { run: { run_id: 'run_123', status: 'queued' }, idempotent_replay: false };
  },
  state,
  windowRef: {
    confirm: () => true,
    crypto: { randomUUID: () => 'uuid-fixed' },
  },
});

await handlers['rdp-trigger-workflow']('research_cycle');
const request = requests[0];
console.log(JSON.stringify({
  path: request.path,
  headerKey: request.headers['Idempotency-Key'],
  bodyKey: request.body.idempotency_key,
  actorOmitted: !Object.prototype.hasOwnProperty.call(request.body, 'actor'),
  keyCleared: Object.keys(state.ui.rdp.idempotencyKeys).length === 0,
  optimisticRun: state.data.rdpRuns.items[0].run_id,
  targetedAfterUnlock: refreshStates[0].panels[0] === 'rdpRuns' && !refreshStates[0].actionInFlight,
}));
"""
    )
    assert '"path":"/rdp/v2/runs"' in output
    assert '"headerKey":"ui-research_cycle-uuid-fixed"' in output
    assert '"bodyKey":"ui-research_cycle-uuid-fixed"' in output
    assert '"actorOmitted":true' in output
    assert '"keyCleared":true' in output
    assert '"optimisticRun":"run_123"' in output
    assert '"targetedAfterUnlock":true' in output


def test_rdp_run_detail_cancel_and_retry_handlers_use_v2_resources() -> None:
    output = _node(
        """
import { createRdpActionHandlers } from './aats/api/static/modules/actions/rdp-actions.js';

const requests = [];
const drawers = [];
const refreshed = [];
const state = { actionInFlight: false, flash: null, data: {}, ui: { rdp: {} } };
const handlers = createRdpActionHandlers({
  beginAction: () => {
    state.actionInFlight = true;
    return () => { state.actionInFlight = false; };
  },
  openDrawer: (payload) => drawers.push(payload),
  renderBanners: () => {},
  refreshDashboard: async () => {},
  refreshPanels: async (panels) => refreshed.push(panels),
  requestJson: async (path, options = {}) => {
    requests.push({ path, method: options.method || 'GET' });
    return {
      run: {
        run_id: 'run_123',
        workflow: 'research_cycle',
        status: 'succeeded',
        completed_steps: 1,
        total_steps: 1,
      },
      attempts: [],
      steps: [{ step_key: 'research_cycle', status: 'succeeded', attempt_no: 1 }],
      events: [],
    };
  },
  state,
  windowRef: { confirm: () => true },
});

await handlers['rdp-open-run']('run_123', null);
await handlers['rdp-cancel-run']('run_123');
await handlers['rdp-retry-run']('run_123');
console.log(JSON.stringify({
  paths: requests.map((item) => `${item.method} ${item.path}`),
  drawerOpened: drawers[0]?.title === 'run_123',
  terminalStepCopyIsTruthful: drawers[0]?.body.includes('当前没有正在执行的步骤')
    && !drawers[0]?.body.includes('当前无执行步骤')
    && drawers[0]?.body.includes('research_cycle'),
  targetedRefreshes: refreshed.every((panels) => panels.length === 1 && panels[0] === 'rdpRuns'),
}));
"""
    )
    assert '"GET /rdp/v2/runs/run_123"' in output
    assert '"POST /rdp/v2/runs/run_123/cancel"' in output
    assert '"POST /rdp/v2/runs/run_123/retry"' in output
    assert '"drawerOpened":true' in output
    assert '"terminalStepCopyIsTruthful":true' in output
    assert '"targetedRefreshes":true' in output
