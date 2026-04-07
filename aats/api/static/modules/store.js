export const AUTO_REFRESH_MS = 30000;
// View freshness is intentionally tied to AUTO_REFRESH_MS: if the auto-refresh
// cadence changes, the stale-while-revalidate window should move with it,
// otherwise switching views can hit a "fresh" cache entry that is actually
// staler than what an auto-refresh would have already rewritten.
export const VIEW_FRESHNESS_MS = AUTO_REFRESH_MS;

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

// refreshPhase state machine:
//   "idle"     — no fetch in flight
//   "primary"  — the primary bundle fetch is in flight; data on screen may be stale
//   "deferred" — primary bundle has already landed, an optional deferred bundle
//                (slow or secondary panels) is still fetching in the background
// Only "primary" should gate bootstrap skeletons and the global refresh button.
// "deferred" is a background fill-in and must not lock any UI.
export const REFRESH_PHASE_IDLE = "idle";
export const REFRESH_PHASE_PRIMARY = "primary";
export const REFRESH_PHASE_DEFERRED = "deferred";

export function createState() {
  return {
    activeView: "home",
    actionInFlight: false,
    refreshPhase: REFRESH_PHASE_IDLE,
    // pendingRefresh: null | { manual: boolean }
    // When set, the currently running refresh should drain this request after
    // it finishes. Carrying the manual flag ensures a manual refresh that got
    // queued still shows the "已刷新" flash when it finally runs.
    pendingRefresh: null,
    loadingView: null,
    readyViews: {},
    viewRefreshedAt: {},
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

const EXCLUDED_CORE_PANELS = {
  risk: new Set(["mode", "runtime"]),
};

const DEFERRED_VIEW_PANELS = {
  home: new Set(["latestDecision", "executionLatest", "reconciliationLatest"]),
  overview: new Set(["latestDecision", "executionLatest", "reconciliationLatest"]),
  risk: new Set([
    "replayStatus",
    "exitExecutionActionHistoryPage",
    "trialGuard",
    "guardedLivePreflight",
    "guardedLiveRunPacket",
  ]),
  strategy: new Set(["trialReviewSummary", "strategyAttribution"]),
  aiAnalysis: new Set(["aiShadowEvaluations", "profileControlSummary"]),
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
      ["trialGuard", "/system/trial-guard"],
      ["guardedLivePreflight", "/system/guarded-live/preflight"],
      ["guardedLiveRunPacket", "/system/guarded-live/run-packet"],
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

// #27 修复：原本 home-view.js 在自己内部又写了一遍 ["latestDecision",
// "executionLatest", "reconciliationLatest"] 来判断"延迟 bundle 是不是还
// 在加载"，这串 key 同时存在于：
//   1. store.js::DEFERRED_VIEW_PANELS.home（决定 deferred bundle 拉哪些 panel）
//   2. dashboard-refresh.js → buildDashboardBundleRequestPlan → 间接读 1
//   3. home-view.js::deferredLoading 的 ad-hoc 判断
// 三处一旦漂移，加载占位文字就和真正延迟拉的 panel 对不上。这里把第 3 处
// 的判断收成一个 helper：调用方只传 view 名和 pendingPanels，本模块内部
// 直接读 DEFERRED_VIEW_PANELS 的真实集合。
export function hasAnyDeferredPanelPending(view, pendingPanels = {}) {
  const set = deferredPanelSetForView(view);
  if (!set || !pendingPanels) return false;
  for (const key of set) {
    if (pendingPanels[key]) return true;
  }
  return false;
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
  const excludedCorePanels = EXCLUDED_CORE_PANELS[view] || null;
  const seen = new Set();
  const specs = [
    ...CORE_SPECS.filter(([key]) => !excludedCorePanels?.has(key)),
    ...viewSpecs(view, state),
  ];
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
  const primaryPanels = dashboardBundlePanelKeys(view, state, { includeDeferred: false });
  const deferredPanels = dashboardBundlePanelKeys(view, state, { deferredOnly: true });
  return {
    primaryPath: buildDashboardBundlePath(view, state, { includeDeferred: false }),
    deferredPath: deferredPanels.length ? buildDashboardBundlePath(view, state, { deferredOnly: true }) : null,
    primaryPanels,
    deferredPanels,
  };
}

// Scope maps: which views participate in each URL-affecting piece of state.
// Used by callers of invalidateCachedViews to invalidate ONLY the views whose
// bundle URL actually changes, rather than blanket-invalidating every view.
// Keep these in sync with the bundle request plan in viewSpecs().
export const PAGE_LIMIT_AFFECTED_VIEWS = Object.freeze({
  recentDecisions: Object.freeze(["strategy"]),
  recentOrders: Object.freeze(["execution"]),
  recentFills: Object.freeze(["execution"]),
  recentReplayValidations: Object.freeze(["replay"]),
  recentAIAssessments: Object.freeze(["aiAnalysis"]),
  recentAIShadowDecisions: Object.freeze(["aiAnalysis"]),
  recentAIShadowEvaluations: Object.freeze(["aiAnalysis"]),
});

// The exit-execution action-history filters are embedded in the bundle URL
// for both the risk view and the dedicated exitExecution workspace, which
// share the same backing panel key.
export const EXIT_EXECUTION_FILTER_AFFECTED_VIEWS = Object.freeze(["risk", "exitExecution"]);

// Drop cached "ready" markers for every view except `exceptView`. Call this
// whenever a piece of state that participates in the bundle URL (pageLimits,
// exit-execution-history filters, …) is mutated outside of the active view's
// refresh path — otherwise switching back into those views would hit a fresh
// cache entry that was built with the old URL parameters.
//
// `affectedViews` (optional): if provided, only these views are invalidated
// (intersected with "not exceptView"). Omit to invalidate every non-active
// view — the safer default for state changes whose scope is hard to pin down.
export function invalidateCachedViews(state, exceptView = null, affectedViews = null) {
  if (!state) return;
  const scopeSet = Array.isArray(affectedViews) && affectedViews.length > 0 ? new Set(affectedViews) : null;
  const shouldInvalidate = (key) => {
    if (key === exceptView) return false;
    if (scopeSet && !scopeSet.has(key)) return false;
    return true;
  };
  if (state.readyViews && typeof state.readyViews === "object") {
    for (const key of Object.keys(state.readyViews)) {
      if (shouldInvalidate(key)) delete state.readyViews[key];
    }
  }
  if (state.viewRefreshedAt && typeof state.viewRefreshedAt === "object") {
    for (const key of Object.keys(state.viewRefreshedAt)) {
      if (shouldInvalidate(key)) delete state.viewRefreshedAt[key];
    }
  }
}

// Derived helpers for refreshPhase. Prefer these over raw string comparisons
// so a future phase value rename stays local to this module.
export function isRefreshInFlight(state) {
  return Boolean(state) && state.refreshPhase !== REFRESH_PHASE_IDLE;
}

export function isPrimaryRefreshInFlight(state) {
  return Boolean(state) && state.refreshPhase === REFRESH_PHASE_PRIMARY;
}
