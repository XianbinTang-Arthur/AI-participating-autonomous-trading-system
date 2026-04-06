import {
  DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS,
  DEFAULT_EXIT_EXECUTION_HISTORY_PAGING,
} from "./store.js";
import { VIEW_ROUTES, resolveKnownView } from "./view-router.js";

export const EXIT_EXECUTION_HISTORY_ACTION_FILTERS = new Set(["all", "refresh_exchange_state", "retry_limit_lookup", "safe_cancel"]);
export const EXIT_EXECUTION_HISTORY_WINDOW_FILTERS = new Set(["all", "1", "6", "24", "168", "720"]);

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

