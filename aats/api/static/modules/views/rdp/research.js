import { surfaceCard } from "../../components.js";
import { escapeHtml } from "../../formatters.js";
import { readableState } from "../../terms.js";
import {
  renderAction,
  shortId,
  statusPill,
  statusTone,
} from "./formatters.js";

const METRIC_LABELS = {
  experiments_with_openings: "有开仓实验",
  max_opening_count: "最大开仓数",
  mean_positive_edge_ratio: "平均正边际占比",
  status: "归因状态",
  failure_ratio: "失败占比",
  failure_count: "失败样本",
  total_count: "总样本",
  full_fill_ratio: "完全成交占比",
  cost_adjusted_edge_proxy_bps: "成本后边际",
  mean_cost_bps: "平均执行成本",
  decision_status: "治理决策",
  runtime_source: "运行参数来源",
};

const RATIO_METRICS = new Set([
  "mean_positive_edge_ratio",
  "failure_ratio",
  "full_fill_ratio",
]);

const BPS_METRICS = new Set(["cost_adjusted_edge_proxy_bps", "mean_cost_bps"]);

function formatMetricValue(key, value) {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  if (RATIO_METRICS.has(key) && Number.isFinite(numeric)) return `${(numeric * 100).toFixed(1)}%`;
  if (BPS_METRICS.has(key) && Number.isFinite(numeric)) return `${numeric.toFixed(2)} bps`;
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(3);
  return readableState(value, String(value));
}

function evidenceMetrics(entry = {}) {
  const metrics = entry.metrics && typeof entry.metrics === "object" ? entry.metrics : {};
  const rows = Object.entries(metrics).filter(([key]) => METRIC_LABELS[key]);
  if (!rows.length) return "";
  return `
    <dl class="rdp-v3-evidence-metrics">
      ${rows.map(([key, value]) => `
        <div><dt>${escapeHtml(METRIC_LABELS[key])}</dt><dd>${escapeHtml(formatMetricValue(key, value))}</dd></div>
      `).join("")}
    </dl>
  `;
}

function evidenceRows(item = {}) {
  const entries = Array.isArray(item.evidence_digest) ? item.evidence_digest : [];
  if (!entries.length) return '<p class="rdp-v3-muted">尚无可展示的证据摘要。</p>';
  return `
    <div class="rdp-v3-evidence-grid">
      ${entries.map((entry) => `
        <article>
          <strong>${escapeHtml(entry.label || entry.headline || entry.phase || "证据")}</strong>
          ${statusPill(entry.status_label || entry.status || "未知", statusTone(entry.status))}
          <p>${escapeHtml(entry.summary || entry.incomplete_reason || entry.headline || "已记录该阶段证据。")}</p>
          ${evidenceMetrics(entry)}
          <span class="rdp-v3-evidence-source">${escapeHtml(entry.round_id ? `来源轮次 ${shortId(entry.round_id, 24)}` : "当前状态快照")}</span>
        </article>
      `).join("")}
    </div>
  `;
}

function renderResearchItem(item = {}, canAdmin = false) {
  const integrityBlocked = item.integrity_status === "blocked";
  const reasons = Array.isArray(item.reason_summary) ? item.reason_summary : [];
  const risks = Array.isArray(item.detail_summary?.risk_summary)
    ? item.detail_summary.risk_summary
    : [];
  const actions = Array.isArray(item.actions) ? item.actions : [];
  return `
    <article class="rdp-v3-research-item tone-${integrityBlocked ? "danger" : "warning"}">
      <div class="rdp-v3-research-item__head">
        <div>
          <span class="rdp-v3-eyebrow">${escapeHtml(`${item.family || "未知策略"} · ${item.timeframe || "未知周期"}`)}</span>
          <strong>${escapeHtml(item.headline || "研究结论待处理")}</strong>
        </div>
        ${statusPill(integrityBlocked ? "证据阻断" : `置信度 ${item.confidence || "未知"}`, integrityBlocked ? "danger" : "warning")}
      </div>
      <p>${escapeHtml(item.decision_summary || "请查看证据后决定。")}</p>
      ${reasons.length ? `<ul>${reasons.slice(0, 3).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>` : ""}
      <details class="rdp-v3-details">
        <summary>查看证据链与审批影响</summary>
        ${evidenceRows(item)}
        ${risks.length ? `<p class="rdp-v3-muted">主要风险：${risks.slice(0, 4).map((risk) => escapeHtml(risk)).join("；")}</p>` : ""}
        <p class="rdp-v3-muted">${escapeHtml(item.approval_effect_summary || "批准后仍需要后续 Gate 和发布门禁。")}</p>
      </details>
      <div class="rdp-v3-actions">
        ${actions.map((action, index) => renderAction(action, canAdmin, index === 0 ? "primary" : "ghost")).join("")}
      </div>
    </article>
  `;
}

function renderAlerts(alerts = {}) {
  const integrity = Array.isArray(alerts.integrity_alerts) ? alerts.integrity_alerts : [];
  const operational = Array.isArray(alerts.operational_alerts) ? alerts.operational_alerts : [];
  const items = [...integrity, ...operational].slice(0, 4);
  if (!items.length) return "";
  return `
    <div class="rdp-v3-alert-strip">
      ${items.map((alert) => `
        <article class="tone-${escapeHtml(statusTone(alert.severity || (alert.blocks_approval ? "blocked" : "warning")))}">
          <strong>${escapeHtml(alert.title || "RDP 提醒")}</strong>
          <p>${escapeHtml(alert.message || alert.description || "请在继续前复核。")}</p>
        </article>
      `).join("")}
    </div>
  `;
}

export function renderResearch(research = {}, canAdmin = false) {
  const overview = research.overview || {};
  const items = Array.isArray(research.items) ? research.items : [];
  return surfaceCard({
    title: "研究证据与治理审阅",
    kicker: "证据先于动作",
    copy: items.length
      ? `当前有 ${items.length} 个组合需要审阅；证据不完整时批准会失败关闭。`
      : (overview.subheadline || "当前没有新的待审阅结论。"),
    classes: "rdp-v3-card rdp-v3-research-card",
    panelKey: "rdpWorkspace",
    content: `
      ${renderAlerts(research.alerts || {})}
      ${items.length
        ? `<div class="rdp-v3-research-list">${items.map((item) => renderResearchItem(item, canAdmin)).join("")}</div>`
        : '<div class="rdp-v3-empty rdp-v3-empty--compact"><strong>无待审阅结论</strong><p>可以先刷新数据或运行完整 RDP；这不代表已有可盈利候选。</p></div>'}
    `,
  });
}
