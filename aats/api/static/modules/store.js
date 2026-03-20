export const AUTO_REFRESH_MS = 10000;
export const DEFAULT_PAGE_LIMITS = {
  recentDecisions: 8,
  recentOrders: 8,
  recentFills: 8,
  recentReconciliations: 8,
  blockerHistory: 8,
  replayValidations: 8,
  recentAIAssessments: 8,
  recentAIShadowDecisions: 8,
  recentAIShadowEvaluations: 8,
};
export const PAGE_LOAD_STEP = 12;

export const CORE_SPECS = [
  ["session", "/auth/session"],
  ["authProviders", "/auth/providers"],
  ["health", "/system/health"],
  ["mode", "/system/mode"],
  ["runtime", "/system/runtime"],
  ["systemRecovery", "/system/recovery"],
  ["blockerControl", "/system/blocker-control"],
];

export function createState() {
  return {
    activeView: "home",
    actionInFlight: false,
    refreshing: false,
    pendingRefresh: false,
    loadingView: null,
    readyViews: {},
    refreshTimer: null,
    lastRefreshAt: null,
    flash: null,
    data: {},
    errors: {},
    pageLimits: { ...DEFAULT_PAGE_LIMITS },
  };
}

export function viewSpecs(view, state = null) {
  const limits = state?.pageLimits || DEFAULT_PAGE_LIMITS;
  const specs = {
    home: [
      ["blockers", "/system/blockers"],
      ["metrics", "/system/metrics"],
      ["portfolio", "/portfolio/latest"],
      ["latestDecision", "/decision/latest"],
      ["executionLatest", "/execution/latest"],
      ["reconciliationLatest", "/reconciliation/latest"],
      ["accountState", "/account/state"],
    ],
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
      ["recentDecisions", `/decision/recent?limit=${limits.recentDecisions}&offset=0`],
      ["executionLatest", "/execution/latest"],
    ],
    execution: [
      ["latestDecision", "/decision/latest"],
      ["metrics", "/system/metrics"],
      ["executionLatest", "/execution/latest"],
      ["recentOrders", `/orders/recent?limit=${limits.recentOrders}&offset=0`],
      ["recentFills", `/fills/recent?limit=${limits.recentFills}&offset=0`],
      ["executionErrors", "/execution/errors"],
    ],
    risk: [
      ["blockers", "/system/blockers"],
      ["metrics", "/system/metrics"],
      ["portfolio", "/portfolio/latest"],
      ["accountState", "/account/state"],
      ["reconciliationLatest", "/reconciliation/latest"],
      ["replayStatus", "/replay/status"],
      ["reconciliationRecent", `/reconciliation/recent?limit=${limits.recentReconciliations}&offset=0`],
      ["blockerHistory", `/system/blocker-history?limit=${limits.blockerHistory}&offset=0`],
      ["replayRecentValidations", `/replay/recent-validations?limit=${limits.replayValidations}&offset=0`],
    ],
    ai: [
      ["aiOverview", "/ai/overview"],
      ["aiRuntime", "/ai/runtime"],
      ["aiLatest", "/ai/latest"],
      ["aiRecent", `/ai/recent?limit=${limits.recentAIAssessments}&offset=0`],
      ["aiShadowLatest", "/ai/shadow/latest"],
      ["aiShadowRecent", `/ai/shadow/recent?limit=${limits.recentAIShadowDecisions}&offset=0`],
      ["aiShadowEvaluations", `/ai/shadow/evaluations?limit=${limits.recentAIShadowEvaluations}&offset=0`],
    ],
    aiConfig: [
      ["aiConfigModel", "/ai-config/summary"],
    ],
    admin: [
      ["operatorUsers", "/auth/users"],
    ],
  };
  return specs[view] || [];
}
