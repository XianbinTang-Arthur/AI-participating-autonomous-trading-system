import { notice, pill, primaryStatusPanel } from "./components.js";
import {
  emptyState,
  formatMaybeTimestamp,
  formatNumber,
  formatRelativeAge,
  middleEllipsis,
} from "./formatters.js";
import { syncRefreshDisabledButtons } from "./refresh-interactivity.js";
import {
  readableFamilyExecutionSummary,
  localizeError,
  operationalStatusCopy,
  operationalStatusHeadline,
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
  resumeActionAvailable,
  resumeActionHintText,
  syncExitExecutionNavigationLinks,
  localizedRecoveryReasons,
  isPausedAwaitingResume,
}) {
  const renderCache = new WeakMap();

  function renderShell() {
    syncExitExecutionNavigationLinks();
    viewLinks.forEach((link) => link.classList.toggle("is-active", link.dataset.view === state.activeView));
    viewSections.forEach((section) => section.classList.toggle("is-active", section.dataset.view === state.activeView));
    renderPageChrome();
    renderSessionSummary();
    renderStatusRibbon();
    renderBanners();
    renderActiveView();
    renderRefreshIndicators();
    updateActionAccess();
    updateRefreshLabel();
    syncRefreshInteractivity();
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
    if (state.activeView !== "home") {
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
            pill(`运行状态 ${readableState(health.runtime_state || health.overall_status)}`, toneForRuntimeState(health.runtime_state || health.overall_status)),
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
                : formatMaybeTimestamp(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts),
              tone: latestDecision.decision_id ? "info" : "neutral",
            },
            {
              label: "最新委托",
              value: readableState(latestOrder?.status || "unknown"),
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
              meta: `活动委托 ${formatNumber(metrics.current_open_order_count)}`,
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

    if (!nodes.bannerContainer) return;
    if (isBootstrapping()) {
      patchHtml(nodes.bannerContainer, "");
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
    if (state.flash) {
      banners.push(notice(state.flash.message, state.flash.tone));
      state.flash = null;
    }
    patchHtml(nodes.bannerContainer, banners.join(""));
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
    if (state.refreshing) {
      patchClassName(nodes.refreshStateChip, "status-pill tone-info refresh-state-chip is-loading");
      patchText(nodes.refreshStateChip, "刷新中");
      if (nodes.refreshStateChip) {
        nodes.refreshStateChip.setAttribute("aria-label", "正在刷新页面数据");
      }
      if (nodes.refreshButton) {
        nodes.refreshButton.disabled = true;
      }
      patchText(nodes.lastRefreshLabel, "正在刷新最新状态…");
      return;
    }
    if (!state.lastRefreshAt) {
      patchClassName(nodes.refreshStateChip, "status-pill tone-neutral refresh-state-chip");
      patchText(nodes.refreshStateChip, "待刷新");
      if (nodes.refreshStateChip) {
        nodes.refreshStateChip.setAttribute("aria-label", "页面尚未完成首次刷新");
      }
      if (nodes.refreshButton) {
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
    if (nodes.refreshButton) {
      nodes.refreshButton.disabled = false;
    }
    patchText(nodes.lastRefreshLabel, `最近刷新：${formatMaybeTimestamp(state.lastRefreshAt)}（${formatRelativeAge(state.lastRefreshAt)}）`);
  }

  function syncRefreshInteractivity() {
    syncRefreshDisabledButtons({
      roots: currentRefreshInteractivityRoots(),
      refreshing: state.refreshing,
      pendingPanels: state.pendingPanels,
      reason: "当前区域正在刷新，请等待刷新完成后再操作。",
      panelReason: "该卡片数据还在补充加载，请稍候再操作。",
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

    if (view === "strategy" || view === "execution" || view === "risk" || view === "exitExecution" || view === "replay" || view === "aiAnalysis" || view === "aiConfig" || view === "admin") {
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
  };
}
