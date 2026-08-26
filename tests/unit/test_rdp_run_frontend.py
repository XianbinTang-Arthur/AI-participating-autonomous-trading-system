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
import { renderRdpControlPanelV3 } from './aats/api/static/modules/views/rdp-control-panel.js';

const html = renderRdpControlPanelV3({
  workspace: {
    schema_version: 'rdp.workspace.v3',
    environment: { name: 'derivatives' },
    health: { overall_health: 'healthy' },
    lifecycle: { current_stage: 'research', stages: [] },
    research: { overview: {}, items: [], alerts: {} },
    release: { candidates: [], observations: [], active_parameters: {}, selection_status: 'no_eligible_candidate' },
    tuning: { proposals: [] },
    execution: {
      capacity: 1,
      active_count: 1,
      queued_count: 1,
      daemon: { fresh: true, status: 'busy', status_label: '执行中' },
      active_run:
      {
        run_id: 'run_active_2',
        workflow: 'governance_cycle',
        status: 'running',
        status_label: '运行中',
        trigger_kind: 'schedule',
        current_step_key: 'quality_monitor',
        heartbeat_at: new Date().toISOString(),
        completed_steps: 1,
        total_steps: 3,
      },
      queued_runs: [{
        run_id: 'run_manual_1',
        workflow: 'research_cycle',
        status: 'queued',
        status_label: '等待执行',
        queue_position: 1,
        waiting_reason: '唯一研究执行槽正在运行“治理周期”。',
        created_at: new Date().toISOString(),
      }],
      recent_runs: [],
      queue_explanation: 'RDP 使用单执行槽保护研究产物一致性。',
    },
    workflows: [],
  },
  canAdmin: true,
});

console.log(JSON.stringify({
  hasCenter: html.includes('运行与执行队列'),
  truthfulQueue: html.includes('唯一研究执行槽正在运行“治理周期”') && html.includes('队列位次 1'),
  runningStep: html.includes('数据质量检查') && html.includes('1/3 步'),
  actions: html.includes('data-action="rdp-open-run"') && html.includes('data-action="rdp-cancel-run"'),
}));
"""
    )
    assert '"hasCenter":true' in output
    assert '"truthfulQueue":true' in output
    assert '"runningStep":true' in output
    assert '"actions":true' in output


def test_rdp_data_governance_renders_bounded_truthful_snapshot() -> None:
    output = _node(
        """
import { renderRdpControlPanelV3 } from './aats/api/static/modules/views/rdp-control-panel.js';

const html = renderRdpControlPanelV3({
  workspace: {
    schema_version: 'rdp.workspace.v3',
    health: {},
    lifecycle: { stages: [] },
    research: { overview: {}, items: [], alerts: {} },
    release: { candidates: [], active_parameters: {} },
    tuning: { proposals: [] },
    execution: {},
    workflows: [],
    data_governance: {
      status: 'unknown',
      coverage: {
        status: 'unknown',
        next_action: '运行只读数据覆盖审计',
        recovery_matrix: [{
          dataset: 'staging.raw_liquidations',
          observed_status: 'collector_unknown',
          classification: 'prospective_only',
          priority: 'P0',
          next_action: '等待真实连续采集',
        }],
      },
      historical_imports: { status: 'available', total_recent: 2 },
      live_collection: { status: 'available', channel_count: 3, drop_count: 1 },
      archives: { status: 'available', partition_count: 4, blocked_count: 1 },
      eligibility: { status: 'available', bundle_count: 1 },
      rebuilds: { status: 'available', total_recent: 1 },
    },
  },
});

console.log(JSON.stringify({
  headings: ['数据覆盖', '历史导入', '实时采集', '不可变归档', '质量资格', '确定性重建', '监控告警']
    .every((value) => html.includes(value)),
  unknownIsTruthful: html.includes('数据证据不完整') && html.includes('运行只读数据覆盖审计'),
  dropVisible: html.includes('丢弃') && html.includes('>1<'),
  recoveryTruth: html.includes('staging.raw_liquidations')
    && html.includes('prospective_only')
    && html.includes('等待真实连续采集'),
  noLiveAction: !html.includes('应用参数') && !html.includes('启动实盘'),
}));
"""
    )
    assert '"headings":true' in output
    assert '"unknownIsTruthful":true' in output
    assert '"dropVisible":true' in output
    assert '"recoveryTruth":true' in output
    assert '"noLiveAction":true' in output


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
  optimisticRun: state.data.rdpWorkspace.execution.queued_runs[0].run_id,
  targetedAfterUnlock: refreshStates[0].panels[0] === 'rdpWorkspace' && !refreshStates[0].actionInFlight,
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
    && drawers[0]?.body.includes('步骤 research_cycle'),
  targetedRefreshes: refreshed.every((panels) => panels.length === 1 && panels[0] === 'rdpWorkspace'),
}));
"""
    )
    assert '"GET /rdp/v2/runs/run_123"' in output
    assert '"POST /rdp/v2/runs/run_123/cancel"' in output
    assert '"POST /rdp/v2/runs/run_123/retry"' in output
    assert '"drawerOpened":true' in output
    assert '"terminalStepCopyIsTruthful":true' in output
    assert '"targetedRefreshes":true' in output
