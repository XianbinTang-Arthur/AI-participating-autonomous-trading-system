import { primaryStatusPanel, responsiveTable, summaryStrip, surfaceCard } from "../components.js";
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

function governanceStatusLabel(value) {
  return value === "available" || value === "ready" ? "证据可用" : "状态未知";
}

function renderDataGovernance(dataGovernance = {}) {
  const coverage = dataGovernance.coverage || {};
  const imports = dataGovernance.historical_imports || {};
  const live = dataGovernance.live_collection || {};
  const archives = dataGovernance.archives || {};
  const eligibility = dataGovernance.eligibility || {};
  const rebuilds = dataGovernance.rebuilds || {};
  const monitoring = dataGovernance.monitoring || {};
  const summary = coverage.summary || {};
  const recovery = Array.isArray(coverage.recovery_matrix)
    ? coverage.recovery_matrix.slice(0, 8)
    : [];
  const cards = [
    {
      title: "数据覆盖",
      kicker: governanceStatusLabel(coverage.status),
      copy: coverage.next_action || "等待覆盖快照。",
      metrics: [
        { label: "表", value: String(coverage.table_count || 0), tone: "neutral" },
        { label: "已观测", value: String(summary.observed || 0), tone: "positive" },
        { label: "质量异常", value: String(summary.observed_with_quality_issues || 0), tone: summary.observed_with_quality_issues ? "warning" : "neutral" },
        { label: "缺失", value: String(summary.missing || 0), tone: summary.missing ? "danger" : "neutral" },
      ],
    },
    {
      title: "历史导入",
      kicker: governanceStatusLabel(imports.status),
      copy: imports.next_action || "等待导入证据。",
      metrics: [
        { label: "最近运行", value: String(imports.total_recent || 0), tone: "info" },
      ],
    },
    {
      title: "实时采集",
      kicker: governanceStatusLabel(live.status),
      copy: live.next_action || "等待连续性证据。",
      metrics: [
        { label: "频道", value: String(live.channel_count || 0), tone: "info" },
        { label: "丢弃", value: String(live.drop_count || 0), tone: live.drop_count ? "danger" : "neutral" },
      ],
    },
    {
      title: "不可变归档",
      kicker: governanceStatusLabel(archives.status),
      copy: archives.next_action || "等待归档证据。",
      metrics: [
        { label: "分区", value: String(archives.partition_count || 0), tone: "info" },
        { label: "阻断", value: String(archives.blocked_count || 0), tone: archives.blocked_count ? "danger" : "neutral" },
      ],
    },
    {
      title: "质量资格",
      kicker: governanceStatusLabel(eligibility.status),
      copy: eligibility.next_action || "等待 bundle 资格证据。",
      metrics: [
        { label: "Bundle", value: String(eligibility.bundle_count || 0), tone: "info" },
      ],
    },
    {
      title: "确定性重建",
      kicker: governanceStatusLabel(rebuilds.status),
      copy: rebuilds.next_action || "等待重建证据。",
      metrics: [
        { label: "最近运行", value: String(rebuilds.total_recent || 0), tone: "info" },
      ],
    },
    {
      title: "监控告警",
      kicker: monitoring.status === "healthy" ? "当前无告警" : (monitoring.status === "critical" ? "需要立即处理" : "需要关注"),
      copy: monitoring.next_action || "等待监控证据。",
      metrics: [
        { label: "告警", value: String(monitoring.alert_count || 0), tone: monitoring.alert_count ? "warning" : "positive" },
        { label: "严重", value: String(monitoring.critical_count || 0), tone: monitoring.critical_count ? "danger" : "neutral" },
      ],
    },
  ];
  return `
    <section class="rdp-v3-data-governance" data-panel-key="rdpWorkspace" aria-label="RDP 数据治理">
      <div class="rdp-v3-data-governance__head">
        <div>
          <span class="rdp-v3-eyebrow">数据治理</span>
          <h3>来源、覆盖、连续性与重建</h3>
          <p>页面只读取预聚合快照；未知、缺失、有效零和失败保持不同状态。</p>
        </div>
        ${statusPill(
          dataGovernance.status === "ready" ? "治理快照可用" : "数据证据不完整",
          dataGovernance.status === "ready" ? "positive" : "warning",
        )}
      </div>
      <div class="rdp-v3-data-governance__grid">
        ${cards.map((card) => surfaceCard({
          title: card.title,
          kicker: card.kicker,
          copy: card.copy,
          classes: "rdp-v3-card rdp-v3-data-card",
          panelKey: "rdpWorkspace",
          content: summaryStrip(card.metrics),
        })).join("")}
      </div>
      <div class="rdp-v3-data-governance__detail">
        <div class="rdp-v3-data-governance__detail-head">
          <div>
            <span class="rdp-v3-eyebrow">恢复矩阵</span>
            <h4>真实缺口与下一步</h4>
          </div>
          <span class="table-meta">最多展示 8 项；完整证据保存在不可变覆盖快照</span>
        </div>
        ${responsiveTable(
          ["数据集", "状态", "恢复分类", "优先级", "下一步"],
          recovery.map((item) => [
            `<strong>${escapeHtml(item.dataset || "未知数据集")}</strong>`,
            statusPill(item.observed_status || "unknown", statusTone(item.observed_status)),
            escapeHtml(item.classification || "尚未分类"),
            escapeHtml(item.priority || "—"),
            `<span class="meta-copy">${escapeHtml(item.next_action || "等待审计")}</span>`,
          ]),
          "当前快照没有待恢复的数据项。",
        )}
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
      ${renderDataGovernance(workspace.data_governance || {})}
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
