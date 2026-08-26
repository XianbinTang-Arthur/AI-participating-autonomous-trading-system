import { primaryStatusPanel, summaryStrip } from "../components.js";
import { escapeHtml } from "../formatters.js";
import {
  renderAction,
  selectionLabel,
  statusPill,
  statusTone,
  workflowLabel,
} from "./rdp/formatters.js";
import { renderLifecycle } from "./rdp/lifecycle.js";
import { renderRelease } from "./rdp/release.js";
import { renderResearch } from "./rdp/research.js";
import { renderRuns } from "./rdp/runs.js";

function environmentLabel(environment = {}) {
  const name = String(environment.name || environment.environment || "unknown").toLowerCase();
  if (name === "prod") return "生产环境";
  if (name === "staging") return "预发布环境";
  if (["paper", "simulation", "derivatives"].includes(name)) return "模拟环境";
  if (name === "dev") return "开发环境";
  return "环境待确认";
}

function healthLabel(status) {
  return {
    healthy: "RDP 健康",
    degraded: "RDP 降级",
    blocked: "RDP 阻断",
    not_initialized: "RDP 未初始化",
  }[status] || "RDP 状态未知";
}

function renderHero(workspace = {}, canAdmin = false) {
  const health = workspace.health || {};
  const overview = workspace.research?.overview || {};
  const execution = workspace.execution || {};
  const nextAction = workspace.next_action || {};
  const activeRun = execution.active_run || null;
  const title = activeRun
    ? `${workflowLabel(activeRun.workflow)}正在运行`
    : (overview.headline || "RDP 研究运营工作台");
  const summary = activeRun
    ? `Run ${activeRun.run_id || "—"} 已占用研究执行槽；其他 Run 会显示真实队列原因。`
    : (nextAction.description || overview.subheadline || "从数据到发布的每一步都需要可追溯证据。");
  return primaryStatusPanel({
    eyebrow: "RDP PLATFORM V3",
    title: "研究运营控制面",
    headline: title,
    summary,
    tone: statusTone(health.overall_health),
    panelKey: "rdpWorkspace",
    pills: [
      statusPill(environmentLabel(workspace.environment || {}), "info"),
      statusPill(healthLabel(health.overall_health), statusTone(health.overall_health)),
      statusPill(`执行槽 ${execution.active_count || 0}/${execution.capacity || 1}`, activeRun ? "info" : "positive"),
      statusPill(`队列 ${execution.queued_count || 0}`, execution.queued_count ? "warning" : "neutral"),
    ],
    metrics: [
      { label: "当前阶段", value: (workspace.lifecycle?.stages || []).find((item) => item.key === workspace.lifecycle?.current_stage)?.label || "未知", tone: "info" },
      { label: "待审阅", value: String(workspace.research?.items?.length || 0), tone: workspace.research?.items?.length ? "warning" : "neutral" },
      { label: "发布候选", value: String(workspace.release?.candidates?.length || 0), tone: workspace.release?.candidates?.length ? "warning" : "neutral" },
      { label: "生效参数", value: String(Object.keys(workspace.release?.active_parameters || {}).length), tone: "neutral" },
    ],
    actions: nextAction.ui_action
      ? renderAction({
        label: nextAction.label,
        ui_action: nextAction.ui_action,
        value: nextAction.value,
        enabled: nextAction.enabled,
        disabled_reason: nextAction.description,
      }, canAdmin, "primary")
      : "",
  });
}

function renderWorkflowActions(workflows = [], canAdmin = false) {
  const preferredOrder = ["data_maintenance", "research_cycle", "governance_cycle", "decision_cycle"];
  const visible = [...workflows]
    .filter((workflow) => preferredOrder.includes(workflow.workflow))
    .sort((a, b) => preferredOrder.indexOf(a.workflow) - preferredOrder.indexOf(b.workflow));
  if (!visible.length) return "";
  return `
    <section class="rdp-v3-workflow-bar" aria-label="RDP 快速动作">
      <div>
        <span class="rdp-v3-eyebrow">快速动作</span>
        <strong>点击后立即创建 Run</strong>
        <p>如执行槽忙，新 Run 会显示队列位次和等待原因。</p>
      </div>
      <div class="rdp-v3-actions">
        ${visible.map((workflow, index) => renderAction({
          label: workflow.label,
          ui_action: workflow.action?.ui_action,
          value: workflow.workflow,
          enabled: workflow.manual_trigger_enabled,
          disabled_reason: workflow.disabled_reason,
        }, canAdmin, index === 1 ? "primary" : "secondary")).join("")}
      </div>
    </section>
  `;
}

export function renderRdpControlPanelV3({ workspace = {}, canAdmin = false } = {}) {
  const schemaVersion = String(workspace.schema_version || "");
  if (schemaVersion && schemaVersion !== "rdp.workspace.v3") {
    return `<div class="notice-card tone-danger">${escapeHtml(`RDP 快照版本不兼容：${schemaVersion}`)}</div>`;
  }
  return `
    <div class="rdp-v3-shell">
      ${renderHero(workspace, canAdmin)}
      ${renderLifecycle(workspace.lifecycle || {})}
      ${renderWorkflowActions(workspace.workflows || [], canAdmin)}
      <div class="rdp-v3-primary-grid">
        ${renderRuns(workspace.execution || {}, canAdmin)}
        ${renderResearch(workspace.research || {}, canAdmin)}
      </div>
      ${renderRelease(workspace.release || {}, workspace.tuning || {}, canAdmin)}
      <footer class="rdp-v3-footnote">
        ${summaryStrip([
          { label: "快照合同", value: workspace.schema_version || "待加载", tone: "neutral" },
          { label: "生成时间", value: workspace.generated_at || "待加载", tone: "neutral" },
          { label: "候选选择", value: selectionLabel(workspace.release?.selection_status), tone: workspace.release?.selection_status === "eligible_for_release_review" ? "warning" : "neutral" },
        ])}
      </footer>
    </div>
  `;
}

// 仓库外可能仍导入旧符号；保留别名，但业务合同已是 V3 workspace。
export const renderRdpControlPanelV2 = renderRdpControlPanelV3;
