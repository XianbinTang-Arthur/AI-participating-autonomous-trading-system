import { notice, pill, primaryStatusPanel } from "./components.js";
import { clearFlash, getFlashTtl } from "./flash.js";
import {
  emptyState,
  formatMaybeTimestamp,
  formatNumber,
  formatRelativeAge,
  middleEllipsis,
} from "./formatters.js";
import { syncRefreshDisabledButtons } from "./refresh-interactivity.js";
import { REFRESH_PHASE_PRIMARY } from "./store.js";
import {
  readableFamilyExecutionSummary,
  localizeError,
  operationalStatusCopy,
  operationalStatusHeadline,
  operationalStatusLabel,
  readableOverlayParentSignalSummary,
  readableState,
  reviewStatusLabel,
  toneForOrderStatus,
  toneForRuntimeState,
  tradingStatusLabel,
} from "./terms.js";
import { VIEW_META } from "./view-router.js";

export function createDashboardShellRenderer({
  state,
  nodes,
  viewLinks,
  viewSections,
  renderActiveView,
  shouldRenderLoadingState,
  isBackgroundRefreshingView,
  isBootstrapping,
  hasResolvedPanel,
  hasResolvedAuthContext,
  operatorCanWrite,
  controlPermissionMessage,
  isProtectedViewAuthBlocked = () => false,
  resumeActionAvailable,
  resumeActionHintText,
  syncExitExecutionNavigationLinks,
  localizedRecoveryReasons,
  isPausedAwaitingResume,
}) {
  // #20 修复说明：renderCache 用 WeakMap 而不是普通 Map，是有意为之但容易踩坑。
  //
  // 缓存的 key 是 view <section> DOM 节点。WeakMap 的好处是：如果某个 view
  // section 在 DOM 重建时被丢弃，这条缓存条目会自动被 GC 掉，不需要我们
  // 手动清理。坏处是：如果应用层代码 *重新* 创建一个新的 section 节点
  // （比如热重载、整页 re-render），即使新节点的 cacheKey 文本完全一样，
  // 也不会命中缓存——因为 WeakMap 的等价语义是"同一个对象引用"而不是
  // "同一个 cacheKey"。在当前架构里，view section 节点是 index.html 一开始
  // 就静态写好的，整个会话期内不变（renderActiveView 只会改 innerHTML），
  // 所以这个 trade-off 是可接受的：节点稳定、缓存命中率高、不需要手动清理。
  //
  // 如果未来引入了"切走某 view 时把它的 section 整个 detach 再重新挂回"
  // 的优化（比如 lazy view loading），缓存命中率会下降——这是可观察的：
  // 视觉会出现一次"全量 patch"的瞬间，但不会出错。要想恢复命中率，可以
  // 切回普通 Map<viewName, cacheKey> 并自行做生命周期管理。
  const renderCache = new WeakMap();

  // Sticky-flash TTL constant (FLASH_DEFAULT_TTL_MS) is now defined in
  // modules/flash.js so the helper module owns the entire sticky-flash
  // protocol (set / clear / live-check / TTL). See flash.js docstring for
  // the design rationale and the lazy _expiresAt stamping contract.

  function renderShell() {
    syncExitExecutionNavigationLinks();
    viewLinks.forEach((link) => link.classList.toggle("is-active", link.dataset.view === state.activeView));
    viewSections.forEach((section) => section.classList.toggle("is-active", section.dataset.view === state.activeView));
    renderPageChrome();
    renderSessionSummary();
    renderRuntimeModeBadge();
    renderStatusRibbon();
    renderBanners();
    renderActiveView();
    renderRefreshIndicators();
    updateActionAccess();
    updateRefreshLabel();
    syncRefreshInteractivity();
  }

  // P0-b Task 2.1: 全局顶栏 runtime mode badge.
  // 数据源: state.data.aiRuntime.effective_operating_mode (从 /ai/runtime 取;
  //         该 endpoint 已纳入 CORE_SPECS, 所以任何 view + 每次 refresh 都更新).
  // 刷新频率: 跟随 dashboard 的 30s auto-refresh 周期,以及任何手动 refresh.
  //
  // 颜色/文案 (2026-04-23 勘误后):
  //   baseline_only      → 灰底蓝字 "仅 baseline, 不使用 AI"
  //   ai_assisted        → 温和琥珀色 "AI 咨询实盘中"
  //   ai_decision_maker  → 稳态蓝色 "AI 决策者已启用"
  //   其它 / 未知         → 灰底, "加载中…"
  function renderRuntimeModeBadge() {
    const badge = nodes.runtimeModeBadge;
    const body = nodes.runtimeModeBadgeBody;
    if (!badge || !body) return;

    const aiRuntime = state.data?.aiRuntime || {};
    const effective = String(aiRuntime.effective_operating_mode || "").trim();
    const configured = String(aiRuntime.configured_operating_mode || "").trim();

    // aiRuntime 还没到 / ai_service 未装配 → 隐藏 badge 避免误导
    if (!effective) {
      badge.hidden = true;
      return;
    }
    badge.hidden = false;

    let toneClass = "runtime-mode-badge--unknown";
    let text = "";
    let title = "";
    if (effective === "baseline_only") {
      toneClass = "runtime-mode-badge--baseline-only";
      text = "仅按基础策略运行";
      title = "当前不让 AI 参与真实交易决策，是否下单由基础策略和风控共同决定。点击查看语义说明。";
    } else if (effective === "ai_assisted") {
      toneClass = "runtime-mode-badge--ai-assisted";
      text = "AI 辅助判断";
      title = "AI 提供辅助判断，最终是否下单仍由基础策略、风控和执行边界共同决定。点击查看语义说明。";
    } else if (effective === "ai_decision_maker") {
      toneClass = "runtime-mode-badge--ai-decision-maker";
      text = "AI 决策者已启用";
      title = "AI 可以给出最终交易意图，但仍受风控、暂停开关、执行门与审计链约束。点击查看语义说明。";
    } else {
      toneClass = "runtime-mode-badge--unknown";
      text = "运行模式待确认";
      title = "当前运行模式不在已知语义列表内，请以配置和运行日志为准。点击查看已知语义。";
    }

    // 若 effective 与 configured 不一致, 提示 operator 手动 override 状态
    if (configured && configured !== effective) {
      text += `（默认：${readableState(configured)}）`;
      title += ` 配置默认是${readableState(configured)}，但当前以${readableState(effective)}生效。`;
    }

    patchClassName(badge, `runtime-mode-badge ${toneClass}`);
    // `<span class="runtime-mode-badge__tag">模式</span>` 已经渲染了 "模式" 前缀,
    // body 只放冒号后的值部分避免重复显示 "模式" 两次.
    patchText(body, text);
    if (badge.getAttribute("title") !== title) {
      badge.setAttribute("title", title);
    }
  }

  function renderPageChrome() {
    const meta = VIEW_META[state.activeView] || VIEW_META.home;
    document.title = meta.docTitle;
    patchText(nodes.pageEyebrow, meta.eyebrow);
    patchText(nodes.pageHeading, meta.heading);
    patchText(nodes.pageCopy, meta.copy);
    patchClassName(nodes.pageHead, meta.hidePageHead ? "page-head is-hidden" : "page-head");
  }

  function renderSessionSummary() {
    const session = state.data.session || {};
    patchText(nodes.sessionIdentityValue, session.identity || "未登录");
    patchText(nodes.sessionRoleValue, `当前身份：${readableState(session.role || "anonymous")}`);
    patchClassName(nodes.authStateChip, `status-pill tone-${session.authenticated ? "positive" : "neutral"}`);
    patchText(nodes.authStateChip, session.authenticated ? "已登录" : "未登录");
  }

  function renderStatusRibbon() {
    if (state.activeView !== "home" || isProtectedViewAuthBlocked()) {
      patchClassName(nodes.statusRibbon, "status-ribbon is-hidden");
      return;
    }

    if (shouldRenderLoadingState("home")) {
      patchClassName(nodes.statusRibbon, "status-ribbon status-ribbon--home");
      patchHtml(nodes.statusRibbon, renderHomeRibbonSkeleton());
      return;
    }

    const health = state.data.health || {};
    const recovery = state.data.systemRecovery?.recovery || {};
    const reconciliation = state.data.reconciliationLatest?.reconciliation || null;
    const portfolio = state.data.portfolio?.portfolio || {};
    const blockerControl = state.data.blockerControl || {};
    const blockers = blockerControl.blockers || state.data.blockers?.blockers || [];
    const primaryBlocker = blockerControl.primary_blocker || blockers[0] || null;
    const latestDecision = state.data.latestDecision || {};
    const latestOrder = state.data.executionLatest?.latest_order || null;
    const metrics = state.data.metrics || {};
    const operationalLabel = operationalStatusLabel({ health, recovery, blockers, reconciliation });
    const runtimeStateValue = health.runtime_state || health.overall_status;

    if (!nodes.statusRibbon) return;
    patchClassName(nodes.statusRibbon, "status-ribbon status-ribbon--home");
    patchHtml(
      nodes.statusRibbon,
      [
        `<div class="status-ribbon__primary">${primaryStatusPanel({
          eyebrow: "主页状态总览",
          title: "",
          headline: homeRibbonHeadline({ health, recovery, blockers, reconciliation }),
          summary: "",
          tone: homeRibbonTone({ health, recovery, blockers, reconciliation }),
          pills: [
            pill(`运行状态 ${runtimeStateValue ? readableState(runtimeStateValue) : operationalLabel}`, toneForRuntimeState(runtimeStateValue || recovery.recovery_state)),
            pill(`自动交易 ${tradingStatusLabel(recovery)}`, recovery.safe_to_trade ? "positive" : isPausedAwaitingResume(recovery) ? "warning" : "danger"),
            pill(`人工复核 ${reviewStatusLabel(recovery.review_required)}`, recovery.review_required ? "warning" : "outline"),
          ],
          metrics: [
            {
              label: "最近决策",
              value: latestDecision.decision_id ? readableFamilyExecutionSummary(latestDecision.position_target || {}, "保持当前仓位") : "暂无",
              meta: latestDecision.decision_id
                ? [
                    formatMaybeTimestamp(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts),
                    readableOverlayParentSignalSummary(latestDecision.position_target || {}, ""),
                  ].filter(Boolean).join(" | ")
                : "当前还没有最近决策记录",
              tone: latestDecision.decision_id ? "info" : "neutral",
            },
            {
              label: "最新委托",
              value: latestOrder ? readableState(latestOrder.status || "unknown") : "暂无活动委托",
              meta: middleEllipsis(latestOrder?.client_order_id, 10, 6, "暂未生成委托"),
              tone: toneForOrderStatus(latestOrder?.status),
            },
            {
              label: "恢复限制",
              value: isPausedAwaitingResume(recovery)
                ? "当前可手动恢复"
                : primaryBlocker
                  ? (primaryBlocker.title || localizeError(primaryBlocker.blocker))
                  : recovery.safe_to_trade
                    ? "当前无硬阻断"
                    : localizedRecoveryReasons(),
              meta: middleEllipsis(reconciliation?.reconciliation_id, 10, 6, "恢复与对账共同决定交易资格"),
              tone: isPausedAwaitingResume(recovery)
                ? "warning"
                : blockers.length > 0 || reconciliation?.halt_required
                  ? "danger"
                  : recovery.safe_to_trade
                    ? "positive"
                    : "warning",
            },
            {
              label: "账户权益",
              value: formatNumber(portfolio.total_equity),
              meta: `活动委托 ${formatNumber(metrics.current_open_order_count, 0, "0")}`,
              tone: "info",
            },
          ],
        })}</div>`,
      ].join(""),
    );
  }

  function renderHomeRibbonSkeleton() {
    return [
      `<div class="status-ribbon__primary">
        <section class="primary-status-panel skeleton-surface skeleton-panel" aria-hidden="true">
          <div class="skeleton-stack">
            <span class="skeleton-line skeleton-line--kicker"></span>
            <span class="skeleton-line skeleton-line--title"></span>
            <span class="skeleton-line skeleton-line--headline"></span>
            <span class="skeleton-line skeleton-line--body"></span>
          </div>
          <div class="skeleton-inline">
            ${Array.from({ length: 3 }, () => '<span class="skeleton-pill"></span>').join("")}
          </div>
          ${loadingTileGrid(3)}
        </section>
      </div>`,
    ].join("");
  }

  function homeRibbonHeadline({ health, recovery, blockers, reconciliation }) {
    return operationalStatusHeadline({ health, recovery, blockers, reconciliation });
  }

  function homeRibbonTone({ health, recovery, blockers, reconciliation }) {
    if (health.halted || blockers.length > 0 || reconciliation?.halt_required) return "danger";
    if (!recovery.safe_to_trade || recovery.review_required) return "warning";
    return "positive";
  }

  function renderBanners() {
    const banners = [];
    const recovery = state.data.systemRecovery?.recovery || {};
    const blockerControl = state.data.blockerControl || {};
    const blockers = blockerControl.blockers || state.data.blockers?.blockers || [];
    const primaryBlocker = blockerControl.primary_blocker || blockers[0] || null;
    const controlsMessage = controlPermissionMessage();
    const authBlocked = isProtectedViewAuthBlocked();

    if (!nodes.bannerContainer) return;
    if (isBootstrapping()) {
      patchHtml(nodes.bannerContainer, "");
      return;
    }
    if (authBlocked && controlsMessage) {
      banners.push(notice(controlsMessage, "warning"));
    }
    if (authBlocked) {
      if (state.flash) {
        const now = Date.now();
        if (!state.flash._expiresAt) {
          state.flash._expiresAt = now + getFlashTtl(state.flash.tone);
        }
        if (now >= state.flash._expiresAt) {
          clearFlash(state);
        } else {
          banners.push(notice(state.flash.message, state.flash.tone));
        }
      }
      patchHtml(nodes.bannerContainer, banners.join(""));
      return;
    }
    if (hasResolvedPanel("systemRecovery") && recovery.safe_to_trade === false) {
      if (isPausedAwaitingResume(recovery)) {
        banners.push(notice(operationalStatusCopy({ recovery }), "info"));
      } else {
        banners.push(
          notice(
            operationalStatusCopy({ recovery, recoveryReasonText: localizedRecoveryReasons() }),
            "warning",
          ),
        );
      }
    }
    if (blockers.length > 0) {
      const headline = primaryBlocker?.title || localizeError(primaryBlocker?.blocker || blockers[0].blocker);
      const detail = primaryBlocker?.recommended_next_step || localizeError(primaryBlocker?.blocker || blockers[0].blocker);
      banners.push(notice(`当前主要阻断原因：${headline}。${detail}`, (primaryBlocker || blockers[0]).affects_execution ? "danger" : "warning"));
    }
    if (controlsMessage) {
      banners.push(notice(controlsMessage, "info"));
    }
    // Sticky flash with lazy TTL: stamp on first render, expire lazily on
    // subsequent renders. Multiple back-to-back renderBanners() calls in the
    // same sync tick re-emit the same banner DOM (instead of consuming the
    // flash on the first call and writing empty banner DOM on the second).
    // Auto-dismissal happens via the per-second tick in app.js, which calls
    // tickFlashExpiry() and re-renders banners when the TTL elapses.
    if (state.flash) {
      const now = Date.now();
      if (!state.flash._expiresAt) {
        state.flash._expiresAt = now + getFlashTtl(state.flash.tone);
      }
      if (now >= state.flash._expiresAt) {
        clearFlash(state);
      } else {
        banners.push(notice(state.flash.message, state.flash.tone));
      }
    }
    patchHtml(nodes.bannerContainer, banners.join(""));
  }

  // Per-second tick from app.js: clear an expired flash and re-render banners
  // so the DOM actually drops the stale banner. renderBanners() lazily expires
  // on render, but without an explicit re-render the user would keep staring
  // at an expired banner until the next state-driven render fired.
  function tickFlashExpiry() {
    if (!state.flash) return;
    if (!state.flash._expiresAt) return;
    if (Date.now() < state.flash._expiresAt) return;
    clearFlash(state);
    renderBanners();
  }

  function updateActionAccess() {
    const actionButtons = [nodes.resumeButton, nodes.haltButton];
    if (!hasResolvedAuthContext()) {
      actionButtons.forEach((node) => {
        if (!node) return;
        node.disabled = true;
        node.title = "正在确认当前账号权限。";
      });
      if (nodes.logoutButton) {
        nodes.logoutButton.disabled = false;
        nodes.logoutButton.title = "";
      }
      patchText(nodes.actionPermissionHint, "正在确认当前账号权限…");
      return;
    }

    if (state.actionInFlight) {
      [nodes.refreshButton, ...actionButtons].forEach((node) => {
        if (!node) return;
        node.disabled = true;
        node.title = "正在提交人工控制请求，请等待本次操作完成。";
      });
      if (nodes.logoutButton) {
        nodes.logoutButton.disabled = false;
        nodes.logoutButton.title = "";
      }
      patchText(nodes.actionPermissionHint, "正在提交人工控制请求，请等待当前操作完成。");
      return;
    }

    const canWrite = operatorCanWrite();
    const buttons = [nodes.refreshButton, ...actionButtons, nodes.logoutButton];
    const disabledReason = controlPermissionMessage() || "当前账号没有人工控制权限。";
    buttons.forEach((node) => {
      if (!node) return;
      const isWriteAction = node !== nodes.logoutButton && node !== nodes.refreshButton;
      if (node === nodes.resumeButton) {
        node.disabled = !canWrite || !resumeActionAvailable();
        node.title = !canWrite ? disabledReason : resumeActionHintText();
        return;
      }
      node.disabled = isWriteAction ? !canWrite : false;
      if (isWriteAction) {
        node.title = !canWrite ? disabledReason : "";
      } else if (node === nodes.refreshButton) {
        node.title = "";
      }
    });
    patchText(nodes.actionPermissionHint, canWrite ? "当前账号可以执行人工控制。" : disabledReason);
  }

  function updateRefreshLabel() {
    // Only the "primary" phase gates the global refresh button and shows the
    // "刷新中" spinner. The "deferred" phase is a background fill-in and must
    // NOT disable the refresh button — the user should be able to kick off a
    // brand-new refresh cycle even while deferred panels are still catching up.
    if (state.refreshPhase === REFRESH_PHASE_PRIMARY) {
      // Bootstrap (no data has landed yet) shows "正在加载" — nothing is
      // being "re-"freshed, and the "刷新" wording would be misleading.
      const isBootstrap = !state.lastRefreshAt;
      patchClassName(nodes.refreshStateChip, "status-pill tone-info refresh-state-chip is-loading");
      patchText(nodes.refreshStateChip, isBootstrap ? "加载中" : "刷新中");
      if (nodes.refreshStateChip) {
        nodes.refreshStateChip.setAttribute(
          "aria-label",
          isBootstrap ? "正在首次加载页面数据" : "正在刷新页面数据",
        );
      }
      if (nodes.refreshButton) {
        nodes.refreshButton.disabled = true;
      }
      patchText(
        nodes.lastRefreshLabel,
        isBootstrap ? "正在加载最新状态…" : "正在刷新最新状态…",
      );
      return;
    }
    if (!state.lastRefreshAt) {
      patchClassName(nodes.refreshStateChip, "status-pill tone-neutral refresh-state-chip");
      patchText(nodes.refreshStateChip, "待刷新");
      if (nodes.refreshStateChip) {
        nodes.refreshStateChip.setAttribute("aria-label", "页面尚未完成首次刷新");
      }
      // Skip re-enabling refreshButton when a manual action is mid-flight.
      // updateActionAccess() disabled it a few lines up in renderShell(), and
      // overwriting it here would make the button look clickable while the
      // runAction guard silently rejects any click. The bootstrap state (no
      // data yet) must not lose that disabled flag either.
      if (nodes.refreshButton && !state.actionInFlight) {
        nodes.refreshButton.disabled = false;
      }
      patchText(nodes.lastRefreshLabel, "尚未刷新");
      return;
    }
    patchClassName(nodes.refreshStateChip, "status-pill tone-positive refresh-state-chip");
    patchText(nodes.refreshStateChip, "已同步");
    if (nodes.refreshStateChip) {
      nodes.refreshStateChip.setAttribute("aria-label", "页面数据已同步");
    }
    // Same rationale as above: updateActionAccess() owns the disabled state
    // during actionInFlight. See the comment in the !state.lastRefreshAt branch.
    if (nodes.refreshButton && !state.actionInFlight) {
      nodes.refreshButton.disabled = false;
    }
    patchText(nodes.lastRefreshLabel, `最近刷新：${formatMaybeTimestamp(state.lastRefreshAt)}（${formatRelativeAge(state.lastRefreshAt)}）`);
  }

  function updateLastRefreshRelativeTime() {
    // Drives the 1s tick that keeps "5 秒前" / "1 分钟前" style relative
    // ages fresh between full renders. renderShell() is only called on state
    // transitions, so without this tick the age string would freeze until
    // the next refresh/action. Only touch the label in steady-state phases:
    //   - PRIMARY phase: updateRefreshLabel is already showing "正在刷新最新
    //     状态…" / "正在加载最新状态…" and a tick here would clobber it.
    //   - No lastRefreshAt yet: the "尚未刷新" text is correct as-is.
    if (state.refreshPhase === REFRESH_PHASE_PRIMARY) return;
    if (!state.lastRefreshAt) return;
    if (!nodes.lastRefreshLabel) return;
    patchText(
      nodes.lastRefreshLabel,
      `最近刷新：${formatMaybeTimestamp(state.lastRefreshAt)}（${formatRelativeAge(state.lastRefreshAt)}）`,
    );
  }

  function syncRefreshInteractivity() {
    // Three layers of "this UI is mid-refresh, lock the action buttons":
    //
    //   1. viewIsLoading — true during the bootstrap / explicit loading
    //      transitions where the entire active view is rendering its skeleton.
    //      In that case the whole view is locked, because clicking through a
    //      skeleton would just dispatch actions against undefined data.
    //
    //   2. isPrimaryRefreshing — true while the primary bundle fetch for the
    //      active view is in flight (manual refresh button click, view switch
    //      to a stale view, 30s background auto-refresh tick). The whole
    //      active view + open drawer get locked for the duration of the
    //      primary fetch (typically <1s). This is what restores the
    //      "卡片/抽屉刷新时按钮锁定" behaviour for every action button —
    //      including buttons inside cards that don't bother to expose a
    //      data-panel-key, of which there are MANY: only ~14 of ~86 surface
    //      cards across the workspace views set panelKey, and the open
    //      drawer never sets one. Without this view-wide lock, the user can
    //      fire actions against pre-refresh stale data while the cards
    //      visibly shimmer.
    //
    //   3. pendingPanels — keyed per-panel. refreshDashboard marks BOTH the
    //      primary and deferred panels as pending while their fetches are in
    //      flight, so the per-card lock follows the shimmer state of each
    //      [data-panel-key] card. After the primary fetch resolves, the
    //      view-wide lock comes off and only the deferred panels stay locked
    //      via pendingPanels until the deferred fetch completes — that's how
    //      the deferred-fill-in phase keeps the rest of the view interactive
    //      while specific cards are still catching up.
    const viewIsLoading = shouldRenderLoadingState(state.activeView);
    const isPrimaryRefreshing = state.refreshPhase === REFRESH_PHASE_PRIMARY;
    syncRefreshDisabledButtons({
      roots: currentRefreshInteractivityRoots(),
      refreshing: viewIsLoading || isPrimaryRefreshing,
      pendingPanels: state.pendingPanels,
      reason: "当前区域正在刷新，请等待刷新完成后再操作。",
      panelReason: "该卡片数据还在刷新，请稍候再操作。",
    });
  }

  function currentRefreshInteractivityRoots() {
    const activeSection = viewSections.find((section) => section.dataset.view === state.activeView) || null;
    const openDrawer = nodes.detailDrawer?.classList.contains("is-open") ? nodes.detailDrawer : null;
    return [activeSection, openDrawer].filter(Boolean);
  }

  function renderRefreshIndicators() {
    const contentNodes = [
      ["home", nodes.homeContent],
      ["overview", nodes.overviewContent],
      ["strategy", nodes.strategyContent],
      ["execution", nodes.executionContent],
      ["risk", nodes.riskContent],
      ["exitExecution", nodes.exitExecutionContent],
      ["replay", nodes.replayContent],
      ["aiAnalysis", nodes.aiAnalysisContent],
      ["aiConfig", nodes.aiConfigContent],
      ["rdp", nodes.rdpContent],
      ["admin", nodes.adminContent],
    ];
    contentNodes.forEach(([view, node]) => {
      patchClassName(node, isBackgroundRefreshingView(view) ? "view-layout is-refreshing" : "view-layout");
    });
    if (nodes.statusRibbon) {
      nodes.statusRibbon.classList.toggle("is-refreshing", isBackgroundRefreshingView("home") && !shouldRenderLoadingState("home"));
    }
  }

  function renderLoadingView() {
    const html = loadingMarkupForView(state.activeView);
    const nodeMap = {
      home: nodes.homeContent,
      overview: nodes.overviewContent,
      strategy: nodes.strategyContent,
      execution: nodes.executionContent,
      risk: nodes.riskContent,
      exitExecution: nodes.exitExecutionContent,
      replay: nodes.replayContent,
      aiAnalysis: nodes.aiAnalysisContent,
      aiConfig: nodes.aiConfigContent,
      rdp: nodes.rdpContent,
      admin: nodes.adminContent,
    };
    patchHtml(nodeMap[state.activeView], html);
  }

  function loadingMarkupForView(view) {
    if (view === "home" || view === "overview") {
      return `
        <div class="panel-grid skeleton-grid" aria-hidden="true">
          <section class="primary-status-panel skeleton-surface skeleton-panel span-12">
            <div class="skeleton-stack">
              <span class="skeleton-line skeleton-line--kicker"></span>
              <span class="skeleton-line skeleton-line--title"></span>
              <span class="skeleton-line skeleton-line--headline"></span>
              <span class="skeleton-line skeleton-line--body"></span>
              <span class="skeleton-line skeleton-line--body-short"></span>
            </div>
            <div class="skeleton-inline">
              ${Array.from({ length: 3 }, () => '<span class="skeleton-pill"></span>').join("")}
            </div>
            ${loadingTileGrid(4)}
          </section>
          <section class="surface-card skeleton-surface skeleton-card span-4">
            <div class="skeleton-stack">
              <span class="skeleton-line skeleton-line--kicker"></span>
              <span class="skeleton-line skeleton-line--title"></span>
              <span class="skeleton-line skeleton-line--body"></span>
            </div>
            ${loadingList(3)}
          </section>
          <section class="surface-card skeleton-surface skeleton-card span-4">
            <div class="skeleton-stack">
              <span class="skeleton-line skeleton-line--kicker"></span>
              <span class="skeleton-line skeleton-line--title"></span>
              <span class="skeleton-line skeleton-line--body-short"></span>
            </div>
            ${loadingTileGrid(4)}
          </section>
          <section class="surface-card skeleton-surface skeleton-card span-4">
            <div class="skeleton-stack">
              <span class="skeleton-line skeleton-line--kicker"></span>
              <span class="skeleton-line skeleton-line--title"></span>
              <span class="skeleton-line skeleton-line--body-short"></span>
            </div>
            ${loadingTileGrid(4)}
          </section>
        </div>
      `;
    }

    if (view === "strategy" || view === "execution" || view === "risk" || view === "exitExecution" || view === "replay" || view === "aiAnalysis" || view === "aiConfig" || view === "rdp" || view === "admin") {
      return `
        <div class="panel-grid skeleton-grid" aria-hidden="true">
          <section class="surface-card hero-card skeleton-surface skeleton-card span-7">
            <div class="skeleton-stack">
              <span class="skeleton-line skeleton-line--kicker"></span>
              <span class="skeleton-line skeleton-line--title"></span>
              <span class="skeleton-line skeleton-line--headline"></span>
              <span class="skeleton-line skeleton-line--body"></span>
            </div>
            ${loadingTileGrid(4)}
          </section>
          <section class="surface-card skeleton-surface skeleton-card span-5">
            <div class="skeleton-stack">
              <span class="skeleton-line skeleton-line--kicker"></span>
              <span class="skeleton-line skeleton-line--title"></span>
              <span class="skeleton-line skeleton-line--body-short"></span>
            </div>
            ${loadingList(4)}
          </section>
          <section class="surface-card skeleton-surface skeleton-card span-12">
            <div class="skeleton-stack">
              <span class="skeleton-line skeleton-line--kicker"></span>
              <span class="skeleton-line skeleton-line--title"></span>
            </div>
            ${loadingList(4)}
          </section>
        </div>
      `;
    }

    return emptyState("正在刷新页面数据…");
  }

  function loadingTileGrid(count) {
    return `
      <div class="skeleton-tile-grid">
        ${Array.from({ length: count }, () => `
          <article class="skeleton-tile">
            <span class="skeleton-tile__label"></span>
            <span class="skeleton-line skeleton-tile__value"></span>
            <span class="skeleton-line skeleton-tile__meta"></span>
          </article>
        `).join("")}
      </div>
    `;
  }

  function loadingList(count) {
    return `
      <div class="skeleton-list">
        ${Array.from({ length: count }, () => `
          <article class="skeleton-row">
            <div class="skeleton-row__head">
              <span class="skeleton-row__title"></span>
              <span class="skeleton-row__badge"></span>
            </div>
            <span class="skeleton-row__value"></span>
            <span class="skeleton-row__value is-short"></span>
          </article>
        `).join("")}
      </div>
    `;
  }

  function patchRenderedSections(sections, containerGetter, fallbackRenderer) {
    const entries = Object.entries(sections || {});
    const hasSectionNodes = entries.length > 0 && entries.every(([key]) => document.getElementById(key));
    if (!hasSectionNodes) {
      const container = containerGetter();
      if (container) {
        patchHtml(container, fallbackRenderer());
      }
      return;
    }
    entries.forEach(([key, html]) => {
      patchHtml(document.getElementById(key), html);
    });
  }

  function patchHtml(node, html) {
    if (!node) return;
    if (renderCache.get(node) === html) return;
    node.innerHTML = html;
    renderCache.set(node, html);
  }

  function patchText(node, text) {
    if (!node) return;
    if (node.textContent === text) return;
    node.textContent = text;
  }

  function patchClassName(node, className) {
    if (!node) return;
    if (node.className === className) return;
    node.className = className;
  }

  return {
    currentRefreshInteractivityRoots,
    patchClassName,
    patchHtml,
    patchRenderedSections,
    patchText,
    renderBanners,
    renderLoadingView,
    renderShell,
    tickFlashExpiry,
    updateLastRefreshRelativeTime,
  };
}
