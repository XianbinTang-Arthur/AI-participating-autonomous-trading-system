import { actionButton } from "./components.js";

export const KNOWN_DYNAMIC_CLIENT_ACTIONS = new Set([
  "apply-exit-execution-history-workspace",
  "collapse-ai-assessments",
  "collapse-ai-shadow-decisions",
  "collapse-ai-shadow-evaluations",
  "collapse-decisions",
  "collapse-fills",
  "collapse-orders",
  "collapse-replay-validations",
  "inspect-decision",
  "inspect-decision-history",
  "inspect-fill",
  "inspect-lifecycle-attribution",
  "inspect-order",
  "inspect-reconciliation",
  "inspect-shadow",
  "inspect-strategy-attribution",
  "inspect-trial-review-details",
  "load-more-ai-assessments",
  "load-more-ai-shadow-decisions",
  "load-more-ai-shadow-evaluations",
  "load-more-decisions",
  "load-more-fills",
  "load-more-orders",
  "load-more-replay-validations",
  "navigate-view",
  "paginate-exit-execution-history",
  "record-scaling-review",
  "record-trial-review",
  "record-trial-review-action",
  "refresh-dashboard",
  "reset-exit-execution-history-workspace",
  "resolve-stuck-order",
  "set-replay-parent-filter",
  "trigger-blocker-action",
  "trigger-exit-execution-refresh",
  "trigger-exit-execution-retry-limit-lookup",
  "trigger-exit-execution-safe-cancel",
  "trigger-halt",
  "trigger-rebaseline",
  "trigger-reconciliation-validate",
  "trigger-resume",
]);

export const KNOWN_RDP_UI_ACTIONS = new Set([
  "rdp-cancel-run",
  "rdp-open-run",
  "rdp-retry-run",
  "rdp-apply-only",
  "rdp-approve-and-apply",
  "rdp-approve-only",
  "rdp-approve-tuning-proposal",
  "rdp-create-release",
  "rdp-reject-recommendation",
  "rdp-reject-tuning-proposal",
  "rdp-rollback-parameters",
  "rdp-run-gate",
  "rdp-run-observation",
  "rdp-trigger-workflow",
]);

export function unsupportedClientActionTitle(action) {
  const normalized = normalizeActionName(action);
  return normalized
    ? `前端暂不支持此动作：${normalized}。请刷新页面或联系维护者。`
    : "后端未声明可执行动作，前端已阻止这次点击。";
}

export function isKnownDynamicClientAction(action) {
  const normalized = normalizeActionName(action);
  return Boolean(normalized && KNOWN_DYNAMIC_CLIENT_ACTIONS.has(normalized));
}

export function isKnownRdpUiAction(action) {
  const normalized = normalizeActionName(action);
  return Boolean(normalized && KNOWN_RDP_UI_ACTIONS.has(normalized));
}

export function dynamicClientActionButton(label, action, value = "", tone = "ghost", options = {}) {
  const normalized = normalizeActionName(action);
  const unsupported = !isKnownDynamicClientAction(normalized);
  return actionButton(
    label || "执行动作",
    unsupported ? "unsupported-client-action" : normalized,
    value || "",
    tone || "ghost",
    {
      ...options,
      disabled: Boolean(options.disabled || unsupported),
      title: unsupported ? unsupportedClientActionTitle(normalized) : options.title || "",
    },
  );
}

function normalizeActionName(action) {
  return String(action || "").trim();
}
