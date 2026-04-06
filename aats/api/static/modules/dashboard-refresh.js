import {
  AUTO_REFRESH_MS,
  VIEW_FRESHNESS_MS,
  buildDashboardBundleRequestPlan,
} from "./store.js";

export function createDashboardRefreshController({
  state,
  nodes,
  fetchDashboardBundle,
  renderShell,
  renderBanners,
  applyPanelResults,
  shouldRedirectToLogin,
}) {
  function hasReadyView(view) {
    return Boolean(state.readyViews[view]);
  }

  function isViewFresh(view) {
    const lastRefresh = state.viewRefreshedAt[view];
    return Boolean(lastRefresh) && Date.now() - lastRefresh < VIEW_FRESHNESS_MS;
  }

  function isBootstrapping() {
    return !state.lastRefreshAt && state.refreshing;
  }

  function shouldRenderLoadingState(view) {
    if (!view) return false;
    if (state.loadingView === view) return true;
    return isBootstrapping() && !hasReadyView(view);
  }

  function isBackgroundRefreshingView(view) {
    return Boolean(view) && state.refreshing && !shouldRenderLoadingState(view) && hasReadyView(view) && state.activeView === view;
  }

  function setPendingPanels(panelKeys = [], pending = true) {
    panelKeys.forEach((key) => {
      if (!key) return;
      if (pending) {
        state.pendingPanels[key] = true;
        return;
      }
      delete state.pendingPanels[key];
    });
  }

  function cancelScheduledRefresh() {
    if (!state.refreshTimer) return;
    window.clearTimeout(state.refreshTimer);
    state.refreshTimer = null;
  }

  function scheduleRefresh() {
    cancelScheduledRefresh();
    if (state.actionInFlight) return;
    if (nodes.autoRefreshToggle && !nodes.autoRefreshToggle.checked) return;
    if (document.visibilityState !== "visible") return;
    state.refreshTimer = window.setTimeout(() => void refreshDashboard(), AUTO_REFRESH_MS);
  }

  function handleVisibilityChange() {
    if (document.visibilityState !== "visible") {
      cancelScheduledRefresh();
      return;
    }
    if (nodes.autoRefreshToggle && !nodes.autoRefreshToggle.checked) return;
    if (state.refreshing) {
      state.pendingRefresh = true;
      return;
    }
    void refreshDashboard();
  }

  async function refreshDashboard({ manual = false } = {}) {
    if (!manual && document.visibilityState !== "visible") {
      cancelScheduledRefresh();
      return;
    }
    if (state.actionInFlight && !manual) {
      state.pendingRefresh = true;
      return;
    }
    if (state.refreshing) {
      state.pendingRefresh = true;
      if (manual) {
        state.flash = { tone: "info", message: "当前正在刷新，已排队一次新的刷新请求。" };
        renderBanners();
      }
      return;
    }
    const refreshingView = state.activeView;
    const refreshPlan = buildDashboardBundleRequestPlan(refreshingView, state);
    const refreshGeneration = state.refreshGeneration + 1;
    let deferredRefreshStarted = false;
    state.refreshGeneration = refreshGeneration;
    setPendingPanels(refreshPlan.deferredPanels, Boolean(refreshPlan.deferredPath));
    cancelScheduledRefresh();
    state.refreshing = true;
    renderShell();
    try {
      const results = await fetchDashboardBundle(refreshPlan.primaryPath);
      if (state.refreshGeneration !== refreshGeneration) {
        return;
      }
      applyPanelResults(results);
      state.readyViews[refreshingView] = true;
      state.viewRefreshedAt[refreshingView] = Date.now();
      if (shouldRedirectToLogin()) {
        window.location.replace("/login");
        return;
      }
      state.lastRefreshAt = new Date();
      if (manual) {
        state.flash = { tone: "info", message: "页面数据已刷新。" };
      }
      if (refreshPlan.deferredPath) {
        deferredRefreshStarted = true;
        void refreshDeferredPanels({
          path: refreshPlan.deferredPath,
          panelKeys: refreshPlan.deferredPanels,
          refreshGeneration,
        });
      }
    } catch (error) {
      if (state.refreshGeneration !== refreshGeneration) return;
      if (error && typeof error === "object" && "name" in error && error.name === "AbortError") {
        const canBackgroundRetry = hasReadyView(refreshingView);
        state.flash = {
          tone: "warning",
          message: canBackgroundRetry ? "请求超时，正在重试…" : "请求超时，首屏数据仍未完成，请稍后手动重试。",
        };
        renderBanners();
        if (canBackgroundRetry) {
          state.pendingRefresh = true;
        }
        return;
      }
      if (manual) {
        state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
      }
    } finally {
      if (!deferredRefreshStarted && state.refreshGeneration === refreshGeneration) {
        setPendingPanels(refreshPlan.deferredPanels, false);
      }
      state.refreshing = false;
      if (state.loadingView === refreshingView) {
        state.loadingView = null;
      }
      renderShell();
      if (state.pendingRefresh) {
        state.pendingRefresh = false;
        void refreshDashboard();
        return;
      }
      scheduleRefresh();
    }
  }

  async function refreshDeferredPanels({ path, panelKeys = [], refreshGeneration }) {
    try {
      const results = await fetchDashboardBundle(path);
      if (state.refreshGeneration !== refreshGeneration) {
        return;
      }
      applyPanelResults(results);
    } catch (error) {
      if (state.refreshGeneration !== refreshGeneration) {
        return;
      }
      panelKeys.forEach((key) => {
        state.errors[key] = error instanceof Error ? error.message : String(error);
      });
    } finally {
      if (state.refreshGeneration !== refreshGeneration) {
        return;
      }
      setPendingPanels(panelKeys, false);
      renderShell();
    }
  }

  return {
    cancelScheduledRefresh,
    handleVisibilityChange,
    hasReadyView,
    isBackgroundRefreshingView,
    isBootstrapping,
    isViewFresh,
    refreshDashboard,
    scheduleRefresh,
    setPendingPanels,
    shouldRenderLoadingState,
  };
}

