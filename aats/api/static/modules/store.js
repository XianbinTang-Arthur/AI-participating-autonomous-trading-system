export const AUTO_REFRESH_MS = 15000;

export const CORE_SPECS = [
  ["session", "/auth/session"],
  ["authProviders", "/auth/providers"],
  ["health", "/system/health"],
  ["mode", "/system/mode"],
  ["runtime", "/system/runtime"],
  ["systemRecovery", "/system/recovery"],
];

export const VIEW_SPECS = {
  overview: [
    ["blockers", "/system/blockers"],
    ["metrics", "/system/metrics"],
    ["portfolio", "/portfolio/latest"],
    ["latestDecision", "/decision/latest"],
    ["executionLatest", "/execution/latest"],
    ["reconciliationLatest", "/reconciliation/latest"],
    ["accountState", "/account/state"],
  ],
  strategy: [
    ["latestDecision", "/decision/latest"],
    ["recentDecisions", "/decision/recent?limit=8"],
    ["executionLatest", "/execution/latest"],
  ],
  execution: [
    ["latestDecision", "/decision/latest"],
    ["metrics", "/system/metrics"],
    ["executionLatest", "/execution/latest"],
    ["recentOrders", "/orders/recent?limit=8"],
    ["recentFills", "/fills/recent?limit=8"],
    ["executionErrors", "/execution/errors"],
  ],
  risk: [
    ["blockers", "/system/blockers"],
    ["metrics", "/system/metrics"],
    ["portfolio", "/portfolio/latest"],
    ["accountState", "/account/state"],
    ["reconciliationLatest", "/reconciliation/latest"],
    ["replayStatus", "/replay/status"],
  ],
  admin: [
    ["operatorUsers", "/auth/users"],
    ["runtimeProfiles", "/runtime-profiles"],
  ],
};

export function createState() {
  return {
    activeView: "overview",
    refreshing: false,
    refreshTimer: null,
    lastRefreshAt: null,
    flash: null,
    data: {},
    errors: {},
  };
}

export function viewSpecs(view) {
  return VIEW_SPECS[view] || [];
}
