export const AUTO_REFRESH_MS = 10000;

export const DEFAULT_PAGE_LIMITS = {
  recentDecisions: 8,
  recentOrders: 8,
  recentFills: 8,
  recentReplayValidations: 8,
  recentAIAssessments: 8,
  recentAIShadowDecisions: 8,
  recentAIShadowEvaluations: 8,
};

export const PAGE_LOAD_STEP = 12;

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
    ui: {
      aiConfig: {
        modeManualEditing: false,
        profileManualEditing: false,
      },
      risk: {
        exitExecutionHistory: {
          action: "all",
          parent: "",
          actor: "",
        },
      },
      replay: {
        parentFilter: "all",
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
  const limits = { ...DEFAULT_PAGE_LIMITS, ...(state?.pageLimits || {}) };
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
      ["positions", "/positions"],
      ["latestDecision", "/decision/latest"],
      ["executionLatest", "/execution/latest"],
      ["reconciliationLatest", "/reconciliation/latest"],
      ["accountState", "/account/state"],
    ],
      strategy: [
        ["strategyRuntime", "/strategy/runtime"],
        ["strategyAttribution", "/reports/strategy-attribution?limit=200"],
        ["latestDecision", "/decision/latest"],
        ["recentDecisions", `/decision/recent?limit=${limits.recentDecisions}&offset=0`],
        ["executionLatest", "/execution/latest"],
        ["trialReviewSummary", "/reports/trial-review-summary?segment_limit=100&window_days=7&period_count=4"],
        ["trialReviewHistory", "/reports/trial-review-history?limit=5&offset=0"],
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
      ["positions", "/positions"],
      ["accountState", "/account/state"],
      ["reconciliationLatest", "/reconciliation/latest"],
      ["replayStatus", "/replay/status"],
    ],
    replay: [
      ["replayStatus", "/replay/status"],
      ["replayRecentValidations", `/replay/recent-validations?limit=${limits.recentReplayValidations}&offset=0`],
      ["reconciliationLatest", "/reconciliation/latest"],
    ],
    aiAnalysis: [
      ["aiOverview", "/ai/overview"],
      ["aiRuntime", "/ai/runtime"],
      ["aiLatest", "/ai/latest"],
      ["aiShadowLatest", "/ai/shadow/latest"],
      ["profileControlSummary", "/reports/profile-control-summary"],
      ["aiRecent", `/ai/recent?limit=${limits.recentAIAssessments}&offset=0`],
      ["aiShadowRecent", `/ai/shadow/recent?limit=${limits.recentAIShadowDecisions}&offset=0`],
      ["aiShadowEvaluations", `/ai/shadow/evaluations?limit=${limits.recentAIShadowEvaluations}&offset=0`],
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

export function dashboardBundlePanelKeys(view, state = null) {
  const seen = new Set();
  const specs = [...CORE_SPECS, ...viewSpecs(view, state)];
  return specs
    .filter(([key]) => {
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .map(([key]) => key);
}

export function buildDashboardBundlePath(view, state = null) {
  const limits = { ...DEFAULT_PAGE_LIMITS, ...(state?.pageLimits || {}) };
  const params = new URLSearchParams({
    view: String(view || "home"),
    recentDecisions: String(limits.recentDecisions),
    recentOrders: String(limits.recentOrders),
    recentFills: String(limits.recentFills),
    recentReplayValidations: String(limits.recentReplayValidations),
    recentAIAssessments: String(limits.recentAIAssessments),
    recentAIShadowDecisions: String(limits.recentAIShadowDecisions),
    recentAIShadowEvaluations: String(limits.recentAIShadowEvaluations),
  });
  dashboardBundlePanelKeys(view, state).forEach((key) => {
    params.append("panel", key);
  });
  return `/dashboard/bundle?${params.toString()}`;
}
