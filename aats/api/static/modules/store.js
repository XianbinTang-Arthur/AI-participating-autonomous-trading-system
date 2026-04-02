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

export const DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS = Object.freeze({
  action: "all",
  parent: "",
  actor: "",
  windowHours: "all",
});

export const DEFAULT_EXIT_EXECUTION_HISTORY_PAGING = Object.freeze({
  risk: {
    offset: 0,
    limit: 20,
  },
  exitExecution: {
    offset: 0,
    limit: 50,
  },
});

export function createState() {
  return {
    activeView: "home",
    actionInFlight: false,
    refreshing: false,
    pendingRefresh: false,
    loadingView: null,
    readyViews: {},
    refreshGeneration: 0,
    refreshTimer: null,
    lastRefreshAt: null,
    flash: null,
    data: {},
    errors: {},
    pendingPanels: {},
    pageLimits: { ...DEFAULT_PAGE_LIMITS },
    ui: {
      aiConfig: {
        modeManualEditing: false,
        profileManualEditing: false,
      },
      risk: {
        exitExecutionHistory: {
          ...DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
          ...DEFAULT_EXIT_EXECUTION_HISTORY_PAGING.risk,
        },
      },
      exitExecution: {
        exitExecutionHistory: {
          ...DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
          ...DEFAULT_EXIT_EXECUTION_HISTORY_PAGING.exitExecution,
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

const DEFERRED_VIEW_PANELS = {
  risk: new Set(["replayStatus", "exitExecutionActionHistoryPage"]),
};

export function viewSpecs(view, state = null) {
  const limits = { ...DEFAULT_PAGE_LIMITS, ...(state?.pageLimits || {}) };
  const riskExitExecutionHistory = state?.ui?.risk?.exitExecutionHistory || {};
  const exitExecutionWorkspaceHistory = state?.ui?.exitExecution?.exitExecutionHistory || {};
  const riskExitExecutionHistoryPath = buildExitExecutionActionHistoryPath(riskExitExecutionHistory);
  const exitExecutionWorkspaceHistoryPath = buildExitExecutionActionHistoryPath(exitExecutionWorkspaceHistory);
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
      ["portfolio", "/portfolio/latest"],
      ["positions", "/positions"],
      ["accountState", "/account/state"],
      ["reconciliationLatest", "/reconciliation/latest"],
      ["replayStatus", "/replay/status"],
      ["exitExecutionActionHistoryPage", riskExitExecutionHistoryPath],
    ],
    exitExecution: [
      ["exitExecutionActionHistoryPage", exitExecutionWorkspaceHistoryPath],
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

function deferredPanelSetForView(view) {
  return DEFERRED_VIEW_PANELS[view] || null;
}

export function buildExitExecutionActionHistoryPath(state = {}) {
  const params = new URLSearchParams({
    limit: String(Math.max(Number(state.limit) || 20, 1)),
    offset: String(Math.max(Number(state.offset) || 0, 0)),
  });
  const action = String(state.action || "").trim();
  const parent = String(state.parent || "").trim();
  const actor = String(state.actor || "").trim();
  const windowHours = String(state.windowHours || "").trim();
  if (action && action !== "all") {
    params.set("action", action);
  }
  if (parent) {
    params.set("parent_intent_id", parent);
  }
  if (actor) {
    params.set("actor", actor);
  }
  if (windowHours && windowHours !== "all") {
    params.set("window_hours", windowHours);
  }
  return `/system/exit-execution/action-history?${params.toString()}`;
}

export function dashboardBundlePanelKeys(view, state = null, options = {}) {
  const includeDeferred = options.includeDeferred !== false;
  const deferredOnly = options.deferredOnly === true;
  const deferredPanels = deferredPanelSetForView(view);
  const seen = new Set();
  const specs = [...CORE_SPECS, ...viewSpecs(view, state)];
  return specs
    .filter(([key]) => {
      if (seen.has(key)) return false;
      seen.add(key);
      const isDeferred = Boolean(deferredPanels?.has(key));
      if (deferredOnly) return isDeferred;
      if (!includeDeferred && isDeferred) return false;
      return true;
    })
    .map(([key]) => key);
}

export function buildDashboardBundlePath(view, state = null, options = {}) {
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
  dashboardBundlePanelKeys(view, state, options).forEach((key) => {
    params.append("panel", key);
  });
  return `/dashboard/bundle?${params.toString()}`;
}

export function buildDashboardBundleRequestPlan(view, state = null) {
  const deferredPanels = dashboardBundlePanelKeys(view, state, { deferredOnly: true });
  return {
    primaryPath: buildDashboardBundlePath(view, state, { includeDeferred: false }),
    deferredPath: deferredPanels.length ? buildDashboardBundlePath(view, state, { deferredOnly: true }) : null,
    deferredPanels,
  };
}
