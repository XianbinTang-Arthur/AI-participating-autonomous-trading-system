export const AUTO_REFRESH_MS = 10000;

export const DEFAULT_PAGE_LIMITS = {
  recentDecisions: 8,
  recentOrders: 8,
  recentFills: 8,
  recentAIAssessments: 8,
  recentAIShadowDecisions: 8,
  recentAIShadowEvaluations: 8,
};

export const PAGE_LOAD_STEP = 12;

export const CORE_BLOCKING_SPECS = [
  ["session", "/auth/session"],
  ["health", "/system/health"],
  ["mode", "/system/mode"],
  ["runtime", "/system/runtime"],
];

export const CORE_BACKGROUND_SPECS = [
  ["authProviders", "/auth/providers"],
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
    backgroundGenerations: {},
    refreshTimer: null,
    lastRefreshAt: null,
    flash: null,
    data: {},
    errors: {},
    pageLimits: { ...DEFAULT_PAGE_LIMITS },
    ui: {
      aiConfig: {
        modeManualEditing: false,
        profileManualEditing: false,
      },
    },
  };
}

export const CORE_SPECS = [
  ["session", "/auth/session"],
  ["authProviders", "/auth/providers"],
  ["health", "/system/health"],
  ["mode", "/system/mode"],
  ["runtime", "/system/runtime"],
  ["systemRecovery", "/system/recovery"],
  ["blockerControl", "/system/blocker-control"],
];

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
      ["strategyRuntime", "/strategy/runtime"],
      ["latestDecision", "/decision/latest"],
      ["recentDecisions", `/decision/recent?limit=${limits.recentDecisions}&offset=0`],
      ["executionLatest", "/execution/latest"],
      ["trialReviewSummary", "/reports/trial-review-summary?segment_limit=100&window_days=7&period_count=4"],
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
      ["metrics", "/system/metrics"],
      ["phase1Shadow", "/system/shadow"],
      ["trialGuard", "/system/trial-guard"],
      ["guardedLivePreflight", "/system/guarded-live-preflight"],
      ["guardedLiveRunPacket", "/reports/guarded-live-run-packet"],
      ["portfolio", "/portfolio/latest"],
      ["accountState", "/account/state"],
      ["reconciliationLatest", "/reconciliation/latest"],
      ["replayStatus", "/replay/status"],
    ],
    aiAnalysis: [
      ["aiOverview", "/ai/overview"],
      ["aiRuntime", "/ai/runtime"],
      ["aiLatest", "/ai/latest"],
      ["aiShadowLatest", "/ai/shadow/latest"],
      ["profileControlSummary", "/reports/profile-control-summary"],
    ],
    aiConfig: [
      ["aiConfigModel", "/ai-config/summary"],
      ["aiRuntime", "/ai/runtime"],
    ],
    admin: [
      ["operatorUsers", "/auth/users"],
    ],
  };
  return specs[view] || [];
}

export function viewBackgroundSpecs(view, state = null) {
  const limits = state?.pageLimits || DEFAULT_PAGE_LIMITS;
  const specs = {
    aiAnalysis: [
      ["aiRecent", `/ai/recent?limit=${limits.recentAIAssessments}&offset=0`],
      ["aiShadowRecent", `/ai/shadow/recent?limit=${limits.recentAIShadowDecisions}&offset=0`],
      ["aiShadowEvaluations", `/ai/shadow/evaluations?limit=${limits.recentAIShadowEvaluations}&offset=0`],
    ],
  };
  return specs[view] || [];
}
