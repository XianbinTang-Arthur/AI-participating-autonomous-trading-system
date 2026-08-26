import { actionButton } from "../../components.js";
import { isKnownRdpUiAction, unsupportedClientActionTitle } from "../../action-contract.js";
import { escapeHtml } from "../../formatters.js";

const STATUS_GLYPHS = {
  danger: "✕",
  warning: "⚠",
  positive: "✓",
  info: "ℹ",
  neutral: "·",
};

const WORKFLOW_LABELS = {
  candles_rolling_15m: "15 分钟 K 线采集",
  data_maintenance: "数据维护",
  decision_cycle: "决策周期",
  governance_cycle: "治理周期",
  microstructure_silver_15m: "微观结构数据加工",
  observation_cycle: "发布观察",
  okx_rest_history_rolling_1h: "OKX 历史数据补充",
  release_cycle: "自动发布",
  reliability_cycle: "可靠性检查",
  research_cycle: "完整 RDP 研究",
};

const STEP_LABELS = {
  daily_ingest: "每日数据采集",
  refresh_recent_data: "刷新近期数据",
  quality_monitor: "数据质量检查",
  artifact_index_rebuild: "重建研究产物索引",
  artifact_validation: "校验研究产物",
  full_pipeline: "完整研究流水线",
  phase2: "阶段 2：数据与研究准备",
  step3: "阶段 3：策略研究",
  import_candidates: "导入候选参数",
  phase3: "阶段 3：候选验证",
  phase4: "阶段 4：组合验证",
  phase5: "阶段 5：执行真实性评估",
  decision: "阶段 6：闭环决策",
};

const GATE_LABELS = {
  pass: "通过",
  warn: "有警告",
  block: "已阻断",
  failed: "失败",
  not_run: "未运行",
};

const OBSERVATION_LABELS = {
  observing: "观察中",
  completed: "观察完成",
  rollback_recommended: "建议回滚",
  rolled_back: "已回滚",
  pending: "待观察",
};

const SELECTION_LABELS = {
  eligible_for_release_review: "存在合格候选",
  no_eligible_candidate: "无合格候选",
};

export function statusTone(status) {
  const value = String(status || "").toLowerCase();
  if (["blocked", "failed", "error", "audit_failed", "missing", "known_gap", "rollback_required", "rollback_recommended"].includes(value)) return "danger";
  if (["degraded", "warn", "warning", "collector_unknown", "observed_with_quality_issues", "queued", "action_required", "partially_succeeded", "succeeded_with_warnings"].includes(value)) return "warning";
  if (["healthy", "complete", "completed", "pass", "succeeded", "success", "done"].includes(value)) return "positive";
  if (["running", "observing", "busy", "starting", "cancellation_requested"].includes(value)) return "info";
  return "neutral";
}

export function statusPill(label, tone = "neutral") {
  const normalized = Object.prototype.hasOwnProperty.call(STATUS_GLYPHS, tone) ? tone : "neutral";
  return `<span class="signal-pill tone-${escapeHtml(normalized)}"><span aria-hidden="true">${STATUS_GLYPHS[normalized]}</span> ${escapeHtml(label || "状态未知")}</span>`;
}

export function workflowLabel(value) {
  const key = String(value || "");
  return WORKFLOW_LABELS[key] || (key ? `流程 ${key}` : "未知流程");
}

export function stepLabel(value) {
  const key = String(value || "");
  return STEP_LABELS[key] || (key ? `步骤 ${key}` : "等待步骤上报");
}

export function gateLabel(value) {
  const key = String(value || "not_run").toLowerCase();
  return GATE_LABELS[key] || "状态未知";
}

export function observationLabel(value) {
  const key = String(value || "pending").toLowerCase();
  return OBSERVATION_LABELS[key] || "状态未知";
}

export function selectionLabel(value) {
  const key = String(value || "");
  return SELECTION_LABELS[key] || "待加载";
}

export function shortId(value, length = 18) {
  const text = String(value || "");
  if (text.length <= length) return text || "—";
  const side = Math.max(5, Math.floor((length - 1) / 2));
  return `${text.slice(0, side)}…${text.slice(-side)}`;
}

export function formatTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return String(value);
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

export function relativeTime(value) {
  if (!value) return "时间未知";
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return String(value);
  const seconds = Math.max(0, Math.floor((Date.now() - parsed.getTime()) / 1000));
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

export function renderAction(action = {}, canAdmin = false, tone = "secondary") {
  if (!action || !action.ui_action) return "";
  const known = isKnownRdpUiAction(action.ui_action);
  const enabled = Boolean(action.enabled) && canAdmin && known;
  const reason = !canAdmin
    ? "当前会话没有 RDP 写权限。"
    : !known
      ? unsupportedClientActionTitle(action.ui_action)
      : action.disabled_reason;
  return actionButton(
    action.label || "执行",
    action.ui_action,
    action.value || "",
    tone,
    { disabled: !enabled, title: reason || "" },
  );
}

export function renderCount(value) {
  const numeric = Number(value || 0);
  return Number.isFinite(numeric) ? String(numeric) : "0";
}
