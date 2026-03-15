const AUTO_REFRESH_MS = 5000;

const state = {
  activeView: "overview",
  refreshing: false,
  refreshTimer: null,
  lastRefreshAt: null,
  flashMessage: null,
  panelErrors: {},
  data: {},
};

const nodes = mapNodes({
  runtimeStateChip: "runtimeStateChip",
  operatingStateChip: "operatingStateChip",
  executionRouteChip: "executionRouteChip",
  submitPostureChip: "submitPostureChip",
  authStateChip: "authStateChip",
  sessionIdentityValue: "sessionIdentityValue",
  sessionRoleValue: "sessionRoleValue",
  sessionCardIdentity: "sessionCardIdentity",
  sessionCardRole: "sessionCardRole",
  sessionCardSource: "sessionCardSource",
  logoutButton: "logoutButton",
  refreshButton: "refreshButton",
  reconcileButton: "reconcileButton",
  rebaselineButton: "rebaselineButton",
  resumeButton: "resumeButton",
  haltButton: "haltButton",
  actionPermissionHint: "actionPermissionHint",
  autoRefreshToggle: "autoRefreshToggle",
  lastRefreshLabel: "lastRefreshLabel",
  bannerContainer: "bannerContainer",
  stripOverallValue: "stripOverallValue",
  stripOverallMeta: "stripOverallMeta",
  stripModeValue: "stripModeValue",
  stripModeMeta: "stripModeMeta",
  stripExecutionValue: "stripExecutionValue",
  stripExecutionMeta: "stripExecutionMeta",
  stripRecoveryValue: "stripRecoveryValue",
  stripRecoveryMeta: "stripRecoveryMeta",
  stripFreshnessValue: "stripFreshnessValue",
  stripFreshnessMeta: "stripFreshnessMeta",
  stripEquityValue: "stripEquityValue",
  stripEquityMeta: "stripEquityMeta",
  overviewDecisionSpotlight: "overviewDecisionSpotlight",
  overviewPosture: "overviewPosture",
  overviewPortfolio: "overviewPortfolio",
  overviewBlockers: "overviewBlockers",
  overviewBlockerStamp: "overviewBlockerStamp",
  overviewRecovery: "overviewRecovery",
  overviewMetrics: "overviewMetrics",
  overviewTimeline: "overviewTimeline",
  decisionSpotlight: "decisionSpotlight",
  decisionTable: "decisionTable",
  decisionLookupInput: "decisionLookupInput",
  loadDecisionButton: "loadDecisionButton",
  executionSpotlight: "executionSpotlight",
  orderTable: "orderTable",
  fillTable: "fillTable",
  executionErrorsPanel: "executionErrorsPanel",
  orderLookupInput: "orderLookupInput",
  loadOrderButton: "loadOrderButton",
  fillLookupInput: "fillLookupInput",
  loadFillButton: "loadFillButton",
  diagnosticReconciliation: "diagnosticReconciliation",
  diagnosticRecovery: "diagnosticRecovery",
  diagnosticReplay: "diagnosticReplay",
  diagnosticAccount: "diagnosticAccount",
  diagnosticBlockers: "diagnosticBlockers",
  diagnosticMetrics: "diagnosticMetrics",
  inspectSystemButton: "inspectSystemButton",
  inspectPortfolioButton: "inspectPortfolioButton",
  inspectLatestDecisionButton: "inspectLatestDecisionButton",
  inspectLatestOrderButton: "inspectLatestOrderButton",
  inspectLatestFillButton: "inspectLatestFillButton",
  inspectReconciliationButton: "inspectReconciliationButton",
  inspectRecoveryButton: "inspectRecoveryButton",
  inspectRecoveryDiagnosticsButton: "inspectRecoveryDiagnosticsButton",
  inspectRuntimeButton: "inspectRuntimeButton",
  detailDrawer: "detailDrawer",
  drawerBackdrop: "drawerBackdrop",
  closeDrawerButton: "closeDrawerButton",
  drawerTitle: "drawerTitle",
  drawerSummary: "drawerSummary",
  drawerBody: "drawerBody",
});

const viewTabs = Array.from(document.querySelectorAll(".workspace-tab"));
const views = Array.from(document.querySelectorAll(".workspace-view"));

init();

function init() {
  bindEvents();
  renderEmptyState();
  updateAuthStateChip();
  void refreshDashboard();
}

function bindEvents() {
  nodes.logoutButton.addEventListener("click", () => void logoutOperator());
  nodes.refreshButton.addEventListener("click", () => void refreshDashboard({ manual: true }));
  nodes.reconcileButton.addEventListener("click", () => void runAction("/reconciliation/validate", { reason: "ui_manual_validate" }, "Reconciliation validation requested."));
  nodes.rebaselineButton.addEventListener("click", () => void runDangerousAction({
    path: "/system/rebaseline",
    body: { reason: "ui_manual_rebaseline" },
    successMessage: "Current exchange state accepted as a new baseline. Runtime remains halted until resume succeeds.",
    confirmMessage: "Accept current exchange account state as the new trusted baseline? This is an operator repair action and does not auto-resume trading.",
  }));
  nodes.resumeButton.addEventListener("click", () => void runAction("/system/resume", { reason: "ui_manual_resume" }, "Resume requested. Runtime readiness was re-evaluated."));
  nodes.haltButton.addEventListener("click", () => void runDangerousAction({
    path: "/system/halt",
    body: { reason: "ui_manual_halt" },
    successMessage: "System halted.",
    confirmMessage: "Safe halt the runtime and stop execution eligibility?",
  }));
  nodes.autoRefreshToggle.addEventListener("change", () => {
    if (nodes.autoRefreshToggle.checked) {
      scheduleRefresh();
    } else {
      cancelScheduledRefresh();
    }
  });
  nodes.inspectSystemButton.addEventListener("click", inspectSystemDetail);
  nodes.inspectPortfolioButton.addEventListener("click", inspectPortfolioDetail);
  nodes.inspectLatestDecisionButton.addEventListener("click", () => void inspectLatestDecision());
  nodes.inspectLatestOrderButton.addEventListener("click", () => void inspectLatestOrder());
  nodes.inspectLatestFillButton.addEventListener("click", () => void inspectLatestFill());
  nodes.inspectReconciliationButton.addEventListener("click", () => void inspectLatestReconciliation());
  nodes.inspectRecoveryButton.addEventListener("click", inspectRecoveryDetail);
  nodes.inspectRecoveryDiagnosticsButton.addEventListener("click", inspectRecoveryDetail);
  nodes.inspectRuntimeButton.addEventListener("click", inspectRuntimeDetail);
  nodes.loadDecisionButton.addEventListener("click", () => void inspectDecision(nodes.decisionLookupInput.value.trim(), { manual: true }));
  nodes.loadOrderButton.addEventListener("click", () => void inspectOrder(nodes.orderLookupInput.value.trim(), { manual: true }));
  nodes.loadFillButton.addEventListener("click", () => void inspectFill(nodes.fillLookupInput.value.trim(), { manual: true }));
  bindEnter(nodes.decisionLookupInput, () => void inspectDecision(nodes.decisionLookupInput.value.trim(), { manual: true }));
  bindEnter(nodes.orderLookupInput, () => void inspectOrder(nodes.orderLookupInput.value.trim(), { manual: true }));
  bindEnter(nodes.fillLookupInput, () => void inspectFill(nodes.fillLookupInput.value.trim(), { manual: true }));
  nodes.drawerBackdrop.addEventListener("click", closeDrawer);
  nodes.closeDrawerButton.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeDrawer();
    }
  });
  viewTabs.forEach((tab) => {
    tab.addEventListener("click", () => setActiveView(tab.dataset.view || "overview"));
  });
  document.body.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const decisionButton = target.closest("[data-inspect-decision]");
    if (decisionButton instanceof HTMLElement) {
      void inspectDecision(decisionButton.dataset.inspectDecision || "", { manual: true });
      return;
    }
    const orderButton = target.closest("[data-inspect-order]");
    if (orderButton instanceof HTMLElement) {
      void inspectOrder(orderButton.dataset.inspectOrder || "", { manual: true });
      return;
    }
    const fillButton = target.closest("[data-inspect-fill]");
    if (fillButton instanceof HTMLElement) {
      void inspectFill(fillButton.dataset.inspectFill || "", { manual: true });
    }
  });
}

function bindEnter(node, handler) {
  node.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") {
      return;
    }
    event.preventDefault();
    handler();
  });
}

function mapNodes(map) {
  return Object.fromEntries(Object.entries(map).map(([key, id]) => [key, document.getElementById(id)]));
}

function setActiveView(viewName) {
  state.activeView = viewName;
  viewTabs.forEach((tab) => tab.classList.toggle("is-active", tab.dataset.view === viewName));
  views.forEach((view) => view.classList.toggle("is-active", view.dataset.view === viewName));
}

async function refreshDashboard({ manual = false } = {}) {
  if (state.refreshing) {
    return;
  }
  state.refreshing = true;
  setActionButtonsBusy(true);
  cancelScheduledRefresh();

  const specs = [
    ["session", "/auth/session"],
    ["health", "/system/health"],
    ["mode", "/system/mode"],
    ["runtime", "/system/runtime"],
    ["blockers", "/system/blockers"],
    ["metrics", "/system/metrics"],
    ["systemRecovery", "/system/recovery"],
    ["portfolio", "/portfolio/latest"],
    ["latestDecision", "/decision/latest"],
    ["recentDecisions", "/decision/recent?limit=8"],
    ["executionLatest", "/execution/latest"],
    ["recentOrders", "/orders/recent?limit=8"],
    ["recentFills", "/fills/recent?limit=8"],
    ["executionErrors", "/execution/errors"],
    ["reconciliationLatest", "/reconciliation/latest"],
    ["replayStatus", "/replay/status"],
    ["accountState", "/account/state"],
  ];

  const results = await Promise.all(specs.map(([key, path]) => fetchPanel(key, path)));
  results.forEach((result) => {
    if (result.ok) {
      state.data[result.key] = result.data;
      delete state.panelErrors[result.key];
    } else {
      state.panelErrors[result.key] = result.error;
    }
  });

  state.lastRefreshAt = new Date();
  renderDashboard({ manual });
  state.refreshing = false;
  setActionButtonsBusy(false);
  scheduleRefresh();
}

async function fetchPanel(key, path) {
  try {
    return { ok: true, key, data: await requestJson(path) };
  } catch (error) {
    return { ok: false, key, error: normalizeError(error, `Failed to load ${path}`) };
  }
}

async function requestJson(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  const data = text ? safeJsonParse(text) : null;
  if (!response.ok) {
    const detail = typeof data === "object" && data !== null && "detail" in data ? data.detail : text || response.statusText;
    const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    error.status = response.status;
    error.payload = data;
    if (response.status === 401 && !options.allowUnauthorized) {
      window.location.assign("/login");
    }
    throw error;
  }
  return data;
}

function safeJsonParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function normalizeError(error, fallbackMessage) {
  return {
    status: typeof error?.status === "number" ? error.status : null,
    message: error?.message || fallbackMessage,
  };
}

async function logoutOperator() {
  try {
    await requestJson("/auth/logout", { method: "POST" });
  } catch (_error) {
    // Clear browser state even if the server-side session is already gone.
  }
  window.location.assign("/login");
}

async function runAction(path, body, successMessage) {
  try {
    await requestJson(path, { method: "POST", body });
    flash(successMessage, "info");
    await refreshDashboard({ manual: true });
  } catch (error) {
    flash(`Action failed: ${normalizeError(error, `Failed to call ${path}`).message}`, "danger");
    renderDashboard({ manual: true });
  }
}

async function runDangerousAction({ path, body, successMessage, confirmMessage }) {
  if (!window.confirm(confirmMessage)) {
    return;
  }
  await runAction(path, body, successMessage);
}

function flash(message, tone = "info") {
  state.flashMessage = { message, tone };
}

function setActionButtonsBusy(busy) {
  nodes.refreshButton.disabled = busy;
  nodes.refreshButton.textContent = busy ? "Refreshing..." : "Refresh";
  updateActionAccess();
}

function scheduleRefresh() {
  cancelScheduledRefresh();
  if (!nodes.autoRefreshToggle.checked) {
    return;
  }
  state.refreshTimer = window.setTimeout(() => void refreshDashboard(), AUTO_REFRESH_MS);
}

function cancelScheduledRefresh() {
  if (state.refreshTimer !== null) {
    window.clearTimeout(state.refreshTimer);
    state.refreshTimer = null;
  }
}

function renderDashboard({ manual = false } = {}) {
  updateAuthStateChip();
  updateActionAccess();
  renderHeaderBadges();
  renderRuntimeStrip();
  renderAlerts();
  renderOverview();
  renderDecisions();
  renderExecution();
  renderDiagnostics();
  nodes.lastRefreshLabel.textContent = state.lastRefreshAt
    ? `Last refresh ${formatDateTime(state.lastRefreshAt)}${manual ? " | manual" : ""}`
    : "Not refreshed yet";
}

function updateActionAccess() {
  const canWrite = operatorCanWrite();
  nodes.reconcileButton.disabled = state.refreshing || !canWrite;
  nodes.rebaselineButton.disabled = state.refreshing || !canWrite;
  nodes.resumeButton.disabled = state.refreshing || !canWrite;
  nodes.haltButton.disabled = state.refreshing || !canWrite;
  nodes.actionPermissionHint.textContent = permissionHint(canWrite);
}

function renderEmptyState() {
  nodes.stripOverallValue.textContent = "-";
  nodes.stripOverallMeta.textContent = "-";
  nodes.stripModeValue.textContent = "-";
  nodes.stripModeMeta.textContent = "-";
  nodes.stripExecutionValue.textContent = "-";
  nodes.stripExecutionMeta.textContent = "-";
  nodes.stripRecoveryValue.textContent = "-";
  nodes.stripRecoveryMeta.textContent = "-";
  nodes.stripFreshnessValue.textContent = "-";
  nodes.stripFreshnessMeta.textContent = "-";
  nodes.stripEquityValue.textContent = "-";
  nodes.stripEquityMeta.textContent = "-";
  nodes.overviewDecisionSpotlight.innerHTML = emptyState("Waiting for runtime data.");
  nodes.overviewPosture.innerHTML = emptyState("Waiting for system posture.");
  nodes.overviewPortfolio.innerHTML = emptyState("Waiting for portfolio state.");
  nodes.overviewBlockers.innerHTML = emptyState("No blocker data yet.");
  nodes.overviewRecovery.innerHTML = emptyState("No recovery posture yet.");
  nodes.overviewMetrics.innerHTML = emptyState("No metrics yet.");
  nodes.overviewTimeline.innerHTML = emptyState("No recent runtime activity.");
  nodes.decisionSpotlight.innerHTML = emptyState("No latest decision yet.");
  nodes.decisionTable.innerHTML = emptyState("No recent decisions yet.");
  nodes.executionSpotlight.innerHTML = emptyState("No execution posture yet.");
  nodes.orderTable.innerHTML = emptyState("No recent orders.");
  nodes.fillTable.innerHTML = emptyState("No recent fills.");
  nodes.executionErrorsPanel.innerHTML = emptyState("No recent execution errors.");
  nodes.diagnosticReconciliation.innerHTML = emptyState("No reconciliation report yet.");
  nodes.diagnosticRecovery.innerHTML = emptyState("No recovery state yet.");
  nodes.diagnosticReplay.innerHTML = emptyState("No replay validation yet.");
  nodes.diagnosticAccount.innerHTML = emptyState("No account state yet.");
  nodes.diagnosticBlockers.innerHTML = emptyState("No blocker history yet.");
  nodes.diagnosticMetrics.innerHTML = emptyState("No runtime metrics yet.");
  setDrawerContent("Operator Detail", `<div class="empty-state">Use a table action or lookup to inspect detail.</div>`, `<div class="empty-state">Raw detail is available once an item is selected.</div>`);
}

function updateAuthStateChip() {
  const session = state.data.session || {};
  const denied = Object.values(state.panelErrors).some((error) => error?.status === 401 || error?.status === 403);
  let text = "anonymous";
  let tone = "outline";
  if (denied) {
    text = "access denied";
    tone = "danger";
  } else if (session.authenticated) {
    text = session.auth_source === "session" ? "session active" : String(session.auth_source || "authenticated");
    tone = "success";
  } else if (session.auth_enabled) {
    text = "login required";
    tone = "warning";
  } else {
    text = "local access";
    tone = "neutral";
  }
  setStatusChip(nodes.authStateChip, text, tone);
  nodes.sessionIdentityValue.textContent = session.identity || "anonymous session";
  nodes.sessionRoleValue.textContent = `role: ${readableState(session.role || "anonymous")}`;
  nodes.sessionCardIdentity.textContent = session.identity || "-";
  nodes.sessionCardRole.textContent = readableState(session.role || "anonymous");
  nodes.sessionCardSource.textContent = readableState(session.auth_source || "anonymous");
  nodes.logoutButton.disabled = !session.authenticated;
}

function operatorCanWrite() {
  const session = state.data.session || {};
  const operatorAuth = state.data.runtime?.operator_auth || {};
  if (session.auth_enabled) {
    return session.role === "operator" || session.role === "admin";
  }
  return Boolean(operatorAuth.unsafe_write_without_auth);
}

function permissionHint(canWrite) {
  const session = state.data.session || {};
  const operatorAuth = state.data.runtime?.operator_auth || {};
  if (session.auth_enabled) {
    if (canWrite) {
      return `Control actions enabled for ${readableState(session.role || "operator")}.`;
    }
    return "Read-only session. Control actions require operator or admin access.";
  }
  if (operatorAuth.unsafe_write_without_auth) {
    return "Local development mode: write actions are enabled without auth.";
  }
  return "Write actions are locked until operator auth is configured or unsafe local write is explicitly enabled.";
}

function renderHeaderBadges() {
  const health = state.data.health || {};
  const mode = state.data.mode || {};
  setStatusChip(nodes.runtimeStateChip, readableState(health.runtime_state || health.overall_status), toneForRuntimeState(health.runtime_state));
  setStatusChip(nodes.operatingStateChip, readableMode(mode.operating_state || "unknown"), mode.execution_blocked ? "warning" : "outline");
  setStatusChip(nodes.executionRouteChip, mode.execution_route || mode.execution_backend || "unknown", mode.exchange_submit_allowed ? "info" : "outline");
  setStatusChip(nodes.submitPostureChip, mode.exchange_submit_allowed ? "submit enabled" : "submit blocked", mode.exchange_submit_allowed ? "success" : "danger");
}

function renderRuntimeStrip() {
  const health = state.data.health || {};
  const mode = state.data.mode || {};
  const portfolio = state.data.portfolio?.portfolio || null;
  const freshness = health.freshness || {};
  const account = state.data.accountState || {};
  const recovery = recoveryData();

  nodes.stripOverallValue.textContent = readableState(health.runtime_state || health.overall_status);
  nodes.stripOverallMeta.textContent = health.execution_blocked ? listOrDash(health.submit_blocked_reasons || health.blockers?.map((item) => item.blocker)) : "No active blockers";

  nodes.stripModeValue.textContent = readableMode(mode.operating_state || mode.mode || "unknown");
  nodes.stripModeMeta.textContent = `${mode.execution_backend || "-"} | ${mode.execution_route || "-"}`;

  nodes.stripExecutionValue.textContent = mode.exchange_submit_allowed ? "Armed" : "Guarded";
  nodes.stripExecutionMeta.textContent = mode.exchange_submit_allowed ? "Submission path is open" : listOrDash(mode.submit_blocked_reasons);

  nodes.stripRecoveryValue.textContent = readableState(recovery.recovery_state);
  nodes.stripRecoveryMeta.textContent = recoverySummaryLine(recovery);

  nodes.stripFreshnessValue.textContent = freshnessSummary(freshness);
  nodes.stripFreshnessMeta.textContent = `market ${booleanWord(freshness.market_fresh)} | account ${booleanWord(account.fresh)} | recon ${booleanWord(freshness.reconciliation_fresh)}`;

  nodes.stripEquityValue.textContent = portfolio ? formatNumber(portfolio.total_equity) : "-";
  nodes.stripEquityMeta.textContent = portfolio ? `gross ${formatNumber(portfolio.gross_exposure)} | net ${formatSigned(portfolio.net_exposure)}` : "No portfolio snapshot";
}

function renderAlerts() {
  const health = state.data.health || {};
  const blockers = state.data.blockers?.blockers || [];
  const recovery = recoveryData();
  const banners = [];
  if (state.flashMessage) {
    banners.push(state.flashMessage);
    state.flashMessage = null;
  }
  if (health.runtime_state === "halted") {
    banners.push({ tone: "danger", message: "Runtime is halted. Execution is stopped until resume succeeds." });
  } else if (health.runtime_state === "blocked") {
    const lead = blockers[0];
    banners.push({
      tone: "warning",
      message: lead ? `Execution is blocked by ${lead.blocker}. ${lead.recommended_action}` : "Execution is blocked by an active safety condition.",
    });
  } else if (recovery.review_required) {
    banners.push({
      tone: "warning",
      message: "Recovery review is required. Accept the current exchange state as a new baseline before resuming automation.",
    });
  } else if (recovery.recovery_state === "rebaseline_completed" && !recovery.safe_to_trade) {
    banners.push({
      tone: "info",
      message: "A new baseline is ready, but the runtime remains halted until resume succeeds.",
    });
  } else if (health.runtime_state === "degraded") {
    banners.push({ tone: "info", message: "Runtime is degraded. Review freshness, blockers, and reconciliation before trusting the next action." });
  }

  Object.entries(state.panelErrors).slice(0, 4).forEach(([panel, error]) => {
    banners.push({
      tone: error.status === 401 || error.status === 403 ? "danger" : "warning",
      message: `${panel} failed: ${error.message}`,
    });
  });

  nodes.bannerContainer.innerHTML = banners.map((item) => `<div class="alert alert-${item.tone}">${escapeHtml(item.message)}</div>`).join("");
}

function renderOverview() {
  const health = state.data.health || {};
  const mode = state.data.mode || {};
  const portfolio = state.data.portfolio?.portfolio || null;
  const latestDecision = state.data.latestDecision || {};
  const blockers = state.data.blockers?.blockers || [];
  const metrics = state.data.metrics || {};
  const recovery = recoveryData();
  const baseline = state.data.runtime?.baseline_takeover || {};

  nodes.overviewDecisionSpotlight.innerHTML = renderDecisionHero(latestDecision, state.data.executionLatest);
  nodes.overviewPosture.innerHTML = renderFactGrid([
    ["Overall", readableState(health.overall_status)],
    ["Runtime", readableState(health.runtime_state)],
    ["Mode", readableMode(mode.mode)],
    ["Operating State", readableMode(mode.operating_state)],
    ["Execution Backend", mode.execution_backend || "-"],
    ["Submit Allowed", booleanWord(mode.exchange_submit_allowed)],
    ["Halted", booleanWord(health.halted)],
    ["Account Ready", booleanWord(state.data.accountState?.ready)],
  ]);

  nodes.overviewPortfolio.innerHTML = renderFactGrid([
    ["Total Equity", portfolio ? formatNumber(portfolio.total_equity) : "-"],
    ["Realized PnL", portfolio ? formatSigned(portfolio.realized_pnl) : "-"],
    ["Unrealized PnL", portfolio ? formatSigned(portfolio.unrealized_pnl) : "-"],
    ["Gross Exposure", portfolio ? formatNumber(portfolio.gross_exposure) : "-"],
    ["Net Exposure", portfolio ? formatSigned(portfolio.net_exposure) : "-"],
    ["Positions", portfolio ? String((portfolio.positions || []).length) : "-"],
    ["USDT Balance", portfolio ? formatNumber(portfolio.balances?.USDT) : "-"],
    ["Updated", state.data.portfolio?.latest_update_timestamp ? formatDateTime(state.data.portfolio.latest_update_timestamp) : "-"],
  ]);

  nodes.overviewBlockerStamp.textContent = blockers.length ? `${blockers.length} active` : "clear";
  nodes.overviewBlockers.innerHTML = renderSignalCards(
    blockers.map((item) => ({
      title: item.blocker,
      subtitle: `${item.subsystem} | ${item.submit_only ? "submit path" : "execution path"}`,
      tone: item.affects_execution ? "warning" : "info",
      detail: item.recommended_action,
    })),
    "No current blockers."
  );

  nodes.overviewRecovery.innerHTML = renderFactGrid([
    ["Recovery State", readableState(recovery.recovery_state)],
    ["Safe To Trade", booleanWord(recovery.safe_to_trade)],
    ["Resume Eligible", booleanWord(recovery.resume_eligible)],
    ["Review Required", booleanWord(recovery.review_required)],
    ["Rebaseline Available", booleanWord(recovery.rebaseline_available)],
    ["Resume Blockers", listOrDash(recovery.resume_blocked_reasons)],
    ["Baseline Status", readableState(baseline.status)],
    ["Baseline Kind", readableState(baseline.baseline_kind)],
    ["Baseline Imported", booleanWord(baseline.baseline_imported)],
    ["Baseline Imported At", formatMaybeTimestamp(baseline.baseline_imported_at)],
    ["Last Rebaseline", formatMaybeTimestamp(baseline.last_rebaseline_at)],
    ["Open Orders At Baseline", formatNumber(baseline.open_order_count)],
  ]);

  nodes.overviewMetrics.innerHTML = renderFactGrid([
    ["Decision Cycles", formatNumber(metrics.decision_cycle_count)],
    ["Order Intents", formatNumber(metrics.order_intent_count)],
    ["Open Orders", formatNumber(metrics.current_open_order_count)],
    ["Fills", formatNumber(metrics.fill_count)],
    ["Rejections", formatNumber(metrics.rejection_count)],
    ["Recon Mismatches", formatNumber(metrics.reconciliation_mismatch_count)],
    ["Recent Exec Errors", formatNumber((metrics.recent_execution_errors || []).length)],
    ["Account Fresh", booleanWord(state.data.accountState?.fresh)],
  ]);

  nodes.overviewTimeline.innerHTML = renderTimeline(buildTimeline());
}

function renderDecisions() {
  const latestDecision = state.data.latestDecision || {};
  const recentDecisions = state.data.recentDecisions?.decisions || [];

  nodes.decisionSpotlight.innerHTML = renderDecisionInvestigation(latestDecision);
  nodes.decisionTable.innerHTML = renderTable(
    ["Decision", "Time", "Target Delta", "Policy", "Risk", "Execution", "Action"],
    recentDecisions.map((item) => ([
      `<div><strong>${escapeHtml(item.symbol || "-")}</strong><div class="mono">${escapeHtml(item.decision_id || "-")}</div></div>`,
      escapeHtml(formatMaybeTimestamp(item.decision_time)),
      escapeHtml(formatSigned(item.target_delta_qty)),
      miniBadge(booleanShort(item.policy_result), item.policy_result ? "success" : "danger"),
      miniBadge(booleanShort(item.risk_result), item.risk_result ? "success" : "danger"),
      `<div class="table-meta">${escapeHtml(decisionOutcomeLabel(item))}</div>`,
      item.decision_id ? `<button class="table-button" data-inspect-decision="${escapeHtml(item.decision_id)}">Inspect</button>` : "",
    ])),
    "No recent decisions available."
  );
}

function renderExecution() {
  const execution = state.data.executionLatest || {};
  const mode = execution.mode || state.data.mode || {};
  const readiness = execution.execution || {};
  const latestOrder = execution.latest_order || null;
  const latestFill = execution.latest_fill || null;
  const latestReconciliation = execution.latest_reconciliation || null;
  const recentOrders = state.data.recentOrders?.orders || [];
  const recentFills = state.data.recentFills?.fills || [];
  const errors = state.data.executionErrors?.errors || [];

  nodes.executionSpotlight.innerHTML = `
    <div class="overview-hero">
      <div class="hero-header">
        <div>
          <p class="hero-id">${escapeHtml(mode.execution_route || mode.execution_backend || "unknown")}</p>
          <h3 class="hero-title">${escapeHtml(mode.exchange_submit_allowed ? "Submit Enabled" : "Guarded")}</h3>
        </div>
        <div class="runtime-badges">
          ${miniBadge(mode.okx_simulated_trading ? "simulated" : "paper", mode.okx_simulated_trading ? "info" : "outline")}
          ${miniBadge(mode.guarded_execution_dry_run ? "dry run" : "submit path", mode.guarded_execution_dry_run ? "warning" : "success")}
        </div>
      </div>
      <p class="hero-copy">${escapeHtml(mode.exchange_submit_allowed ? "All execution gates are clear." : listOrDash(mode.submit_blocked_reasons))}</p>
      ${renderFactGrid([
        ["Execution Ready", booleanWord(readiness.ready)],
        ["Latest Order", latestOrder ? `${latestOrder.status} | ${latestOrder.client_order_id}` : "-"],
        ["Latest Fill", latestFill ? `${formatNumber(latestFill.fill_qty)} @ ${formatNumber(latestFill.fill_price)}` : "-"],
        ["Reconciliation", latestReconciliation ? latestReconciliation.severity : "-"],
        ["Recovery", execution.recovery?.safe_startup ? "safe" : "review required"],
        ["Open Orders", formatNumber(state.data.metrics?.current_open_order_count)],
      ])}
    </div>
  `;

  nodes.orderTable.innerHTML = renderTable(
    ["Order", "Decision", "Side", "Quantity", "Venue", "Status", "Updated", "Action"],
    recentOrders.map((order) => ([
      `<div><strong>${escapeHtml(order.symbol || "-")}</strong><div class="mono">${escapeHtml(order.client_order_id || "-")}</div></div>`,
      escapeHtml(order.decision_id || "-"),
      escapeHtml(order.side || "-"),
      escapeHtml(formatNumber(order.requested_qty)),
      escapeHtml(order.venue || order.submission_mode || "-"),
      miniBadge(order.status || "-", toneForOrderStatus(order.status)),
      escapeHtml(formatMaybeTimestamp(order.last_update_ts || order.created_at)),
      order.client_order_id ? `<button class="table-button" data-inspect-order="${escapeHtml(order.client_order_id)}">Inspect</button>` : "",
    ])),
    "No recent orders available."
  );

  nodes.fillTable.innerHTML = renderTable(
    ["Fill", "Decision", "Side", "Quantity", "Price", "Fee", "Venue", "Ingested", "Action"],
    recentFills.map((fill) => ([
      `<div><strong>${escapeHtml(fill.symbol || "-")}</strong><div class="mono">${escapeHtml(fill.fill_id || "-")}</div></div>`,
      escapeHtml(fill.decision_id || "-"),
      escapeHtml(fill.side || "-"),
      escapeHtml(formatNumber(fill.fill_qty)),
      escapeHtml(formatNumber(fill.fill_price)),
      escapeHtml(formatNumber(fill.fee_amount)),
      escapeHtml(fill.venue || "-"),
      escapeHtml(formatMaybeTimestamp(fill.ingestion_timestamp)),
      fill.fill_id ? `<button class="table-button" data-inspect-fill="${escapeHtml(fill.fill_id)}">Inspect</button>` : "",
    ])),
    "No recent fills available."
  );

  nodes.executionErrorsPanel.innerHTML = renderSignalCards(
    errors.slice(0, 6).map((item) => ({
      title: item.message || item.status || "execution issue",
      subtitle: `${item.subsystem || "execution"} | ${formatMaybeTimestamp(item.timestamp)}`,
      tone: item.severity === "error" ? "danger" : "warning",
      detail: [item.decision_id, item.order_id].filter(Boolean).join(" | ") || "no linked identifiers",
    })),
    "No recent execution errors."
  );
}

function renderDiagnostics() {
  const reconciliation = state.data.reconciliationLatest?.reconciliation || null;
  const mismatchSummary = state.data.reconciliationLatest?.mismatch_summary || null;
  const latestValidation = state.data.reconciliationLatest?.latest_validation || null;
  const replay = state.data.replayStatus || {};
  const account = state.data.accountState || {};
  const blockerHistory = state.data.blockers?.recent_history || [];
  const runtime = state.data.runtime || {};
  const metrics = state.data.metrics || {};
  const recovery = recoveryData();
  const baseline = runtime.baseline_takeover || account.baseline_takeover || {};

  nodes.diagnosticReconciliation.innerHTML = reconciliation ? `
    <div class="overview-hero">
      <div class="hero-header">
        <div>
          <p class="hero-id">${escapeHtml(reconciliation.reconciliation_id)}</p>
          <h3 class="hero-title">${escapeHtml(reconciliation.severity)}</h3>
        </div>
        <div class="runtime-badges">
          ${miniBadge(reconciliation.halt_required ? "halt required" : "safe", reconciliation.halt_required ? "danger" : "success")}
          ${miniBadge(reconciliation.exchange_comparison_enabled ? "exchange aware" : "local only", reconciliation.exchange_comparison_enabled ? "info" : "outline")}
        </div>
      </div>
      <p class="hero-copy">${escapeHtml(listOrDash(mismatchSummary?.mismatch_reasons) || "-")}</p>
      ${renderFactGrid([
        ["Mismatch Reasons", listOrDash(mismatchSummary?.mismatch_reasons)],
        ["Mismatch Categories", listOrDash(mismatchSummary?.mismatch_categories)],
        ["Safety Impacts", listOrDash(mismatchSummary?.safety_impacts)],
        ["Review Required", booleanWord(reconciliation.review_required)],
        ["Recommended Action", mismatchSummary?.recommended_operator_action || "-"],
        ["Last Validation", latestValidation?.validated_at ? formatDateTime(latestValidation.validated_at) : "-"],
        ["Exchange Comparison", booleanWord(reconciliation.exchange_comparison_enabled)],
      ])}
    </div>
  ` : emptyState("No reconciliation report yet.");

  nodes.diagnosticRecovery.innerHTML = renderFactGrid([
    ["Recovery State", readableState(recovery.recovery_state)],
    ["Safe To Trade", booleanWord(recovery.safe_to_trade)],
    ["Resume Eligible", booleanWord(recovery.resume_eligible)],
    ["Review Required", booleanWord(recovery.review_required)],
    ["Rebaseline Available", booleanWord(recovery.rebaseline_available)],
    ["Resume Blockers", listOrDash(recovery.resume_blocked_reasons)],
    ["Baseline Status", readableState(baseline.status)],
    ["Baseline Kind", readableState(baseline.baseline_kind)],
    ["Baseline Source", baseline.baseline_source || "-"],
    ["Baseline Imported At", formatMaybeTimestamp(baseline.baseline_imported_at)],
    ["Baseline Event", baseline.event_ref || "-"],
    ["Last Rebaseline Event", baseline.last_rebaseline_event_ref || "-"],
  ]);

  nodes.diagnosticReplay.innerHTML = renderFactGrid([
    ["Replay Supported", booleanWord(replay.supported)],
    ["Replay Healthy", booleanWord(replay.healthy)],
    ["Last Decision", replay.last_validation?.decision_id || "-"],
    ["Replayed Events", formatNumber(replay.last_validation?.replayed_event_count)],
    ["Divergences", formatNumber(replay.last_validation?.divergence_count)],
    ["Baseline Switches", formatNumber(replay.last_validation?.baseline_switch_count)],
    ["Validated", replay.last_validation?.validated_at ? formatDateTime(replay.last_validation.validated_at) : "-"],
  ]);

  nodes.diagnosticAccount.innerHTML = renderFactGrid([
    ["Backend", account.backend || "-"],
    ["Read Enabled", booleanWord(account.read_enabled)],
    ["Connected", booleanWord(account.connected)],
    ["Fresh", booleanWord(account.fresh)],
    ["Ready", booleanWord(account.ready)],
    ["Blocking Reason", account.current_blocking_reason || "-"],
    ["Last Refresh", account.last_refresh_timestamp ? formatDateTime(account.last_refresh_timestamp) : "-"],
    ["Recovery", readableState(account.recovery?.recovery_state)],
    ["Baseline Status", readableState(account.baseline_takeover?.status)],
  ]);

  nodes.diagnosticBlockers.innerHTML = renderTimeline(
    blockerHistory.slice().reverse().map((item) => ({
      title: "Blocker snapshot",
      subtitle: `${item.runtime_state || "-"} | ${item.operating_state || "-"}`,
      timestamp: item.created_at,
      tone: item.execution_blocked ? "warning" : "info",
      detail: item.blockers?.length ? item.blockers.map((blocker) => blocker.blocker).join(", ") : "clear",
    })),
    "No blocker history available."
  );

  nodes.diagnosticMetrics.innerHTML = renderFactGrid([
    ["Decision Cycles", formatNumber(metrics.decision_cycle_count)],
    ["Order Intents", formatNumber(metrics.order_intent_count)],
    ["Fills", formatNumber(metrics.fill_count)],
    ["Rejections", formatNumber(metrics.rejection_count)],
    ["Open Orders", formatNumber(metrics.current_open_order_count)],
    ["Runtime Uptime", formatDuration(runtime.uptime_seconds)],
    ["Last Decision", runtime.last_decision_timestamp ? formatDateTime(runtime.last_decision_timestamp) : "-"],
    ["Last Reconciliation", runtime.last_reconciliation_timestamp ? formatDateTime(runtime.last_reconciliation_timestamp) : "-"],
  ]);
}

async function inspectLatestDecision() {
  const decisionId = state.data.latestDecision?.decision_id;
  if (!decisionId) {
    flash("No latest decision is available yet.", "warning");
    renderAlerts();
    return;
  }
  await inspectDecision(decisionId, { manual: true });
}

async function inspectLatestOrder() {
  const orderId = state.data.executionLatest?.latest_order?.client_order_id;
  if (!orderId) {
    flash("No latest order is available yet.", "warning");
    renderAlerts();
    return;
  }
  await inspectOrder(orderId, { manual: true });
}

async function inspectLatestFill() {
  const fillId = state.data.executionLatest?.latest_fill?.fill_id;
  if (!fillId) {
    flash("No latest fill is available yet.", "warning");
    renderAlerts();
    return;
  }
  await inspectFill(fillId, { manual: true });
}

async function inspectLatestReconciliation() {
  const reconciliationId = state.data.reconciliationLatest?.reconciliation?.reconciliation_id;
  if (!reconciliationId) {
    flash("No reconciliation report is available yet.", "warning");
    renderAlerts();
    return;
  }
  try {
    const detail = await requestJson(`/reconciliation/${encodeURIComponent(reconciliationId)}`);
    showStructuredDetail({
      title: "Reconciliation Detail",
      summary: [
        ["Reconciliation ID", detail.reconciliation?.reconciliation_id],
        ["Severity", detail.reconciliation?.severity],
        ["Halt Required", booleanWord(detail.reconciliation?.halt_required)],
        ["Exchange Aware", booleanWord(detail.reconciliation?.exchange_comparison_enabled)],
      ],
      sections: [
        detailCard("Mismatch Summary", detail.mismatch_summary || {}),
        detailCard("Reconciliation", detail.reconciliation || {}),
      ],
    });
  } catch (error) {
    flash(`Reconciliation lookup failed: ${normalizeError(error, "Reconciliation lookup failed").message}`, "danger");
    renderAlerts();
  }
}

function inspectSystemDetail() {
  showStructuredDetail({
    title: "System Detail",
    summary: [
      ["Runtime State", state.data.health?.runtime_state],
      ["Operating State", state.data.mode?.operating_state],
      ["Execution Blocked", booleanWord(state.data.health?.execution_blocked)],
      ["Submit Blocked", booleanWord(state.data.health?.submit_blocked)],
    ],
    sections: [
      detailCard("Health", state.data.health || {}),
      detailCard("Mode", state.data.mode || {}),
      detailCard("Recovery", state.data.systemRecovery || {}),
      detailCard("Blockers", state.data.blockers || {}),
      detailCard("Account", state.data.accountState || {}),
    ],
  });
}

function inspectRecoveryDetail() {
  const recovery = recoveryData();
  const baseline = state.data.runtime?.baseline_takeover || state.data.accountState?.baseline_takeover || {};
  showStructuredDetail({
    title: "Recovery Detail",
    summary: [
      ["Recovery State", readableState(recovery.recovery_state)],
      ["Safe To Trade", booleanWord(recovery.safe_to_trade)],
      ["Resume Eligible", booleanWord(recovery.resume_eligible)],
      ["Review Required", booleanWord(recovery.review_required)],
      ["Rebaseline Available", booleanWord(recovery.rebaseline_available)],
      ["Baseline Status", readableState(baseline.status)],
    ],
    sections: [
      detailCard("System Recovery", state.data.systemRecovery || {}),
      detailCard("Runtime Recovery", state.data.runtime?.recovery || {}),
      detailCard("Baseline Takeover", baseline || {}),
      detailCard("Account State", state.data.accountState || {}),
    ],
  });
}

function inspectRuntimeDetail() {
  showStructuredDetail({
    title: "Runtime Detail",
    summary: [
      ["Profile", state.data.mode?.config_profile],
      ["Execution Route", state.data.mode?.execution_route],
      ["Startup", state.data.runtime?.startup_timestamp],
      ["Uptime", formatDuration(state.data.runtime?.uptime_seconds)],
    ],
    sections: [
      detailCard("Runtime", state.data.runtime || {}),
      detailCard("Recovery", state.data.systemRecovery || {}),
      detailCard("Metrics", state.data.metrics || {}),
      detailCard("Replay", state.data.replayStatus || {}),
    ],
  });
}

function inspectPortfolioDetail() {
  showStructuredDetail({
    title: "Portfolio Detail",
    summary: [
      ["Total Equity", formatNumber(state.data.portfolio?.portfolio?.total_equity)],
      ["Gross Exposure", formatNumber(state.data.portfolio?.portfolio?.gross_exposure)],
      ["Net Exposure", formatSigned(state.data.portfolio?.portfolio?.net_exposure)],
      ["Updated", formatMaybeTimestamp(state.data.portfolio?.latest_update_timestamp)],
    ],
    sections: [
      detailCard("Portfolio Snapshot", state.data.portfolio?.portfolio || {}),
    ],
  });
}

async function inspectDecision(decisionId, { manual = false } = {}) {
  if (!decisionId) {
    if (manual) {
      flash("Enter a valid decision ID.", "warning");
      renderAlerts();
    }
    return;
  }
  try {
    const detail = await requestJson(`/decision/${encodeURIComponent(decisionId)}`);
    nodes.decisionLookupInput.value = decisionId;
    showStructuredDetail({
      title: "Decision Detail",
      summary: [
        ["Decision ID", detail.decision_id],
        ["Symbol", detail.decision_context?.symbol],
        ["Timeframe", detail.decision_context?.timeframe],
        ["Target Delta", formatSigned(detail.position_target?.delta_position_qty)],
        ["Policy Allowed", booleanWord(detail.policy_decision?.execution_allowed)],
        ["Risk Approved", booleanWord(detail.risk_decision?.approved)],
      ],
      sections: [
        detailCard("Decision Context", detail.decision_context || {}),
        detailCard("Baseline Assessment", detail.baseline_assessment || {}),
        detailCard("AI Assessment", detail.ai_assessment || {}),
        detailCard("Target / Policy / Risk", {
          position_target: detail.position_target || null,
          policy_decision: detail.policy_decision || null,
          risk_decision: detail.risk_decision || null,
        }),
        detailCard("Execution Chain", {
          execution_plan: detail.execution_plan || null,
          order_intents: detail.order_intents || [],
          order_updates: detail.order_updates || [],
          fills: detail.fills || [],
          portfolio_snapshot: detail.portfolio_snapshot || null,
          reconciliations: detail.reconciliations || [],
        }),
        detailCard("Audit", detail.audit || {}),
      ],
    });
  } catch (error) {
    flash(`Decision lookup failed: ${normalizeError(error, "Decision lookup failed").message}`, "danger");
    renderAlerts();
  }
}

async function inspectOrder(orderId, { manual = false } = {}) {
  if (!orderId) {
    if (manual) {
      flash("Enter a valid order ID.", "warning");
      renderAlerts();
    }
    return;
  }
  try {
    const detail = await requestJson(`/orders/${encodeURIComponent(orderId)}`);
    nodes.orderLookupInput.value = orderId;
    showStructuredDetail({
      title: "Order Detail",
      summary: [
        ["Order ID", detail.order?.client_order_id],
        ["Decision ID", detail.order?.decision_id],
        ["Symbol", detail.order?.symbol],
        ["Status", detail.order?.status],
        ["Venue", detail.order?.venue],
        ["Requested Qty", formatNumber(detail.order?.requested_qty)],
      ],
      sections: [
        detailCard("Order", detail.order || {}),
        detailCard("Linked Fills", detail.fills || []),
      ],
    });
  } catch (error) {
    flash(`Order lookup failed: ${normalizeError(error, "Order lookup failed").message}`, "danger");
    renderAlerts();
  }
}

async function inspectFill(fillId, { manual = false } = {}) {
  if (!fillId) {
    if (manual) {
      flash("Enter a valid fill ID.", "warning");
      renderAlerts();
    }
    return;
  }
  try {
    const detail = await requestJson(`/fills/${encodeURIComponent(fillId)}`);
    nodes.fillLookupInput.value = fillId;
    showStructuredDetail({
      title: "Fill Detail",
      summary: [
        ["Fill ID", detail.fill?.fill_id],
        ["Decision ID", detail.fill?.decision_id],
        ["Symbol", detail.fill?.symbol],
        ["Side", detail.fill?.side],
        ["Quantity", formatNumber(detail.fill?.fill_qty)],
        ["Price", formatNumber(detail.fill?.fill_price)],
      ],
      sections: [
        detailCard("Fill", detail.fill || {}),
      ],
    });
  } catch (error) {
    flash(`Fill lookup failed: ${normalizeError(error, "Fill lookup failed").message}`, "danger");
    renderAlerts();
  }
}

function renderDecisionHero(latestDecision, executionLatest) {
  const target = latestDecision.position_target || null;
  const baseline = latestDecision.baseline_assessment || null;
  const policy = latestDecision.policy_decision || null;
  const risk = latestDecision.risk_decision || null;
  const summary = latestDecision.summary || null;
  const latestOrder = executionLatest?.latest_order || null;
  const latestFill = executionLatest?.latest_fill || null;
  const outcome = decisionOutcomeLabel(summary);

  if (!latestDecision.decision_id) {
    return emptyState("No latest decision is available yet.");
  }

  return `
    <div class="overview-hero">
      <div class="hero-header">
        <div>
          <p class="hero-id">${escapeHtml(latestDecision.decision_id)}</p>
          <h3 class="hero-title">${escapeHtml(latestDecision.decision_context?.symbol || "-")} | ${escapeHtml(latestDecision.decision_context?.timeframe || "-")}</h3>
        </div>
        <div class="runtime-badges">
          ${miniBadge(policy?.execution_allowed ? "policy ok" : "policy blocked", policy?.execution_allowed ? "success" : "danger")}
          ${miniBadge(risk?.approved ? "risk ok" : "risk blocked", risk?.approved ? "success" : "danger")}
          ${miniBadge(target?.delta_position_qty ? "active target" : "flat target", target?.delta_position_qty ? "info" : "outline")}
        </div>
      </div>
      <p class="hero-copy">${escapeHtml(outcome)}</p>
      ${renderFactGrid([
        ["Target Delta", formatSigned(target?.delta_position_qty)],
        ["Target Qty", formatSigned(target?.target_position_qty)],
        ["Baseline Bias", baseline?.direction_bias || "-"],
        ["Composite Alpha", formatSigned(baseline?.composite_alpha_score)],
        ["Position Scale", formatNumber(baseline?.suggested_position_scale)],
        ["Volatility Target", formatNumber(baseline?.volatility_target_scale)],
        ["Latest Order", latestOrder ? latestOrder.status : "-"],
        ["Latest Fill", latestFill ? `${formatNumber(latestFill.fill_qty)} @ ${formatNumber(latestFill.fill_price)}` : "-"],
      ])}
    </div>
  `;
}

function renderDecisionInvestigation(latestDecision) {
  if (!latestDecision.decision_id) {
    return emptyState("No decision detail is available yet.");
  }
  const context = latestDecision.decision_context || {};
  const baseline = latestDecision.baseline_assessment || {};
  const aiAssessment = latestDecision.ai_assessment || {};
  const target = latestDecision.position_target || {};
  const policy = latestDecision.policy_decision || {};
  const risk = latestDecision.risk_decision || {};
  const latestReconciliation = latestDecision.latest_reconciliation || null;

  return `
    <div class="overview-hero">
      <div class="hero-header">
        <div>
          <p class="hero-id">${escapeHtml(latestDecision.decision_id)}</p>
          <h3 class="hero-title">${escapeHtml(context.symbol || "-")} | ${escapeHtml(context.timeframe || "-")}</h3>
        </div>
        <div class="runtime-badges">
          ${miniBadge(policy.execution_allowed ? "policy ok" : "policy blocked", policy.execution_allowed ? "success" : "danger")}
          ${miniBadge(risk.approved ? "risk approved" : "risk denied", risk.approved ? "success" : "danger")}
        </div>
      </div>
      ${renderFactGrid([
        ["Decision Time", formatMaybeTimestamp(context.as_of_ts)],
        ["Bias", baseline.direction_bias || "-"],
        ["Baseline Confidence", formatNumber(baseline.confidence)],
        ["AI Mode", aiAssessment.operating_mode || state.data.mode?.ai_operating_mode || "-"],
        ["Target Qty", formatSigned(target.target_position_qty)],
        ["Target Delta", formatSigned(target.delta_position_qty)],
        ["Risk Rejections", listOrDash(risk.rejection_reasons)],
        ["Latest Reconciliation", latestReconciliation?.severity || "-"],
      ])}
      <div class="signal-card">
        <div class="signal-head">
          <span class="signal-title">Reason Codes</span>
          <button class="table-button" data-inspect-decision="${escapeHtml(latestDecision.decision_id)}">Open audit chain</button>
        </div>
        <div class="signal-copy">${escapeHtml(listOrDash(baseline.reason_codes || aiAssessment.reason_codes || risk.rejection_reasons))}</div>
      </div>
    </div>
  `;
}

function buildTimeline() {
  const items = [];
  const latestDecision = state.data.latestDecision || {};
  const latestValidation = state.data.reconciliationLatest?.latest_validation || null;
  const errors = state.data.executionErrors?.errors || [];
  const blockerHistory = state.data.blockers?.recent_history || [];
  const recovery = recoveryData();
  const latestRebaselineAction = state.data.systemRecovery?.latest_rebaseline_action || null;
  const latestResumeAction = state.data.systemRecovery?.latest_resume_action || null;

  if (latestDecision.decision_id) {
    items.push({
      title: "Latest decision completed",
      subtitle: latestDecision.decision_id,
      timestamp: latestDecision.decision_context?.as_of_ts,
      tone: latestDecision.risk_decision?.approved ? "info" : "warning",
      detail: decisionOutcomeLabel(latestDecision.summary),
    });
  }

  if (latestValidation) {
    items.push({
      title: "Reconciliation validation",
      subtitle: latestValidation.reconciliation_id || "-",
      timestamp: latestValidation.validated_at,
      tone: latestValidation.halt_required ? "danger" : "success",
      detail: listOrDash(latestValidation.mismatch_reasons),
    });
  }

  if (latestRebaselineAction) {
    items.push({
      title: "Operator rebaseline",
      subtitle: latestRebaselineAction.status || "-",
      timestamp: latestRebaselineAction.created_at,
      tone: recovery.review_required ? "warning" : latestRebaselineAction.status === "rebaseline_completed" ? "info" : "danger",
      detail: latestRebaselineAction.reason || "Operator accepted a new trusted baseline.",
    });
  }

  if (latestResumeAction) {
    items.push({
      title: "Operator resume",
      subtitle: latestResumeAction.status || "-",
      timestamp: latestResumeAction.created_at,
      tone: latestResumeAction.status === "resumed" || latestResumeAction.status === "already_resumed" ? "success" : "warning",
      detail: latestResumeAction.reason || "Operator requested resume.",
    });
  }

  errors.slice(0, 2).forEach((item) => {
    items.push({
      title: "Execution issue",
      subtitle: item.order_id || item.decision_id || "-",
      timestamp: item.timestamp,
      tone: item.severity === "error" ? "danger" : "warning",
      detail: item.message || item.status || "execution issue",
    });
  });

  blockerHistory.slice(-2).forEach((item) => {
    items.push({
      title: "Blocker snapshot",
      subtitle: item.runtime_state || "-",
      timestamp: item.created_at,
      tone: item.execution_blocked ? "warning" : "info",
      detail: item.blockers?.length ? item.blockers.map((blocker) => blocker.blocker).join(", ") : "clear",
    });
  });

  return items.sort((left, right) => dateValue(right.timestamp) - dateValue(left.timestamp)).slice(0, 6);
}

function renderTimeline(items, emptyText = "No recent operational activity.") {
  if (!items.length) {
    return emptyState(emptyText);
  }
  return `<div class="timeline-list">${items.map((item) => `
    <article class="timeline-item">
      <div class="timeline-head">
        <div>
          <div class="timeline-title">${escapeHtml(item.title)}</div>
          <div class="mono">${escapeHtml(item.subtitle || "-")}</div>
        </div>
        ${miniBadge(item.tone || "neutral", item.tone || "neutral")}
      </div>
      <div class="timeline-meta">${escapeHtml(formatMaybeTimestamp(item.timestamp))}</div>
      <div class="signal-copy">${escapeHtml(item.detail || "-")}</div>
    </article>
  `).join("")}</div>`;
}

function renderSignalCards(items, emptyText) {
  if (!items.length) {
    return emptyState(emptyText);
  }
  return items.map((item) => `
    <article class="signal-card">
      <div class="signal-head">
        <span class="signal-title">${escapeHtml(item.title)}</span>
        ${miniBadge(item.tone || "neutral", item.tone || "neutral")}
      </div>
      <div class="detail-meta">${escapeHtml(item.subtitle || "-")}</div>
      <div class="signal-copy">${escapeHtml(item.detail || "-")}</div>
    </article>
  `).join("");
}

function renderFactGrid(rows) {
  return `<div class="fact-grid">${rows.map(([label, value]) => `
    <div class="fact-row">
      <span class="fact-key">${escapeHtml(label)}</span>
      <strong class="fact-value">${escapeHtml(value == null || value === "" ? "-" : String(value))}</strong>
    </div>
  `).join("")}</div>`;
}

function renderTable(headers, rows, emptyText) {
  if (!rows.length) {
    return emptyState(emptyText);
  }
  return `
    <div class="table-shell">
      <table class="data-table">
        <thead>
          <tr>${headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("")}</tr>
        </thead>
        <tbody>
          ${rows.map((row) => `<tr>${row.map((cell) => `<td>${cell || "-"}</td>`).join("")}</tr>`).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function setStatusChip(node, label, tone) {
  node.textContent = label;
  node.className = `status-badge ${badgeClass(tone)}`;
}

function showStructuredDetail({ title, summary = [], sections = [] }) {
  const summaryHtml = summary.length
    ? `<div class="detail-card"><h3>Summary</h3><div class="detail-grid">${summary.map(([key, value]) => `
        <div class="detail-grid-row">
          <span class="detail-key">${escapeHtml(key)}</span>
          <strong class="detail-value">${escapeHtml(value == null || value === "" ? "-" : String(value))}</strong>
        </div>
      `).join("")}</div></div>`
    : `<div class="empty-state">No summary fields available.</div>`;
  const bodyHtml = sections.length
    ? sections.map((section) => `
      <section class="detail-card">
        <h3>${escapeHtml(section.title)}</h3>
        <pre class="detail-json">${escapeHtml(JSON.stringify(section.value, null, 2))}</pre>
      </section>
    `).join("")
    : `<div class="empty-state">No detail sections available.</div>`;
  setDrawerContent(title, summaryHtml, bodyHtml);
  openDrawer();
}

function detailCard(title, value) {
  return { title, value };
}

function setDrawerContent(title, summaryHtml, bodyHtml) {
  nodes.drawerTitle.textContent = title;
  nodes.drawerSummary.innerHTML = summaryHtml;
  nodes.drawerBody.innerHTML = bodyHtml;
}

function openDrawer() {
  nodes.detailDrawer.classList.add("is-open");
  nodes.detailDrawer.setAttribute("aria-hidden", "false");
}

function closeDrawer() {
  nodes.detailDrawer.classList.remove("is-open");
  nodes.detailDrawer.setAttribute("aria-hidden", "true");
}

function badgeClass(tone) {
  if (tone === "success") return "status-success";
  if (tone === "warning") return "status-warning";
  if (tone === "danger") return "status-danger";
  if (tone === "info") return "status-info";
  if (tone === "neutral") return "status-neutral";
  return "status-outline";
}

function miniBadge(label, tone = "neutral") {
  return `<span class="mini-badge ${badgeClass(tone).replace("status-", "mini-")}">${escapeHtml(label)}</span>`;
}

function toneForRuntimeState(runtimeState) {
  if (runtimeState === "healthy") return "success";
  if (runtimeState === "degraded") return "warning";
  if (runtimeState === "blocked" || runtimeState === "halted") return "danger";
  return "neutral";
}

function toneForOrderStatus(status) {
  if (status === "FILLED") return "success";
  if (status === "PARTIALLY_FILLED" || status === "SUBMITTED" || status === "SUBMITTING") return "info";
  if (status === "CANCELED" || status === "CANCEL_PENDING") return "warning";
  if (status === "FAILED" || status === "REJECTED" || status === "BLOCKED") return "danger";
  return "outline";
}

function decisionOutcomeLabel(summary) {
  if (!summary) return "No decision outcome.";
  const orderCount = summary.execution_result?.order_count ?? 0;
  const fillCount = summary.execution_result?.fill_count ?? 0;
  if (summary.risk_result === false) return "Risk denied execution for this decision.";
  if (summary.policy_result === false) return "Policy blocked execution for this decision.";
  if (orderCount === 0) return "Decision completed without creating an execution intent.";
  if (fillCount === 0) return "Execution intent exists but no fills have been ingested yet.";
  return "Decision flowed through execution and produced fills.";
}

function freshnessSummary(freshness) {
  const parts = [
    freshness.market_fresh ? "market" : null,
    freshness.account_fresh ? "account" : null,
    freshness.reconciliation_fresh ? "recon" : null,
  ].filter(Boolean);
  return parts.length === 3 ? "Fully Fresh" : parts.length ? parts.join(" + ") : "Stale";
}

function recoveryData() {
  return state.data.systemRecovery?.recovery || state.data.runtime?.recovery || state.data.accountState?.recovery || {};
}

function recoverySummaryLine(recovery) {
  const parts = [];
  if (recovery.resume_eligible) {
    parts.push("resume eligible");
  } else if (recovery.review_required) {
    parts.push("review required");
  } else if (recovery.recovery_state) {
    parts.push(readableState(recovery.recovery_state));
  }
  if (recovery.resume_blocked_reasons?.length) {
    parts.push(listOrDash(recovery.resume_blocked_reasons));
  }
  return parts.length ? parts.join(" | ") : "-";
}

function formatDuration(seconds) {
  const number = Number(seconds);
  if (!Number.isFinite(number)) return "-";
  if (number < 60) return `${Math.round(number)}s`;
  if (number < 3600) return `${Math.floor(number / 60)}m ${Math.round(number % 60)}s`;
  return `${Math.floor(number / 3600)}h ${Math.floor((number % 3600) / 60)}m`;
}

function readableState(value) {
  return value ? String(value).replaceAll("_", " ") : "-";
}

function readableMode(value) {
  return value ? String(value).replaceAll("_", " ") : "-";
}

function listOrDash(value) {
  if (!value || (Array.isArray(value) && value.length === 0)) return "-";
  return Array.isArray(value) ? value.join(", ") : String(value);
}

function booleanWord(value) {
  if (value === true) return "yes";
  if (value === false) return "no";
  return "-";
}

function booleanShort(value) {
  if (value === true) return "ok";
  if (value === false) return "blocked";
  return "n/a";
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (Number.isNaN(number)) return String(value);
  if (Math.abs(number) >= 1000) return number.toFixed(2);
  if (Math.abs(number) >= 1) return number.toFixed(4);
  return number.toFixed(6);
}

function formatSigned(value) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (Number.isNaN(number)) return String(value);
  return `${number > 0 ? "+" : ""}${formatNumber(number)}`;
}

function formatDateTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("en-CA", { hour12: false });
}

function formatMaybeTimestamp(value) {
  return value ? formatDateTime(value) : "-";
}

function dateValue(value) {
  if (!value) return 0;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

function emptyState(message) {
  return `<div class="empty-state">${escapeHtml(message)}</div>`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function showDetail(title, sections) {
  showStructuredDetail({
    title,
    sections: (sections || []).map((section) => ({ title: section.title, value: section.value })),
  });
}

window.refreshDashboard = refreshDashboard;
window.showDetail = showDetail;
window.setActiveView = setActiveView;
