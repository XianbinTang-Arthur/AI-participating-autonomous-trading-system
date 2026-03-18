import { fetchPanels, requestJson } from "./modules/api-client.js";
import { actionButton, kvList, notice, pill, statusCard, surfaceCard } from "./modules/components.js";
import {
  booleanWord,
  emptyState,
  escapeHtml,
  formatMaybeTimestamp,
  formatNumber,
  formatRelativeAge,
  formatSigned,
  listOrDash,
  rawJson,
} from "./modules/formatters.js";
import { AUTO_REFRESH_MS, CORE_SPECS, DEFAULT_PAGE_LIMITS, PAGE_LOAD_STEP, createState, viewSpecs } from "./modules/store.js";
import { localizeError, readableState, toneForOrderStatus, toneForRuntimeState } from "./modules/terms.js";
import {
  decisionDrawerRows,
  fillDrawerRows,
  fillSceneSummary,
  orderDrawerRows,
  orderSceneSummary,
} from "./modules/trade-display.js";
import { renderAdminView } from "./modules/views/admin-view.js";
import { renderExecutionSections, renderExecutionView } from "./modules/views/execution-view.js";
import { renderOverviewView } from "./modules/views/overview-view.js";
import { renderRiskSections, renderRiskView } from "./modules/views/risk-view.js";
import { renderStrategySections, renderStrategyView } from "./modules/views/strategy-view.js";

const state = createState();
state.activeView = document.body.dataset.view || "overview";

const viewTabs = Array.from(document.querySelectorAll(".workspace-tab"));
const viewSections = Array.from(document.querySelectorAll(".workspace-view"));

const nodes = {
  statusRibbon: document.getElementById("statusRibbon"),
  bannerContainer: document.getElementById("bannerContainer"),
  sessionIdentityValue: document.getElementById("sessionIdentityValue"),
  sessionRoleValue: document.getElementById("sessionRoleValue"),
  authStateChip: document.getElementById("authStateChip"),
  logoutButton: document.getElementById("logoutButton"),
  refreshButton: document.getElementById("refreshButton"),
  reconcileButton: document.getElementById("reconcileButton"),
  rebaselineButton: document.getElementById("rebaselineButton"),
  resumeButton: document.getElementById("resumeButton"),
  haltButton: document.getElementById("haltButton"),
  autoRefreshToggle: document.getElementById("autoRefreshToggle"),
  actionPermissionHint: document.getElementById("actionPermissionHint"),
  lastRefreshLabel: document.getElementById("lastRefreshLabel"),
  overviewContent: document.getElementById("overviewContent"),
  strategyContent: document.getElementById("strategyContent"),
  executionContent: document.getElementById("executionContent"),
  riskContent: document.getElementById("riskContent"),
  adminContent: document.getElementById("adminContent"),
  detailDrawer: document.getElementById("detailDrawer"),
  drawerBackdrop: document.getElementById("drawerBackdrop"),
  closeDrawerButton: document.getElementById("closeDrawerButton"),
  drawerEyebrow: document.getElementById("drawerEyebrow"),
  drawerTitle: document.getElementById("drawerTitle"),
  drawerSummary: document.getElementById("drawerSummary"),
  drawerBody: document.getElementById("drawerBody"),
};
const renderCache = new WeakMap();

init();

function init() {
  bindEvents();
  renderShell();
  void refreshDashboard();
}

function bindEvents() {
  viewTabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      setActiveView(tab.dataset.view || "overview");
    });
  });

  nodes.refreshButton.addEventListener("click", () => void refreshDashboard({ manual: true }));
  nodes.reconcileButton.addEventListener("click", () =>
    void runAction("/reconciliation/validate", { reason: "ui_manual_validate" }, "已发起人工对账检查。")
  );
  nodes.rebaselineButton.addEventListener("click", () =>
    void runDangerousAction({
      path: "/system/rebaseline",
      body: { reason: "ui_manual_rebaseline" },
      successMessage: "基线已重建。请继续观察恢复状态和对账状态。",
      confirmMessage: "现在接受当前账户状态为新的可信基线吗？这会影响后续恢复判断。",
    })
  );
  nodes.resumeButton.addEventListener("click", () =>
    void runAction("/system/resume", { reason: "ui_manual_resume" }, "已发起恢复请求，系统将重新评估是否具备交易条件。")
  );
  nodes.haltButton.addEventListener("click", () =>
    void runDangerousAction({
      path: "/system/halt",
      body: { reason: "ui_manual_halt" },
      successMessage: "系统已进入安全暂停状态。",
      confirmMessage: "确认暂停系统并阻断继续交易吗？",
    })
  );
  nodes.logoutButton.addEventListener("click", () => void logoutOperator());
  nodes.autoRefreshToggle.addEventListener("change", () => {
    if (nodes.autoRefreshToggle.checked) {
      scheduleRefresh();
    } else {
      cancelScheduledRefresh();
    }
  });
  nodes.closeDrawerButton.addEventListener("click", closeDrawer);
  nodes.drawerBackdrop.addEventListener("click", closeDrawer);

  document.addEventListener("click", (event) => {
    const target = event.target instanceof HTMLElement ? event.target.closest("[data-action]") : null;
    if (!target) return;
    const action = target.dataset.action;
    const value = target.dataset.value || "";
    if (!action) return;
    void dispatchAction(action, value);
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.id !== "operatorCreateForm") return;
    event.preventDefault();
    void createOperatorUser();
  });
}

async function refreshDashboard({ manual = false } = {}) {
  if (state.refreshing) return;
  state.refreshing = true;
  renderShell();
  try {
    const specs = dedupeSpecs([...CORE_SPECS, ...viewSpecs(state.activeView, state)]);
    const results = await fetchPanels(specs);
    for (const [key, result] of Object.entries(results)) {
      state.data[key] = result.data;
      state.errors[key] = result.error;
    }
    if (shouldRedirectToLogin()) {
      window.location.replace("/login");
      return;
    }
    state.lastRefreshAt = new Date();
    if (manual) {
      state.flash = { tone: "info", message: "页面数据已刷新。" };
    }
  } finally {
    state.refreshing = false;
    renderShell();
    scheduleRefresh();
  }
}

function renderShell() {
  renderSessionSummary();
  renderStatusRibbon();
  renderBanners();
  renderActiveView();
  updateActionAccess();
  updateRefreshLabel();
}

function renderSessionSummary() {
  const session = state.data.session || {};
  patchText(nodes.sessionIdentityValue, session.identity || "未登录");
  patchText(nodes.sessionRoleValue, `当前身份：${readableState(session.role || "anonymous")}`);
  patchClassName(nodes.authStateChip, `status-pill tone-${session.authenticated ? "positive" : "neutral"}`);
  patchText(nodes.authStateChip, session.authenticated ? "已登录" : "未登录");
}

function renderStatusRibbon() {
  const health = state.data.health || {};
  const mode = state.data.mode || {};
  const runtime = state.data.runtime || {};
  const recovery = state.data.systemRecovery?.recovery || {};
  const account = state.data.accountState || {};
  const reconciliation = state.data.reconciliationLatest?.reconciliation || null;
  const portfolio = state.data.portfolio?.portfolio || {};

  patchHtml(nodes.statusRibbon, [
    statusCard({
      title: "运行状态",
      value: readableState(health.runtime_state || health.overall_status),
      meta: `系统总览：${readableState(health.overall_status)}`,
      pills: [pill(`运行档位 ${readableState(mode.operating_state || "unknown")}`, toneForRuntimeState(health.runtime_state || health.overall_status))],
    }),
    statusCard({
      title: "当前能否下单",
      value: recovery.safe_to_trade ? "允许继续自动交易" : "当前禁止继续交易",
      meta: recovery.safe_to_trade ? "没有发现会阻止发单的风险项" : localizedRecoveryReasons(),
      pills: [
        pill(`手动暂停 ${booleanWord(health.halted)}`, health.halted ? "danger" : "outline"),
        pill(`等待人工确认 ${booleanWord(recovery.review_required)}`, recovery.review_required ? "warning" : "outline"),
      ],
    }),
    statusCard({
      title: "交易通道",
      value: readableState(runtime.environment_capabilities?.exchange_submission_target || mode.execution_route || "unknown"),
      meta: `执行线路 ${runtime.environment_capabilities?.execution_route || mode.execution_route || "-"}`,
      pills: [
        pill(`允许向交易所报单 ${booleanWord(mode.exchange_submit_allowed)}`, mode.exchange_submit_allowed ? "positive" : "outline"),
      ],
    }),
    statusCard({
      title: "账户同步",
      value: account.ready ? "正常" : "异常",
      meta: account.ready ? "余额、仓位和挂单快照可用" : listOrDash(account.blockers),
      pills: [
        pill(`交易所连接 ${booleanWord(account.connected)}`, account.connected ? "positive" : "warning"),
        pill(`快照新鲜 ${booleanWord(account.fresh)}`, account.fresh ? "positive" : "warning"),
      ],
    }),
    statusCard({
      title: "账实核对",
      value: readableState(reconciliation?.severity || "unknown"),
      meta: reconciliation?.reconciliation_id || "还没有新的对账结论",
      pills: [
        pill(`要求停机 ${booleanWord(reconciliation?.halt_required)}`, reconciliation?.halt_required ? "danger" : "outline"),
      ],
    }),
    statusCard({
      title: "账户权益",
      value: formatNumber(portfolio.total_equity),
      meta: `已实现 ${formatSigned(portfolio.realized_pnl)} / 持仓浮盈亏 ${formatSigned(portfolio.unrealized_pnl)}`,
      pills: [
        pill(`总敞口 ${formatNumber(portfolio.gross_exposure)}`, "info"),
      ],
    }),
  ].join(""));
}

function renderBanners() {
  const banners = [];
  const recovery = state.data.systemRecovery?.recovery || {};
  const blockers = state.data.blockers?.blockers || [];
  const controlsMessage = controlPermissionMessage();

  if (!recovery.safe_to_trade) {
    banners.push(notice(`当前不能继续自动交易：${localizedRecoveryReasons()}`, "warning"));
  }
  if (blockers.length > 0) {
    banners.push(notice(`当前主要阻断：${localizeError(blockers[0].blocker)}`, blockers[0].affects_execution ? "danger" : "warning"));
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

function renderActiveView() {
  const viewData = {
    ...state.data,
    errors: state.errors,
    uiHints: {
      recoveryReasonsText: localizedRecoveryReasons(),
      controlPermissionMessage: controlPermissionMessage(),
    },
  };
  if (state.activeView === "overview" && nodes.overviewContent) {
    patchHtml(nodes.overviewContent, renderOverviewView(viewData));
    return;
  }
  if (state.activeView === "strategy") {
    patchRenderedSections(renderStrategySections(viewData), () => nodes.strategyContent, () => renderStrategyView(viewData));
    return;
  }
  if (state.activeView === "execution") {
    patchRenderedSections(renderExecutionSections(viewData), () => nodes.executionContent, () => renderExecutionView(viewData));
    return;
  }
  if (state.activeView === "risk") {
    patchRenderedSections(renderRiskSections(viewData), () => nodes.riskContent, () => renderRiskView(viewData));
    return;
  }
  if (state.activeView === "admin" && nodes.adminContent) {
    patchHtml(nodes.adminContent, renderAdminView(viewData));
  }
}

function updateActionAccess() {
  const actionButtons = [nodes.reconcileButton, nodes.rebaselineButton, nodes.resumeButton, nodes.haltButton];
  if (!hasResolvedAuthContext()) {
    actionButtons.forEach((node) => {
      if (!node) return;
      node.disabled = true;
      node.title = "正在确认当前账号权限…";
    });
    if (nodes.logoutButton) {
      nodes.logoutButton.disabled = false;
      nodes.logoutButton.title = "";
    }
    patchText(nodes.actionPermissionHint, "正在确认当前账号权限…");
    return;
  }

  const canWrite = operatorCanWrite();
  const buttons = [...actionButtons, nodes.logoutButton];
  const disabledReason = controlPermissionMessage() || "当前账号没有执行该操作的权限。";
  buttons.forEach((node) => {
    if (!node) return;
    node.disabled = !canWrite && node !== nodes.logoutButton;
    if (node !== nodes.logoutButton) {
      node.title = !canWrite ? disabledReason : "";
    }
  });
  patchText(nodes.actionPermissionHint, canWrite ? "当前账号可以执行人工操作。" : disabledReason);
}

function updateRefreshLabel() {
  if (state.refreshing) {
    patchText(nodes.lastRefreshLabel, "正在刷新最新状态…");
    return;
  }
  if (!state.lastRefreshAt) {
    patchText(nodes.lastRefreshLabel, "尚未刷新");
    return;
  }
  patchText(nodes.lastRefreshLabel, `最近刷新：${formatMaybeTimestamp(state.lastRefreshAt)}（${formatRelativeAge(state.lastRefreshAt)}）`);
}

function setActiveView(view) {
  state.activeView = view;
  viewTabs.forEach((tab) => tab.classList.toggle("is-active", tab.dataset.view === view));
  viewSections.forEach((section) => section.classList.toggle("is-active", section.dataset.view === view));
  renderActiveView();
  void refreshDashboard();
}

async function runAction(path, body, successMessage) {
  try {
    await requestJson(path, { method: "POST", body });
    state.flash = { tone: "info", message: successMessage };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function runDangerousAction({ path, body, successMessage, confirmMessage }) {
  if (!window.confirm(confirmMessage)) return;
  await runAction(path, body, successMessage);
}

async function logoutOperator() {
  try {
    await requestJson("/auth/logout", { method: "POST" });
    window.location.assign("/login");
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function dispatchAction(action, value) {
  if (action === "inspect-decision") return inspectDecision(value);
  if (action === "inspect-order") return inspectOrder(value);
  if (action === "inspect-fill") return inspectFill(value);
  if (action === "inspect-reconciliation") return inspectReconciliation(value);
  if (action === "resolve-stuck-order") return resolveStuckOrder(value);
  if (action === "evaluate-strategy-profile") return evaluateStrategyProfile();
  if (action === "accept-strategy-profile-now") return acceptStrategyProfileRecommendation(value, "manual_now");
  if (action === "stage-strategy-profile") return acceptStrategyProfileRecommendation(value, "stage_only");
  if (action === "reject-strategy-profile") return rejectStrategyProfileRecommendation(value);
  if (action === "activate-pending-strategy-profile") return activatePendingStrategyProfile();
  if (action === "rollback-strategy-profile") return rollbackStrategyProfile();
  if (action === "load-more-orders") return adjustPageLimit("recentOrders", PAGE_LOAD_STEP);
  if (action === "collapse-orders") return resetPageLimit("recentOrders");
  if (action === "load-more-fills") return adjustPageLimit("recentFills", PAGE_LOAD_STEP);
  if (action === "collapse-fills") return resetPageLimit("recentFills");
  if (action === "load-more-decisions") return adjustPageLimit("recentDecisions", PAGE_LOAD_STEP);
  if (action === "collapse-decisions") return resetPageLimit("recentDecisions");
  if (action === "load-more-reconciliations") return adjustPageLimit("recentReconciliations", PAGE_LOAD_STEP);
  if (action === "collapse-reconciliations") return resetPageLimit("recentReconciliations");
  if (action === "load-more-blocker-history") return adjustPageLimit("blockerHistory", PAGE_LOAD_STEP);
  if (action === "collapse-blocker-history") return resetPageLimit("blockerHistory");
  if (action === "load-more-replay-validations") return adjustPageLimit("replayValidations", PAGE_LOAD_STEP);
  if (action === "collapse-replay-validations") return resetPageLimit("replayValidations");
  if (action === "toggle-user") return toggleOperatorUser(value);
  if (action === "change-user-role") return updateOperatorUserRole(value);
  if (action === "reset-user-password") return resetOperatorPassword(value);
  if (action === "delete-user") return deleteOperatorUser(value);
}

async function inspectDecision(decisionId) {
  if (!decisionId) return;
  try {
    const detail = await requestJson(`/decision/${encodeURIComponent(decisionId)}`);
    openDrawer({
      eyebrow: "决策详情",
      title: detail.decision_id || "决策详情",
      summary: strategySummary(detail),
      body: [
        surfaceCard({
          title: "决策摘要",
          content: kvList(decisionDrawerRows(detail, describeDecisionIntent)),
        }),
        surfaceCard({
          title: "交易解释",
          content: `<div class="callout"><p>${escapeHtml(strategySummary(detail))}</p></div>`,
        }),
        surfaceCard({
          title: "原始记录",
          content: rawJson(detail),
        }),
      ].join(""),
    });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function inspectOrder(orderId) {
  if (!orderId) return;
  try {
    const detail = await requestJson(`/orders/${encodeURIComponent(orderId)}`);
    const order = detail.order || {};
    const fills = detail.fills || [];
    openDrawer({
      eyebrow: `${orderSceneSummary(order)}详情`,
      title: order.client_order_id || `${orderSceneSummary(order)}详情`,
      summary: `这笔${orderSceneSummary(order)}当前状态：${readableState(order.status)}。${fills.length ? ` 已关联 ${fills.length} 笔成交。` : " 目前还没有关联成交。"} `,
      body: [
        surfaceCard({
          title: `${orderSceneSummary(order)}摘要`,
          content: kvList([
            ...orderDrawerRows(order),
            ["最后更新时间", formatMaybeTimestamp(order.last_update_ts || order.created_at), formatRelativeAge(order.last_update_ts || order.created_at)],
          ]),
        }),
        surfaceCard({
          title: "关联成交",
          content: fills.length
            ? kvList(
                fills.map((fill) => [
                  fill.fill_id || "-",
                  `${formatNumber(fill.fill_qty)} @ ${formatNumber(fill.fill_price)}`,
                  `${readableState(fill.side)} | 手续费 ${formatNumber(fill.fee_amount)} ${fill.fee_currency || ""}`,
                ])
              )
            : emptyState("这笔委托暂时还没有对应成交。"),
        }),
        surfaceCard({
          title: "原始记录",
          content: rawJson(detail),
        }),
      ].join(""),
    });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function inspectFill(fillId) {
  if (!fillId) return;
  try {
    const detail = await requestJson(`/fills/${encodeURIComponent(fillId)}`);
    const fill = detail.fill || {};
    openDrawer({
      eyebrow: `${fillSceneSummary(fill)}详情`,
      title: fill.fill_id || `${fillSceneSummary(fill)}详情`,
      summary: `这笔${fillSceneSummary(fill)}是 ${readableState(fill.side)} ${formatNumber(fill.fill_qty)}，成交价 ${formatNumber(fill.fill_price)}。`,
      body: [
        surfaceCard({
          title: `${fillSceneSummary(fill)}摘要`,
          content: kvList([
            ...fillDrawerRows(fill),
            ["落库时间", formatMaybeTimestamp(fill.ingestion_timestamp), formatRelativeAge(fill.ingestion_timestamp)],
          ]),
        }),
        surfaceCard({
          title: "原始记录",
          content: rawJson(detail),
        }),
      ].join(""),
    });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function inspectReconciliation(reconciliationId) {
  if (!reconciliationId) return;
  try {
    const detail = await requestJson(`/reconciliation/${encodeURIComponent(reconciliationId)}`);
    const reconciliation = detail.reconciliation || {};
    openDrawer({
      eyebrow: "对账详情",
      title: reconciliation.reconciliation_id || "对账详情",
      summary: `这次对账结论是 ${readableState(reconciliation.severity)}。${reconciliation.halt_required ? " 系统已要求暂停自动交易。" : ""}`,
      body: [
        surfaceCard({
          title: "核对摘要",
          content: kvList([
            ["核对级别", readableState(reconciliation.severity), reconciliation.exchange_comparison_enabled ? "已比对交易所" : "仅校验本地记录"],
            ["是否要求停机", booleanWord(reconciliation.halt_required), booleanWord(reconciliation.review_required)],
            ["差异原因", listOrDash(detail.mismatch_summary?.mismatch_reasons), listOrDash(detail.mismatch_summary?.mismatch_categories)],
            ["建议动作", localizeError(detail.mismatch_summary?.recommended_operator_action || "-"), listOrDash(detail.mismatch_summary?.safety_impacts)],
            ["核对时间", formatMaybeTimestamp(reconciliation.as_of_ts), formatRelativeAge(reconciliation.as_of_ts)],
          ]),
        }),
        surfaceCard({
          title: "原始记录",
          content: rawJson(detail),
        }),
      ].join(""),
    });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function resolveStuckOrder(orderId) {
  if (!orderId) return;
  await runDangerousAction({
    path: `/orders/${encodeURIComponent(orderId)}/resolve-stuck-submission`,
    body: { reason: "ui_resolve_stuck_submission" },
    successMessage: "已提交卡单处理请求。",
    confirmMessage: "确认对这笔长时间卡住的委托执行人工恢复处理吗？",
  });
}

async function adjustPageLimit(key, delta) {
  const current = Number(state.pageLimits?.[key] || DEFAULT_PAGE_LIMITS[key] || 0);
  state.pageLimits[key] = current + delta;
  await refreshDashboard();
}

async function resetPageLimit(key) {
  state.pageLimits[key] = DEFAULT_PAGE_LIMITS[key] || state.pageLimits[key];
  await refreshDashboard();
}

async function evaluateStrategyProfile() {
  try {
    const result = await requestJson("/strategy-profiles/auto-tuning/evaluate-now", { method: "POST" });
    const profileId = result?.recommendation?.recommended_profile_id || "未知档位";
    const autoApplied = Boolean(result?.auto_activation);
    state.flash = {
      tone: autoApplied ? "info" : "warning",
      message: autoApplied
        ? `已生成建议并自动切换到更保守的档位：${profileId}。`
        : `已生成新的策略档位建议：${profileId}。如需生效，请在设置页继续审批。`,
    };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function acceptStrategyProfileRecommendation(recommendationId, activationMode) {
  if (!recommendationId) return;
  const confirmMessage =
    activationMode === "stage_only"
      ? "确认把这条 AI 建议转为待审批档位吗？它不会立刻覆盖当前运行中的参数。"
      : "确认立即采纳这条 AI 建议吗？当前策略档位会立刻切换。";
  if (!window.confirm(confirmMessage)) return;
  try {
    const result = await requestJson(`/strategy-profiles/recommendations/${encodeURIComponent(recommendationId)}/accept`, {
      method: "POST",
      body: {
        reason: activationMode === "stage_only" ? "ui_stage_strategy_profile" : "ui_accept_strategy_profile_now",
        activation_mode: activationMode,
      },
    });
    state.flash = {
      tone: "info",
      message:
        activationMode === "stage_only"
          ? `已把建议转成待审批档位：${result?.activation?.pending_profile_id || "未知档位"}。`
          : `已切换到档位：${result?.active_revision?.profile_label || result?.active_revision?.profile_id || "未知档位"}。`,
    };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function rejectStrategyProfileRecommendation(recommendationId) {
  if (!recommendationId) return;
  const reasonDetail = window.prompt("请输入拒绝原因，便于后续复盘。", "保留当前档位，暂不采纳这条建议。");
  if (reasonDetail === null) return;
  try {
    await requestJson(`/strategy-profiles/recommendations/${encodeURIComponent(recommendationId)}/reject`, {
      method: "POST",
      body: {
        reason_code: "operator_rejected_strategy_profile_recommendation",
        reason_detail: reasonDetail,
      },
    });
    state.flash = { tone: "info", message: "已拒绝这条策略档位建议。" };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function activatePendingStrategyProfile() {
  if (!window.confirm("确认激活当前待审批档位吗？这会立刻替换当前生效的策略门槛。")) return;
  try {
    const result = await requestJson("/strategy-profiles/pending/activate", {
      method: "POST",
      body: { reason: "ui_activate_pending_strategy_profile" },
    });
    state.flash = {
      tone: "info",
      message: `已激活待审批档位：${result?.active_revision?.profile_label || result?.active_revision?.profile_id || "未知档位"}。`,
    };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function rollbackStrategyProfile() {
  if (!window.confirm("确认回滚到上一个稳定档位吗？这会撤销最近一次策略档位切换。")) return;
  try {
    const result = await requestJson("/strategy-profiles/rollback", {
      method: "POST",
      body: { reason: "ui_rollback_strategy_profile" },
    });
    state.flash = {
      tone: "warning",
      message: `已回滚到档位：${result?.active_revision?.profile_label || result?.active_revision?.profile_id || "未知档位"}。`,
    };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function createOperatorUser() {
  const username = document.getElementById("operatorCreateUsername")?.value.trim();
  const password = document.getElementById("operatorCreatePassword")?.value;
  const role = document.getElementById("operatorCreateRole")?.value;
  const enabled = document.getElementById("operatorCreateEnabled")?.value === "true";
  if (!username || !password || !role) {
    state.flash = { tone: "warning", message: "请先完整填写用户名、初始密码和角色。" };
    renderBanners();
    return;
  }
  try {
    await requestJson("/auth/users", { method: "POST", body: { username, password, role, enabled } });
    state.flash = { tone: "info", message: `已创建账号 ${username}。` };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function toggleOperatorUser(username) {
  const user = findOperatorUser(username);
  if (!user) return;
  try {
    await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
      method: "PATCH",
      body: { enabled: !user.enabled },
    });
    state.flash = { tone: "info", message: `${username} 已${user.enabled ? "停用" : "启用"}。` };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function updateOperatorUserRole(username) {
  const user = findOperatorUser(username);
  if (!user) return;
  const nextRole = window.prompt("请输入新角色：viewer / operator / admin", user.role || "viewer");
  if (!nextRole || nextRole === user.role) return;
  try {
    await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
      method: "PATCH",
      body: { role: nextRole },
    });
    state.flash = { tone: "info", message: `${username} 的角色已更新为 ${nextRole}。` };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function resetOperatorPassword(username) {
  const password = window.prompt(`请输入 ${username} 的新密码`);
  if (!password) return;
  try {
    await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
      method: "PATCH",
      body: { password },
    });
    state.flash = { tone: "info", message: `${username} 的密码已重置。` };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

async function deleteOperatorUser(username) {
  if (!window.confirm(`确认删除账户 ${username} 吗？`)) return;
  try {
    await requestJson(`/auth/users/${encodeURIComponent(username)}`, { method: "DELETE" });
    state.flash = { tone: "info", message: `${username} 已删除。` };
    await refreshDashboard({ manual: true });
  } catch (error) {
    state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
    renderBanners();
  }
}

function findOperatorUser(username) {
  return (state.data.operatorUsers?.users || []).find((item) => item.username === username) || null;
}

function operatorCanWrite() {
  const session = state.data.session || {};
  const runtimeAuth = state.data.runtime?.operator_auth || {};
  const authProviders = state.data.authProviders || {};
  if (!authProviders.auth_enabled) return Boolean(runtimeAuth.unsafe_write_without_auth);
  return session.role === "operator" || session.role === "admin" || session.identity === "api_key_write";
}

function hasResolvedAuthContext() {
  return Object.prototype.hasOwnProperty.call(state.data, "authProviders") && Object.prototype.hasOwnProperty.call(state.data, "session");
}

function shouldRedirectToLogin() {
  const authProviders = state.data.authProviders || {};
  const session = state.data.session || {};
  return Boolean(authProviders.auth_enabled) && !session.authenticated;
}

function controlPermissionMessage() {
  if (!hasResolvedAuthContext()) {
    return "";
  }
  const session = state.data.session || {};
  const runtimeAuth = state.data.runtime?.operator_auth || {};
  const authProviders = state.data.authProviders || {};
  if (!authProviders.auth_enabled) {
    return runtimeAuth.unsafe_write_without_auth
      ? ""
      : "当前环境不允许未认证写入，所以人工操作按钮会置灰。";
  }
  if (!session.authenticated) {
    return "当前未登录，所以恢复交易等人工操作按钮会置灰。请先用 operator 或 admin 账号登录。";
  }
  if (session.role === "viewer") {
    return "当前账号是只读 viewer，只能查看，不能执行恢复交易等人工操作。请切换为 operator 或 admin。";
  }
  return "";
}

function effectiveRecoveryReasons() {
  const recovery = state.data.systemRecovery?.recovery || {};
  const explicitReasons = Array.isArray(recovery.resume_blocked_reasons) ? recovery.resume_blocked_reasons.filter(Boolean) : [];
  if (explicitReasons.length > 0) {
    return explicitReasons;
  }
  const blockers = Array.isArray(state.data.blockers?.blockers)
    ? state.data.blockers.blockers
        .filter((item) => item && item.blocker && item.affects_execution !== false)
        .map((item) => item.blocker)
    : [];
  if (blockers.length > 0) {
    return Array.from(new Set(blockers));
  }
  if (state.data.health?.halted) {
    return ["kill_switch_active"];
  }
  return [];
}

function localizedRecoveryReasons() {
  return listOrDash(effectiveRecoveryReasons().map((item) => localizeError(item)));
}

function scheduleRefresh() {
  cancelScheduledRefresh();
  if (!nodes.autoRefreshToggle.checked) return;
  state.refreshTimer = window.setTimeout(() => void refreshDashboard(), AUTO_REFRESH_MS);
}

function cancelScheduledRefresh() {
  if (!state.refreshTimer) return;
  window.clearTimeout(state.refreshTimer);
  state.refreshTimer = null;
}

function openDrawer({ eyebrow, title, summary, body }) {
  nodes.drawerEyebrow.textContent = eyebrow;
  nodes.drawerTitle.textContent = title;
  nodes.drawerSummary.textContent = summary;
  nodes.drawerBody.innerHTML = body;
  nodes.detailDrawer.classList.add("is-open");
  nodes.detailDrawer.setAttribute("aria-hidden", "false");
  nodes.drawerBackdrop.hidden = false;
}

function closeDrawer() {
  nodes.detailDrawer.classList.remove("is-open");
  nodes.detailDrawer.setAttribute("aria-hidden", "true");
  nodes.drawerBackdrop.hidden = true;
}

function dedupeSpecs(specs) {
  const seen = new Set();
  return specs.filter(([key]) => {
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
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

function strategySummary(detail) {
  const policy = detail.policy_decision || {};
  const risk = detail.risk_decision || {};
  if (!detail.decision_id) return "最近没有新的策略详情。";
  return `系统当前对 ${detail.decision_context?.symbol || "当前标的"} 的交易结论是 ${describeDecisionIntent(detail)}。` +
    `${policy.execution_allowed ? "策略门禁已通过，" : "策略门禁未通过，"}` +
    `${risk.approved ? "风控也允许继续执行。" : `风控仍在拦截：${listOrDash(risk.rejection_reasons)}。`}`;
}

function describeDecisionIntent(detail) {
  const target = detail.position_target || {};
  const rawIntent = String(target.position_intent || "hold").toLowerCase();
  const currentQty = Number(target.current_position_qty ?? detail.decision_context?.current_position_qty ?? 0);
  const targetQty = Number(target.target_position_qty ?? 0);
  const openOrders = Array.isArray(detail.decision_context?.current_open_orders) ? detail.decision_context.current_open_orders : [];
  if (rawIntent === "hold" && currentQty === 0 && targetQty === 0 && openOrders.length === 0) {
    return "继续观望";
  }
  return readableState(rawIntent);
}

window.refreshDashboard = refreshDashboard;
