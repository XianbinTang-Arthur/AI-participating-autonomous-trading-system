import {
  DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
  DEFAULT_EXIT_EXECUTION_HISTORY_PAGING,
} from "./store.js";
import { VIEW_ROUTES, resolveKnownView } from "./view-router.js";

// #19/#37/#38 修复：原本"合法集合"和"渲染下拉的 [value, label] 列表"分散在三处：
//   1. navigation-state.js 的 EXIT_EXECUTION_HISTORY_*_FILTERS（Set，用来校验 URL 参数）
//   2. exit-execution-helpers.js::normalizedExitExecutionHistoryFilters 的 inline 数组（再写一次允许值）
//   3. exit-execution-helpers.js::renderExitExecutionAction*Options 的 [value, label] 数组（下拉显示）
// 想加个新窗口（例如 12 小时）就得三处同步改，必然漂移。
//
// 这里把"value+label"列表作为唯一来源，校验集合直接 derive 出来。两个 helper
// 渲染函数和 normalize 函数都改成 import 这一份配置。
export const EXIT_EXECUTION_HISTORY_ACTION_OPTIONS = [
  ["all", "全部动作"],
  ["refresh_exchange_state", "刷新交易所状态"],
  ["retry_limit_lookup", "重试拆单上限查询"],
  ["safe_cancel", "安全取消退出任务"],
];
export const EXIT_EXECUTION_HISTORY_WINDOW_OPTIONS = [
  ["all", "全部时间"],
  ["1", "最近 1 小时"],
  ["6", "最近 6 小时"],
  ["24", "最近 24 小时"],
  ["168", "最近 7 天"],
  ["720", "最近 30 天"],
];
export const EXIT_EXECUTION_HISTORY_ACTION_FILTERS = new Set(
  EXIT_EXECUTION_HISTORY_ACTION_OPTIONS.map(([value]) => value)
);
export const EXIT_EXECUTION_HISTORY_WINDOW_FILTERS = new Set(
  EXIT_EXECUTION_HISTORY_WINDOW_OPTIONS.map(([value]) => value)
);

const VIEW_REPLAY_FILTERS = new Set(["all", "inventory_only", "target_only", "target_and_inventory"]);

export function coerceReplayParentFilter(value) {
  return VIEW_REPLAY_FILTERS.has(value) ? value : "all";
}

export function normalizeExitExecutionHistoryFilterValue(value) {
  return String(value || "").trim().toLowerCase();
}

export function exitExecutionHistoryWindowThresholdMs(value, now = Date.now()) {
  const normalized = normalizeExitExecutionHistoryFilterValue(value);
  if (!normalized || normalized === "all") {
    return null;
  }
  const hours = Number(normalized);
  if (!Number.isFinite(hours) || hours <= 0) {
    return null;
  }
  return now - (hours * 60 * 60 * 1000);
}

export function createNavigationStateController({ state, viewLinks = [] }) {
  function ensureExitExecutionHistoryState(view = "risk") {
    if (view === "exitExecution") {
      state.ui.exitExecution = state.ui.exitExecution || {};
      state.ui.exitExecution.exitExecutionHistory = {
        ...DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
        ...DEFAULT_EXIT_EXECUTION_HISTORY_PAGING.exitExecution,
        ...(state.ui.exitExecution.exitExecutionHistory || {}),
      };
      return state.ui.exitExecution.exitExecutionHistory;
    }
    state.ui.risk = state.ui.risk || {};
    state.ui.risk.exitExecutionHistory = {
      ...DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
      ...DEFAULT_EXIT_EXECUTION_HISTORY_PAGING.risk,
      ...(state.ui.risk.exitExecutionHistory || {}),
    };
    return state.ui.risk.exitExecutionHistory;
  }

  function copyExitExecutionHistoryFilters(source, target) {
    target.action = String(source?.action || "all");
    target.parent = String(source?.parent || "");
    target.actor = String(source?.actor || "");
    target.windowHours = String(source?.windowHours || "all");
  }

  // #24 修复说明：offset 重置看起来"非对称"，但其实是有意设计，下面把规则
  // 写清楚——避免有人来想"统一一下"反而把分页位置搞丢。
  //
  // 调用场景：用户在风险页（risk）或独立工作台页（exitExecution）改了任何
  // 一个筛选条件（动作、父任务、操作人、时间窗口）。这两个 view 共用一份
  // 筛选语义但各自维护一份独立的 offset/limit 分页位置：
  //
  //   - 风险页是"概览中嵌入的小列表"，offset 通常停在第 0 页。
  //   - 独立工作台是"长历史排查"，offset 可能停在很深的页码。
  //
  // 同步规则：
  //   1. 把 source view 的 4 个筛选字段拷贝到另一边（始终）。
  //   2. 只把"非 source view 那一侧"的 offset 重置成 0；source view 自己
  //      的 offset 保留不变。
  //
  // 这样做的目的是"用户在 A 侧改了筛选，B 侧的旧分页位置已经无效，应该回到
  // 第一页；但 A 侧本来就在主动操作，offset 由 A 侧自己的回调（应用筛选 /
  // 翻页）独立管理，不应该被 sync 函数重置覆盖"。如果两边都重置，A 侧从
  // 第 5 页改个筛选会弹回第 0 页；如果都不重置，B 侧再次进入时会停在
  // 一个对新筛选完全不合理的旧 offset 上，可能直接拿到空数据。
  function syncExitExecutionHistoryFiltersAcrossViews(sourceView = "risk") {
    const sourceState = ensureExitExecutionHistoryState(sourceView);
    const riskState = ensureExitExecutionHistoryState("risk");
    const exitExecutionState = ensureExitExecutionHistoryState("exitExecution");
    copyExitExecutionHistoryFilters(sourceState, riskState);
    copyExitExecutionHistoryFilters(sourceState, exitExecutionState);
    if (sourceView !== "risk") {
      riskState.offset = 0;
    }
    if (sourceView !== "exitExecution") {
      exitExecutionState.offset = 0;
    }
  }

  function activeExitExecutionHistoryView() {
    return state.activeView === "exitExecution" ? "exitExecution" : "risk";
  }

  function activeExitExecutionHistoryState() {
    return ensureExitExecutionHistoryState(activeExitExecutionHistoryView());
  }

  function readExitExecutionHistoryStateFromLocation(search = window.location.search || "") {
    const params = new URLSearchParams(search);
    const parsed = {
      ...DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
      ...DEFAULT_EXIT_EXECUTION_HISTORY_PAGING.exitExecution,
    };
    const action = String(params.get("action") || "").trim();
    if (EXIT_EXECUTION_HISTORY_ACTION_FILTERS.has(action)) {
      parsed.action = action;
    }
    const parentIntentId = String(params.get("parent_intent_id") || "").trim();
    if (parentIntentId) {
      parsed.parent = parentIntentId;
    }
    const actor = String(params.get("actor") || "").trim();
    if (actor) {
      parsed.actor = actor;
    }
    const windowHours = String(params.get("window_hours") || "").trim();
    if (EXIT_EXECUTION_HISTORY_WINDOW_FILTERS.has(windowHours)) {
      parsed.windowHours = windowHours;
    }
    const offset = Number(params.get("offset") || "");
    if (Number.isFinite(offset) && offset >= 0) {
      parsed.offset = offset;
    }
    const limit = Number(params.get("limit") || "");
    if (Number.isFinite(limit) && limit > 0) {
      parsed.limit = limit;
    }
    return parsed;
  }

  function hydrateViewStateFromLocation(view = state.activeView) {
    if (view !== "exitExecution") {
      return;
    }
    const parsed = readExitExecutionHistoryStateFromLocation();
    state.ui.exitExecution = state.ui.exitExecution || {};
    state.ui.exitExecution.exitExecutionHistory = parsed;
    const riskState = ensureExitExecutionHistoryState("risk");
    copyExitExecutionHistoryFilters(parsed, riskState);
    riskState.offset = 0;
  }

  function buildExitExecutionViewPath() {
    const historyState = ensureExitExecutionHistoryState("exitExecution");
    const params = new URLSearchParams({
      offset: String(Math.max(Number(historyState.offset) || 0, 0)),
      limit: String(Math.max(Number(historyState.limit) || DEFAULT_EXIT_EXECUTION_HISTORY_PAGING.exitExecution.limit, 1)),
    });
    const parentIntentId = String(historyState.parent || "").trim();
    const actor = String(historyState.actor || "").trim();
    const action = String(historyState.action || "").trim();
    const windowHours = String(historyState.windowHours || "").trim();
    if (parentIntentId) {
      params.set("parent_intent_id", parentIntentId);
    }
    if (actor) {
      params.set("actor", actor);
    }
    if (action && action !== "all") {
      params.set("action", action);
    }
    if (windowHours && windowHours !== "all") {
      params.set("window_hours", windowHours);
    }
    return `${VIEW_ROUTES.exitExecution}?${params.toString()}`;
  }

  function buildViewPath(view = state.activeView) {
    if (view === "exitExecution") {
      return buildExitExecutionViewPath();
    }
    return VIEW_ROUTES[resolveKnownView(view)] || VIEW_ROUTES.home;
  }

  function syncActiveViewLocationState({ pushHistory = false } = {}) {
    const targetPath = buildViewPath(state.activeView);
    const currentPath = `${window.location.pathname}${window.location.search}`;
    if (currentPath === targetPath) {
      return;
    }
    if (pushHistory) {
      window.history.pushState({ view: state.activeView }, "", targetPath);
      return;
    }
    window.history.replaceState({ view: state.activeView }, "", targetPath);
  }

  function syncExitExecutionNavigationLinks() {
    const exitExecutionHref = buildViewPath("exitExecution");
    viewLinks
      .filter((link) => link.dataset.view === "exitExecution")
      .forEach((link) => {
        if (link.getAttribute("href") !== exitExecutionHref) {
          link.setAttribute("href", exitExecutionHref);
        }
      });
  }

  return {
    activeExitExecutionHistoryState,
    activeExitExecutionHistoryView,
    buildExitExecutionViewPath,
    buildViewPath,
    ensureExitExecutionHistoryState,
    hydrateViewStateFromLocation,
    readExitExecutionHistoryStateFromLocation,
    syncActiveViewLocationState,
    syncExitExecutionHistoryFiltersAcrossViews,
    syncExitExecutionNavigationLinks,
  };
}

