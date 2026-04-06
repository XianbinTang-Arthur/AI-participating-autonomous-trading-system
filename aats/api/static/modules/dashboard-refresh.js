import { DEFERRED_BUNDLE_TIMEOUT_MS } from "./api-client.js";
import { isFlashLive, setFlash } from "./flash.js";
import {
  AUTO_REFRESH_MS,
  REFRESH_PHASE_DEFERRED,
  REFRESH_PHASE_IDLE,
  REFRESH_PHASE_PRIMARY,
  VIEW_FRESHNESS_MS,
  buildDashboardBundleRequestPlan,
} from "./store.js";

export function createDashboardRefreshController({
  state,
  nodes,
  fetchDashboardBundle,
  renderShell,
  applyPanelResults,
  shouldRedirectToLogin,
}) {
  // Note: this controller does NOT receive renderBanners. Earlier versions of
  // refreshDashboard set state.flash + called renderBanners() inline (e.g. the
  // "已排队" notice in isPrimaryInFlight branch, see C6 in the round-4 review),
  // but every such producer site has been removed. The remaining flash sets
  // here go through setFlash() and rely on the next renderShell() (in finally{}
  // or the chained refresh) to surface them — which is the right behaviour
  // because shell-renderer's sticky-flash design re-emits the banner DOM on
  // every renderBanners() call within the 8s TTL window.

  // AbortControllers for the currently running primary AND deferred fetches.
  // When a new refresh starts (any generation bump), it aborts any leftover
  // in-flight fetches from prior generations so bandwidth isn't wasted on
  // responses that would be discarded by the generation guard anyway. This
  // also prevents the "fast tab-switch" pile-up where every in-flight fetch
  // still runs to completion in the browser even though nobody is reading
  // the result.
  //
  // Note that aborting alone does NOT prevent stale pendingPanels clobber:
  // the aborted fetch's finally block still runs in a microtask AFTER the
  // new generation has already set up its pendingPanels. Ownership tracking
  // inside setPendingPanels (storing generation numbers instead of booleans)
  // is what actually guards against cross-generation writes.
  let currentPrimaryAbort = null;
  let currentDeferredAbort = null;

  function hasReadyView(view) {
    return Boolean(state.readyViews[view]);
  }

  function isViewFresh(view) {
    const lastRefresh = state.viewRefreshedAt[view];
    return Boolean(lastRefresh) && Date.now() - lastRefresh < VIEW_FRESHNESS_MS;
  }

  function isPrimaryInFlight() {
    return state.refreshPhase === REFRESH_PHASE_PRIMARY;
  }

  function isRefreshInFlight() {
    return state.refreshPhase !== REFRESH_PHASE_IDLE;
  }

  function isBootstrapping() {
    // Bootstrap = first primary fetch, no data has ever landed. Deferred phase
    // never counts as bootstrap because by definition primary already finished.
    return !state.lastRefreshAt && isPrimaryInFlight();
  }

  function shouldRenderLoadingState(view) {
    if (!view) return false;
    if (state.loadingView === view) return true;
    return isBootstrapping() && !hasReadyView(view);
  }

  function isBackgroundRefreshingView(view) {
    return (
      Boolean(view)
      && isRefreshInFlight()
      && !shouldRenderLoadingState(view)
      && hasReadyView(view)
      && state.activeView === view
    );
  }

  // pendingPanels is keyed by panel name, with the *generation that owns the
  // entry* as the value. Storing a generation (rather than a bare boolean)
  // lets the clear path do ownership checking: a stale Gen N "clear" cannot
  // clobber entries that Gen N+1 has already taken ownership of.
  //
  // The map covers BOTH primary-fetch panels (cleared in the primary fetch's
  // finally block) and deferred-fetch panels (cleared in refreshDeferredPanels'
  // finally). Both flows use this same map so refresh-interactivity.js can do
  // a single uniform "is this card refreshing?" check on every render.
  //
  // Downstream consumers (refresh-interactivity.js, home-view.js, risk-view.js)
  // only do truthy checks on these values, so storing numbers >= 1 is
  // backwards-compatible with the existing Boolean coercion. Note that the
  // home/risk view consumers ONLY check specific deferred-panel keys
  // (latestDecision, executionLatest, replayStatus, ...), so adding primary
  // panels to the map doesn't affect their loading-skeleton heuristics.
  function setPendingPanels(panelKeys, generation, { pending = true } = {}) {
    if (!Array.isArray(panelKeys) || panelKeys.length === 0) return;
    if (!Number.isFinite(generation) || generation <= 0) return;
    panelKeys.forEach((key) => {
      if (!key) return;
      if (pending) {
        // New generation always wins ownership. This is the only write path
        // that mutates an entry previously owned by an older generation.
        state.pendingPanels[key] = generation;
        return;
      }
      // Ownership-gated clear: only drop the entry if we're still the owner.
      // Otherwise a newer generation has taken it over, and that newer
      // generation's own finally block will clear it at the right time.
      if (state.pendingPanels[key] === generation) {
        delete state.pendingPanels[key];
      }
    });
  }

  function cancelScheduledRefresh() {
    if (!state.refreshTimer) return;
    window.clearTimeout(state.refreshTimer);
    state.refreshTimer = null;
  }

  function scheduleRefresh() {
    cancelScheduledRefresh();
    // Never overwrite / race with an in-flight refresh. Whichever refresh is
    // currently running will call scheduleRefresh() itself on completion.
    if (isRefreshInFlight()) return;
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
    // An action is mid-flight. The previous implementation silently dropped
    // the visible-again nudge, which could leave the user staring at up to
    // 30s of stale data after a failing action (the success path immediately
    // fires a manual refresh anyway, but the failure path only re-arms a
    // 30s scheduleRefresh, losing the visibility signal entirely). Queue a
    // pendingRefresh so finishAction()'s drain path handles it.
    if (state.actionInFlight) {
      state.pendingRefresh = state.pendingRefresh || { manual: false };
      return;
    }
    if (isPrimaryInFlight()) {
      // A primary fetch is already running — queue a non-manual drain so the
      // visible-again nudge still produces a refresh after the current one.
      state.pendingRefresh = state.pendingRefresh || { manual: false };
      return;
    }
    void refreshDashboard();
  }

  async function refreshDashboard({ manual = false } = {}) {
    // First-refresh (bootstrap) must always run, even if the tab was opened in
    // the background. Otherwise init() degenerates into a permanent skeleton
    // state until the user foregrounds the tab.
    const isFirstRefresh = !state.lastRefreshAt;
    if (!manual && !isFirstRefresh && document.visibilityState !== "visible") {
      cancelScheduledRefresh();
      return;
    }
    if (state.actionInFlight) {
      // Even manual refresh requests must queue while an action is in flight,
      // not just background auto-refreshes. Without this guard,
      // dispatchAction("navigate-view") on the *same* view (which fires a
      // manual refresh from app.js) would race the action's own
      // post-success refreshDashboard call: both would slip past the
      // !manual check and run concurrently. The second one would still
      // bounce off isPrimaryInFlight below, but in the meantime the first
      // burns a network round-trip whose result the generation guard then
      // discards. Quietly queueing here lets the action's finishAction
      // drain the request through the regular path with the manual flag
      // intact, eliminating the race.
      //
      // Manual flag preservation: an existing queued manual flag must not
      // be downgraded by a subsequent background auto-refresh, otherwise
      // the queued manual request loses its "已刷新" flash when it
      // finally runs. Mirror the isPrimaryInFlight branch below.
      const previouslyManual = Boolean(state.pendingRefresh?.manual);
      state.pendingRefresh = { manual: manual || previouslyManual };
      return;
    }
    if (isPrimaryInFlight()) {
      // Queue this request. If a manual request gets queued while a
      // background auto-refresh is running, remember the manual flag so the
      // drained request still shows the "已刷新" flash.
      const previouslyManual = Boolean(state.pendingRefresh?.manual);
      state.pendingRefresh = { manual: manual || previouslyManual };
      // No "已排队" flash here on purpose:
      //
      // The user has not actually waited yet — finally{} below drains the
      // pendingRefresh in the same microtask the current fetch resolves, so
      // the only window in which "已排队" would be visible is the time it
      // takes the current primary fetch to finish (typically <1s). Sitting on
      // a sticky 8s "已排队" banner long after the drain has already produced
      // its own "页面数据已刷新" / action-success flash is more confusing
      // than informative.
      //
      // refreshButton is NOT locked during a normal refresh
      // (shell-renderer.js syncRefreshInteractivity only locks during loading
      // state, not during background refresh), so a user double-tap landing
      // here is fully expected and should not produce a banner. The shimmer
      // applied by renderRefreshIndicators already communicates "refresh in
      // progress".
      return;
    }

    const refreshingView = state.activeView;
    const refreshPlan = buildDashboardBundleRequestPlan(refreshingView, state);
    const refreshGeneration = state.refreshGeneration + 1;
    let deferredRefreshStarted = false;
    state.refreshGeneration = refreshGeneration;

    // Supersede any leftover in-flight fetches from prior generations.
    // Primary is normally unreachable here (the isPrimaryInFlight guard
    // above would have queued us), but deferred CAN still be running — its
    // phase doesn't block a new primary. Aborting both frees bandwidth; the
    // responses would otherwise run to completion and be discarded by the
    // generation guard. See the comment on currentPrimaryAbort for why
    // ownership tracking in setPendingPanels is still required on top of
    // this (microtask ordering means the aborted fetch's finally still runs
    // after we've set up new pendingPanels).
    if (currentPrimaryAbort) {
      try { currentPrimaryAbort.abort(); } catch { /* ignore */ }
      currentPrimaryAbort = null;
    }
    if (currentDeferredAbort) {
      try { currentDeferredAbort.abort(); } catch { /* ignore */ }
      currentDeferredAbort = null;
    }

    // Mark BOTH primary AND deferred panels as pending while the primary
    // fetch is in flight. The pendingPanels mechanism is what
    // refresh-interactivity.js uses to lock the action buttons inside any
    // [data-panel-key] card whose key appears in pendingPanels — including
    // open detail drawers, which the dashboard's currentRefreshInteractivityRoots
    // also passes to syncRefreshDisabledButtons. Without marking the primary
    // panels here, manual / background refreshes leave their action buttons
    // clickable while the underlying card is visibly shimmering, which lets
    // the user fire actions against pre-refresh stale data and confuses the
    // intent of the loading affordance.
    //
    // Primary panels get cleared from pendingPanels in the finally block
    // below the moment the primary fetch resolves; the deferred panels stay
    // pending until refreshDeferredPanels finishes (or this generation gets
    // superseded). The two-phase clear is what keeps deferred-only background
    // fill-ins from re-locking the whole view every 30s.
    setPendingPanels(refreshPlan.primaryPanels, refreshGeneration);
    setPendingPanels(refreshPlan.deferredPanels, refreshGeneration);
    cancelScheduledRefresh();
    state.refreshPhase = REFRESH_PHASE_PRIMARY;

    const abortController = new AbortController();
    currentPrimaryAbort = abortController;

    renderShell();
    try {
      const results = await fetchDashboardBundle(refreshPlan.primaryPath, {
        signal: abortController.signal,
      });
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
      // Only surface the generic "页面数据已刷新" notice if no caller-set flash
      // is *still live* on screen. Action handlers (runAction et al.) set a
      // much more specific success message right before awaiting
      // refreshDashboard; unconditionally overwriting that with the generic
      // notice would silently destroy the action-specific outcome message.
      //
      // We use isFlashLive() rather than `!state.flash` to also cover the
      // (worst-case 1s) window where state.flash exists but its lazy
      // _expiresAt has already passed and tickFlashExpiry has not yet
      // cleared it. In that window the flash is dead from the user's POV,
      // so the manual "页面数据已刷新" notice should still take over.
      if (manual && !isFlashLive(state)) {
        setFlash(state, "info", "页面数据已刷新。");
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
      // Generation mismatch = this refresh was superseded by a newer one
      // (via supersede abort or plain enqueue drain). Silently bail; the
      // winner owns all shared state from here on.
      if (state.refreshGeneration !== refreshGeneration) return;
      if (error && typeof error === "object" && "name" in error && error.name === "AbortError") {
        const canBackgroundRetry = hasReadyView(refreshingView);
        // The previous implementation lied about "重试" by setting
        // pendingRefresh — but the drain path would just re-hit the same
        // timeout and loop. Instead, surface an honest warning and let the
        // regular scheduleRefresh cycle handle the retry at AUTO_REFRESH_MS.
        //
        // Only flash on two situations to avoid banner spam on flaky networks:
        //   1. Manual refreshes — the user clicked a button and expects
        //      feedback on the outcome, even when negative.
        //   2. Bootstrap failures (!canBackgroundRetry) — the user is
        //      staring at a skeleton with no data and needs to know why.
        // Background auto-refresh timeouts on views that already have data
        // are swallowed silently; the shimmer indicator already communicates
        // "refresh in progress" and the next cycle will retry.
        if (manual || !canBackgroundRetry) {
          setFlash(
            state,
            "warning",
            canBackgroundRetry
              ? "请求超时，将在下次自动刷新时重试。"
              : "请求超时，首屏数据仍未完成，请稍后手动重试。",
          );
        }
        // No renderBanners() here — the finally block will renderShell()
        // if this generation is still current, which re-renders banners.
        return;
      }
      // Surface non-abort network/server errors when:
      //   1. Manual refresh — the user clicked and expects feedback.
      //   2. Bootstrap failure (no data on screen yet) — the user is staring
      //      at a skeleton and deserves to know why, not be left wondering
      //      if the spinner is just slow.
      // Background auto-refresh failures on views that already have data are
      // intentionally silent to avoid banner spam on flaky networks; the
      // next auto-refresh cycle will retry.
      if (manual || !hasReadyView(refreshingView)) {
        setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      }
    } finally {
      // Release our controller reference if it's still ours. If it has been
      // replaced by a newer generation, leave the new owner alone.
      if (currentPrimaryAbort === abortController) {
        currentPrimaryAbort = null;
      }
      // Never mutate shared phase/loadingView state on behalf of a stale
      // generation; the winning generation owns it and will set it correctly.
      const isCurrentGeneration = state.refreshGeneration === refreshGeneration;
      if (!isCurrentGeneration) {
        return;
      }
      // Always release the primary panels from pending: we're either past the
      // primary fetch (success) or abandoning it (error/abort). Either way,
      // the lock that primary-pending applied to action buttons inside those
      // cards must come off so users can interact with the (now-stable) data.
      // setPendingPanels is ownership-checked, so a newer generation that
      // already took these keys over is unaffected.
      setPendingPanels(refreshPlan.primaryPanels, refreshGeneration, { pending: false });
      if (!deferredRefreshStarted) {
        setPendingPanels(refreshPlan.deferredPanels, refreshGeneration, { pending: false });
      }
      // Transition phase: if deferred is chasing, stay in "deferred" so
      // isBackgroundRefreshingView / status chip still reflect the fill-in;
      // otherwise back to idle.
      state.refreshPhase = deferredRefreshStarted ? REFRESH_PHASE_DEFERRED : REFRESH_PHASE_IDLE;
      if (state.loadingView === refreshingView) {
        state.loadingView = null;
      }
      if (state.pendingRefresh) {
        const drained = state.pendingRefresh;
        state.pendingRefresh = null;
        // Don't render twice — the next refreshDashboard() immediately sets
        // refreshPhase and calls renderShell itself.
        void refreshDashboard(drained);
        return;
      }
      renderShell();
      scheduleRefresh();
    }
  }

  async function refreshDeferredPanels({ path, panelKeys = [], refreshGeneration }) {
    // Register our own abort controller so a newer refreshDashboard() can
    // cancel us via currentDeferredAbort.abort() — see the supersede block
    // at the top of refreshDashboard. Without this, deferred bundles would
    // keep running up to DEFERRED_BUNDLE_TIMEOUT_MS after the user switches
    // views or force-refreshes.
    const abortController = new AbortController();
    currentDeferredAbort = abortController;
    try {
      const results = await fetchDashboardBundle(path, {
        timeout: DEFERRED_BUNDLE_TIMEOUT_MS,
        signal: abortController.signal,
      });
      if (state.refreshGeneration !== refreshGeneration) {
        return;
      }
      applyPanelResults(results);
    } catch (error) {
      if (state.refreshGeneration !== refreshGeneration) {
        return;
      }
      // Supersede aborts are already caught by the generation guard above:
      // refreshDashboard() bumps state.refreshGeneration BEFORE calling
      // currentDeferredAbort.abort(), so by the time the AbortError reaches
      // this catch, the generation check has already returned. Any AbortError
      // that makes it past that guard is our OWN DEFERRED_BUNDLE_TIMEOUT_MS
      // timer firing — the deferred bundle genuinely failed to complete, so
      // surface it to the user as a panel error rather than silently leaving
      // stale data on screen with no indication anything went wrong.
      const isTimeoutAbort =
        error && typeof error === "object" && "name" in error && error.name === "AbortError";
      const message = isTimeoutAbort
        ? "请求超时，请稍后重试。"
        : error instanceof Error ? error.message : String(error);
      panelKeys.forEach((key) => {
        state.errors[key] = message;
      });
    } finally {
      if (currentDeferredAbort === abortController) {
        currentDeferredAbort = null;
      }
      // Ownership-checked clear: only drops entries still owned by THIS
      // generation. The previous implementation cleared unconditionally,
      // which raced with a newer generation's setPendingPanels and could
      // wipe its freshly-set pending flags within the microtask window
      // after our fetch rejects. See setPendingPanels for details.
      setPendingPanels(panelKeys, refreshGeneration, { pending: false });
      if (state.refreshGeneration !== refreshGeneration) {
        return;
      }
      // Deferred has completed for the current generation — return to idle
      // if no newer primary has taken over.
      if (state.refreshPhase === REFRESH_PHASE_DEFERRED) {
        state.refreshPhase = REFRESH_PHASE_IDLE;
      }
      renderShell();
      // Ensure the auto-refresh timer is armed after deferred completes.
      // Primary's finally already called scheduleRefresh(), but at that point
      // isRefreshInFlight() was still true (we were in deferred phase), so
      // scheduleRefresh bailed. Now that we're back to idle, arm it.
      scheduleRefresh();
    }
  }

  return {
    cancelScheduledRefresh,
    handleVisibilityChange,
    hasReadyView,
    isBackgroundRefreshingView,
    isBootstrapping,
    isRefreshInFlight,
    isViewFresh,
    refreshDashboard,
    scheduleRefresh,
    shouldRenderLoadingState,
  };
}
