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
  overviewPostureTitle: "overviewPostureTitle",
  overviewPostureCopy: "overviewPostureCopy",
  overviewPortfolio: "overviewPortfolio",
  overviewBlockers: "overviewBlockers",
  overviewBlockerStamp: "overviewBlockerStamp",
  overviewRecoveryPanel: "overviewRecoveryPanel",
  overviewRecovery: "overviewRecovery",
  overviewRecoveryTitle: "overviewRecoveryTitle",
  overviewRecoveryCopy: "overviewRecoveryCopy",
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
  runtimeProfileSummary: "runtimeProfileSummary",
  runtimeProfileSupervisor: "runtimeProfileSupervisor",
  runtimeProfileForm: "runtimeProfileForm",
  runtimeProfileRevisionSelect: "runtimeProfileRevisionSelect",
  runtimeProfileLabel: "runtimeProfileLabel",
  runtimeProfileDefaultSymbol: "runtimeProfileDefaultSymbol",
  runtimeProfileAllowedSymbols: "runtimeProfileAllowedSymbols",
  runtimeProfileProductType: "runtimeProfileProductType",
  runtimeProfileMarginMode: "runtimeProfileMarginMode",
  runtimeProfileDefaultOrderQty: "runtimeProfileDefaultOrderQty",
  runtimeProfileMaxNotional: "runtimeProfileMaxNotional",
  runtimeProfileMaxPositionQty: "runtimeProfileMaxPositionQty",
  runtimeProfileMaxOpenOrders: "runtimeProfileMaxOpenOrders",
  runtimeProfileDefaultLeverage: "runtimeProfileDefaultLeverage",
  runtimeProfileMaxLeverage: "runtimeProfileMaxLeverage",
  runtimeProfileShortBias: "runtimeProfileShortBias",
  runtimeProfileDynamicLeverage: "runtimeProfileDynamicLeverage",
  runtimeProfileActivationNote: "runtimeProfileActivationNote",
  runtimeProfileCreateButton: "runtimeProfileCreateButton",
  runtimeProfileSaveButton: "runtimeProfileSaveButton",
  runtimeProfileStageButton: "runtimeProfileStageButton",
  runtimeProfileCancelPendingButton: "runtimeProfileCancelPendingButton",
  runtimeProfileRestartButton: "runtimeProfileRestartButton",
  runtimeProfilePermissionNote: "runtimeProfilePermissionNote",
  runtimeProfileImpact: "runtimeProfileImpact",
  runtimeProfileTable: "runtimeProfileTable",
  operatorSummary: "operatorSummary",
  operatorBootstrap: "operatorBootstrap",
  operatorCreateForm: "operatorCreateForm",
  operatorCreateUsername: "operatorCreateUsername",
  operatorCreatePassword: "operatorCreatePassword",
  operatorCreateRole: "operatorCreateRole",
  operatorCreateEnabled: "operatorCreateEnabled",
  operatorCreateButton: "operatorCreateButton",
  operatorPermissionNote: "operatorPermissionNote",
  operatorUsersTable: "operatorUsersTable",
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
  nodes.runtimeProfileCreateButton.addEventListener("click", () => void createRuntimeProfileDraft());
  nodes.runtimeProfileForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void saveRuntimeProfileDraft();
  });
  nodes.runtimeProfileRevisionSelect.addEventListener("change", populateRuntimeProfileDraftForm);
  nodes.runtimeProfileStageButton.addEventListener("click", () => void stageRuntimeProfileDraft());
  nodes.runtimeProfileCancelPendingButton.addEventListener("click", () => void cancelPendingRuntimeProfile());
  nodes.runtimeProfileRestartButton.addEventListener("click", () => void requestRuntimeProfileRestart());
  nodes.operatorCreateForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void createOperatorUser();
  });
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
      return;
    }
    const toggleUserButton = target.closest("[data-toggle-user]");
    if (toggleUserButton instanceof HTMLElement) {
      void toggleOperatorUser(toggleUserButton.dataset.toggleUser || "", toggleUserButton.dataset.nextEnabled === "true");
      return;
    }
    const roleUserButton = target.closest("[data-role-user]");
    if (roleUserButton instanceof HTMLElement) {
      void updateOperatorUserRole(roleUserButton.dataset.roleUser || "", roleUserButton.dataset.currentRole || "");
      return;
    }
    const passwordUserButton = target.closest("[data-password-user]");
    if (passwordUserButton instanceof HTMLElement) {
      void resetOperatorUserPassword(passwordUserButton.dataset.passwordUser || "");
      return;
    }
    const deleteUserButton = target.closest("[data-delete-user]");
    if (deleteUserButton instanceof HTMLElement) {
      void deleteOperatorUser(deleteUserButton.dataset.deleteUser || "");
      return;
    }
    const selectRuntimeProfileButton = target.closest("[data-select-runtime-profile]");
    if (selectRuntimeProfileButton instanceof HTMLElement) {
      nodes.runtimeProfileRevisionSelect.value = selectRuntimeProfileButton.dataset.selectRuntimeProfile || "";
      populateRuntimeProfileDraftForm();
      setActiveView("runtime-profiles");
      return;
    }
    const stageRuntimeProfileButton = target.closest("[data-stage-runtime-profile]");
    if (stageRuntimeProfileButton instanceof HTMLElement) {
      nodes.runtimeProfileRevisionSelect.value = stageRuntimeProfileButton.dataset.stageRuntimeProfile || "";
      populateRuntimeProfileDraftForm();
      void stageRuntimeProfileDraft();
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

function setRuntimeProfilesViewEnabled(enabled) {
  const tab = document.querySelector('.workspace-tab[data-view="runtime-profiles"]');
  const view = document.querySelector('.workspace-view[data-view="runtime-profiles"]');
  if (tab instanceof HTMLElement) {
    tab.hidden = !enabled;
  }
  if (view instanceof HTMLElement) {
    view.hidden = !enabled;
  }
  if (!enabled && state.activeView === "runtime-profiles") {
    setActiveView("overview");
  }
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
    ["authProviders", "/auth/providers"],
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

  const runtimeProfileControlEnabled = state.data.authProviders?.runtime_profile_control_enabled === true;

  if (operatorCanAdmin()) {
    const operatorUsers = await fetchPanel("operatorUsers", "/auth/users");
    if (operatorUsers.ok) {
      state.data[operatorUsers.key] = operatorUsers.data;
      delete state.panelErrors[operatorUsers.key];
    } else {
      state.panelErrors[operatorUsers.key] = operatorUsers.error;
    }
    if (runtimeProfileControlEnabled) {
      const runtimeProfiles = await fetchPanel("runtimeProfiles", "/runtime-profiles");
      if (runtimeProfiles.ok) {
        state.data[runtimeProfiles.key] = runtimeProfiles.data;
        delete state.panelErrors[runtimeProfiles.key];
      } else {
        state.panelErrors[runtimeProfiles.key] = runtimeProfiles.error;
      }
    } else {
      delete state.data.runtimeProfiles;
      delete state.panelErrors.runtimeProfiles;
    }
  } else {
    delete state.data.operatorUsers;
    delete state.panelErrors.operatorUsers;
    delete state.data.runtimeProfiles;
    delete state.panelErrors.runtimeProfiles;
  }

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
  renderRuntimeProfiles();
  renderOperators();
  nodes.lastRefreshLabel.textContent = state.lastRefreshAt
    ? `Last refresh ${formatDateTime(state.lastRefreshAt)}${manual ? " | manual" : ""}`
    : "Not refreshed yet";
}

function updateActionAccess() {
  const canWrite = operatorCanWrite();
  const recovery = recoveryData();
  const recoveryPolicy = recoveryPolicyData();
  nodes.reconcileButton.disabled = state.refreshing || !canWrite;
  nodes.rebaselineButton.disabled = state.refreshing || !canWrite || !recoveryPolicy.operator_rebaseline_supported || !recovery.rebaseline_available;
  nodes.rebaselineButton.hidden = !recoveryPolicy.operator_rebaseline_supported;
  nodes.resumeButton.disabled = state.refreshing || !canWrite;
  nodes.haltButton.disabled = state.refreshing || !canWrite;
  nodes.actionPermissionHint.textContent = permissionHint(canWrite);
}

function renderEmptyState() {
  setRuntimeProfilesViewEnabled(false);
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
  nodes.runtimeProfileSummary.innerHTML = emptyState("No runtime profile control state yet.");
  nodes.runtimeProfileSupervisor.innerHTML = emptyState("No supervisor state yet.");
  nodes.runtimeProfileImpact.innerHTML = emptyState("No draft diff yet.");
  nodes.runtimeProfileTable.innerHTML = emptyState("No runtime profile revisions yet.");
  nodes.runtimeProfilePermissionNote.textContent = "Checking runtime profile permissions.";
  nodes.operatorSummary.innerHTML = emptyState("No auth posture yet.");
  nodes.operatorBootstrap.innerHTML = emptyState("No bootstrap status yet.");
  nodes.operatorUsersTable.innerHTML = emptyState("No operator account data yet.");
  nodes.operatorPermissionNote.textContent = "Checking admin access.";
  nodes.runtimeProfileRevisionSelect.innerHTML = `<option value="">No draft selected</option>`;
  setRuntimeProfileFormEnabled(false);
  setOperatorCreateFormEnabled(false);
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

function operatorCanAdmin() {
  const session = state.data.session || {};
  const operatorAuth = state.data.runtime?.operator_auth || {};
  if (session.auth_enabled) {
    return session.role === "admin";
  }
  return session.role === "admin" && Boolean(operatorAuth.unsafe_write_without_auth);
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
  const runtimeProfile = runtimeProfileData();
  const environment = environmentData();
  setStatusChip(nodes.runtimeStateChip, readableState(health.runtime_state || health.overall_status), toneForRuntimeState(health.runtime_state));
  setStatusChip(nodes.operatingStateChip, readableMode(runtimeProfile.name || mode.operating_state || "unknown"), mode.execution_blocked ? "warning" : "outline");
  setStatusChip(nodes.executionRouteChip, environment.execution_route || mode.execution_route || mode.execution_backend || "unknown", mode.exchange_submit_allowed ? "info" : "outline");
  setStatusChip(
    nodes.submitPostureChip,
    environment.exchange_submission_enabled
      ? "exchange submit"
      : environment.exchange_submission_possible
      ? "guarded exchange"
      : "local paper",
    environment.exchange_submission_enabled ? "success" : environment.exchange_submission_possible ? "warning" : "neutral",
  );
}

function renderRuntimeStrip() {
  const health = state.data.health || {};
  const mode = state.data.mode || {};
  const runtimeProfile = runtimeProfileData();
  const environment = environmentData();
  const portfolio = state.data.portfolio?.portfolio || null;
  const primaryPosition = trackedPortfolioPosition(portfolio);
  const freshness = health.freshness || {};
  const account = state.data.accountState || {};
  const recovery = recoveryData();

  nodes.stripOverallValue.textContent = readableState(health.runtime_state || health.overall_status);
  nodes.stripOverallMeta.textContent = health.execution_blocked ? listOrDash(health.submit_blocked_reasons || health.blockers?.map((item) => item.blocker)) : "No active blockers";

  nodes.stripModeValue.textContent = readableMode(runtimeProfile.name || mode.operating_state || mode.mode || "unknown");
  nodes.stripModeMeta.textContent = `${readableMode(mode.operating_state || "-")} | ${readableMode(environment.product_type || mode.trading_product_type || "-")} | ${readableMode(environment.margin_model || mode.margin_mode || "-")}`;

  nodes.stripExecutionValue.textContent = environment.exchange_submission_enabled
    ? "Exchange Armed"
    : environment.exchange_submission_possible
    ? "Guarded Exchange"
    : "Local Paper";
  nodes.stripExecutionMeta.textContent = environment.exchange_submission_enabled
    ? `${environment.exchange_submission_target || "exchange"} | ${readableMode(environment.position_directionality)} | lev ${formatNumber(mode.max_target_leverage || 1)}`
    : `${listOrDash(mode.submit_blocked_reasons)} | ${readableMode(environment.position_directionality)}`;

  if (runtimeProfile.name === "paper_local") {
    nodes.stripRecoveryValue.textContent = "Local Paper";
    nodes.stripRecoveryMeta.textContent = recovery.safe_to_trade
      ? "No exchange baseline takeover in this profile"
      : recoverySummaryLine(recovery);
  } else {
    nodes.stripRecoveryValue.textContent = readableState(recovery.recovery_state);
    nodes.stripRecoveryMeta.textContent = recoverySummaryLine(recovery);
  }

  nodes.stripFreshnessValue.textContent = freshnessSummary(freshness);
  nodes.stripFreshnessMeta.textContent = `market ${booleanWord(freshness.market_fresh)} | account ${booleanWord(account.fresh)} | recon ${booleanWord(freshness.reconciliation_fresh)}`;

  nodes.stripEquityValue.textContent = portfolio ? formatNumber(portfolio.total_equity) : "-";
  nodes.stripEquityMeta.textContent = portfolio
    ? `gross ${formatNumber(portfolio.gross_exposure)} | ${positionPosture(primaryPosition)}`
    : "No portfolio snapshot";
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
      message: recoveryPolicyData().operator_rebaseline_supported
        ? "Recovery review is required. Accept the current exchange state as a new baseline before resuming automation."
        : "Recovery review is required before trusting the next automated action.",
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
  const runtimeProfile = runtimeProfileData();
  const environment = environmentData();
  const policyProfile = state.data.runtime?.policy_profile || state.data.mode?.policy_profile || {};
  const portfolio = state.data.portfolio?.portfolio || null;
  const latestDecision = state.data.latestDecision || {};
  const executionLatest = state.data.executionLatest || {};
  const blockers = state.data.blockers?.blockers || [];
  const metrics = state.data.metrics || {};
  const recovery = recoveryData();
  const baseline = state.data.runtime?.baseline_takeover || {};
  const paperLocal = runtimeProfile.name === "paper_local";
  const primaryPosition = trackedPortfolioPosition(portfolio);

  applyOverviewProfile(runtimeProfile);

  nodes.overviewDecisionSpotlight.innerHTML = renderDecisionHero(latestDecision, executionLatest);
  nodes.overviewPosture.innerHTML = renderFactGrid(paperLocal
      ? [
        ["Runtime", readableState(health.runtime_state)],
        ["Profile", readableMode(runtimeProfile.name)],
        ["Product", readableMode(environment.product_type)],
        ["Margin Model", readableMode(environment.margin_model)],
        ["Market Feed", environment.market_data_source_kind || "-"],
        ["Execution Route", environment.execution_route || mode.execution_route || "-"],
        ["Directionality", readableMode(environment.position_directionality)],
        ["Leverage Support", readableMode(environment.leverage_support)],
        ["Local Only", booleanWord(environment.local_only)],
        ["Exchange Coupled", booleanWord(environment.exchange_coupled)],
        ["Account Observation", booleanWord(state.data.accountState?.read_enabled)],
        ["Halted", booleanWord(health.halted)],
      ]
    : [
        ["Overall", readableState(health.overall_status)],
        ["Runtime", readableState(health.runtime_state)],
        ["Profile", readableMode(runtimeProfile.name)],
        ["Product", readableMode(environment.product_type)],
        ["Margin Model", readableMode(environment.margin_model)],
        ["Directionality", readableMode(environment.position_directionality)],
        ["Leverage Support", readableMode(environment.leverage_support)],
        ["Operating State", readableMode(mode.operating_state)],
        ["Execution Route", environment.execution_route || mode.execution_route || "-"],
        ["Submit Target", environment.exchange_submission_target || "-"],
        ["Submit Allowed", booleanWord(mode.exchange_submit_allowed)],
        ["Human Approval", booleanWord(policyProfile.requires_human_approval)],
    ]);

    nodes.overviewPortfolio.innerHTML = renderFactGrid([
      ["Total Equity", portfolio ? formatNumber(portfolio.total_equity) : "-"],
      ["Realized PnL", portfolio ? formatSigned(portfolio.realized_pnl) : "-"],
      ["Unrealized PnL", portfolio ? formatSigned(portfolio.unrealized_pnl) : "-"],
      ["Gross Exposure", portfolio ? formatNumber(portfolio.gross_exposure) : "-"],
      ["Net Exposure", portfolio ? formatSigned(portfolio.net_exposure) : "-"],
      ["Primary Symbol", trackedSymbol() || "-"],
      ["Primary Side", primaryPosition ? readableMode(primaryPosition.exposure_side) : "-"],
      ["Primary Qty", primaryPosition ? formatSigned(primaryPosition.position_qty) : "-"],
      ["Primary Leverage", primaryPosition ? formatNumber(primaryPosition.target_leverage) : "-"],
      ["Margin Usage", portfolio ? formatNumber(portfolio.margin_usage) : "-"],
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
      ["Latest Decision", formatMaybeTimestamp(latestDecision.decision_context?.as_of_ts)],
      ["Latest Fill", fillFreshnessLabel(executionLatest.latest_fill)],
      ["Latest Action", decisionActivityLabel(latestDecision, executionLatest.latest_order, executionLatest.latest_fill)],
      ["Account Fresh", booleanWord(state.data.accountState?.fresh)],
    ]);

  nodes.overviewTimeline.innerHTML = renderTimeline(buildTimeline({ paperLocal }));
}

function renderDecisions() {
  const latestDecision = state.data.latestDecision || {};
  const recentDecisions = state.data.recentDecisions?.decisions || [];

  nodes.decisionSpotlight.innerHTML = renderDecisionInvestigation(latestDecision);
  nodes.decisionTable.innerHTML = renderTable(
    ["Decision", "Intent", "Outcome", "When", "Open"],
    recentDecisions.map((item) => ([
      `<div class="cell-stack"><strong>${escapeHtml(item.symbol || "-")}</strong><div class="table-meta">${escapeHtml(item.timeframe || "-")} | ${escapeHtml(item.decision_id || "-")}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(recentDecisionHeadline(item))}</strong><div class="table-meta">${escapeHtml(recentDecisionNarrative(item))}</div></div>`,
      `<div class="cell-stack"><div class="table-inline-badges">${miniBadge(item.policy_result ? "policy ok" : "policy blocked", item.policy_result ? "success" : "danger")}${miniBadge(item.risk_result ? "risk ok" : "risk blocked", item.risk_result ? "success" : "danger")}</div><div class="table-meta">${escapeHtml(recentDecisionOutcome(item))}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(formatRelativeAge(item.decision_time))}</strong><div class="table-meta">${escapeHtml(formatMaybeTimestamp(item.decision_time))}</div></div>`,
      item.decision_id ? `<button class="table-button" data-inspect-decision="${escapeHtml(item.decision_id)}">Inspect</button>` : "",
    ])),
    "No recent decisions available."
  );
}

function renderExecution() {
  const execution = state.data.executionLatest || {};
  const mode = execution.mode || state.data.mode || {};
  const runtimeProfile = runtimeProfileData();
  const environment = environmentData();
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
          <h3 class="hero-title">${escapeHtml(executionHeadline(runtimeProfile, environment, mode))}</h3>
        </div>
        <div class="runtime-badges">
          ${miniBadge(readableMode(runtimeProfile.name || "unknown"), environment.exchange_coupled ? "info" : "outline")}
          ${miniBadge(executionModeBadge(environment, mode), executionModeTone(environment, mode))}
        </div>
        </div>
        <p class="hero-copy">${escapeHtml(executionCopy(runtimeProfile, environment, mode))}</p>
        ${renderFactGrid([
          ["Execution Ready", booleanWord(readiness.ready)],
          ["Product", readableMode(environment.product_type)],
          ["Margin Model", readableMode(environment.margin_model)],
          ["Directionality", readableMode(environment.position_directionality)],
          ["Latest Order", latestOrder ? `${latestOrder.status} | ${latestOrder.client_order_id}` : "-"],
          ["Latest Fill", latestFill ? `${formatNumber(latestFill.fill_qty)} @ ${formatNumber(latestFill.fill_price)}` : "-"],
          ["Latest Intent", latestFill?.position_intent || latestOrder?.submission_payload?.positionIntent || state.data.latestDecision?.position_target?.position_intent || "-"],
          ["Latest Fill Age", fillFreshnessLabel(latestFill)],
          ["Reconciliation", latestReconciliation ? latestReconciliation.severity : "-"],
          ["Recovery", readableState(execution.recovery?.recovery_state)],
          ["Open Orders", formatNumber(state.data.metrics?.current_open_order_count)],
        ])}
      </div>
    `;

  nodes.orderTable.innerHTML = renderTable(
    ["Order", "Meaning", "State", "Updated", "Open"],
    recentOrders.map((order) => ([
      `<div class="cell-stack"><strong>${escapeHtml(order.symbol || "-")}</strong><div class="table-meta">${escapeHtml(order.client_order_id || "-")}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(recentOrderHeadline(order))}</strong><div class="table-meta">${escapeHtml(recentOrderNarrative(order))}</div></div>`,
      `<div class="cell-stack"><div class="table-inline-badges">${miniBadge(order.status || "-", toneForOrderStatus(order.status))}</div><div class="table-meta">${escapeHtml(recentOrderStateSummary(order))}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(formatRelativeAge(order.last_update_ts || order.created_at))}</strong><div class="table-meta">${escapeHtml(formatMaybeTimestamp(order.last_update_ts || order.created_at))}</div></div>`,
      order.client_order_id ? `<button class="table-button" data-inspect-order="${escapeHtml(order.client_order_id)}">Inspect</button>` : "",
    ])),
    "No recent orders available."
  );

  nodes.fillTable.innerHTML = renderTable(
    ["Fill", "What Happened", "Impact", "Ingested", "Open"],
    recentFills.map((fill) => ([
      `<div class="cell-stack"><strong>${escapeHtml(fill.symbol || "-")}</strong><div class="table-meta">${escapeHtml(fill.fill_id || "-")}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(recentFillHeadline(fill))}</strong><div class="table-meta">${escapeHtml(recentFillNarrative(fill))}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(recentFillImpactSummary(fill))}</strong><div class="table-meta">${escapeHtml(`fee ${formatNumber(fill.fee_amount)} ${fill.fee_currency || ""}`.trim())}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(formatRelativeAge(fill.ingestion_timestamp))}</strong><div class="table-meta">${escapeHtml(formatMaybeTimestamp(fill.ingestion_timestamp))}</div></div>`,
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

    nodes.diagnosticRecovery.innerHTML = renderSignalCards([
      {
        title: `Recovery ${readableState(recovery.recovery_state)}`,
        subtitle: `safe ${booleanWord(recovery.safe_to_trade)} | resume ${booleanWord(recovery.resume_eligible)}`,
        tone: recovery.safe_to_trade ? "success" : recovery.review_required ? "warning" : "danger",
        detail: recovery.review_required
          ? `Operator review is still required. Resume blockers: ${listOrDash(recovery.resume_blocked_reasons)}.`
          : recovery.safe_to_trade
            ? "Recovery posture is clear enough to keep trading."
            : `Trading is still gated by ${listOrDash(recovery.resume_blocked_reasons)}.`,
      },
      {
        title: `Baseline ${readableState(baseline.status)}`,
        subtitle: `${readableState(baseline.baseline_kind)} | ${baseline.baseline_source || "-"}`,
        tone: baseline.status === "ready" || baseline.status === "accepted" ? "info" : "outline",
        detail: baseline.baseline_imported_at
          ? `Imported ${formatRelativeAge(baseline.baseline_imported_at)}. Event ref ${baseline.event_ref || "-"}.`
          : "No baseline import timestamp is stored yet.",
      },
      {
        title: "Operator recovery controls",
        subtitle: `rebaseline ${booleanWord(recovery.rebaseline_available)} | review ${booleanWord(recovery.review_required)}`,
        tone: recovery.rebaseline_available ? "warning" : "info",
        detail: baseline.last_rebaseline_event_ref
          ? `Last rebaseline event ${baseline.last_rebaseline_event_ref}.`
          : "No rebaseline event has been recorded yet.",
      },
    ], "No recovery posture yet.");

    nodes.diagnosticReplay.innerHTML = renderSignalCards([
      {
        title: replay.last_validation ? "Replay validated recently" : "Replay idle",
        subtitle: replay.last_validation?.decision_id || "no recent validation",
        tone: replay.healthy ? "success" : replay.supported ? "warning" : "outline",
        detail: replay.last_validation
          ? `Validated ${formatRelativeAge(replay.last_validation.validated_at)} with ${formatNumber(replay.last_validation.divergence_count)} divergence${pluralize(replay.last_validation.divergence_count)}.`
          : "No recent replay validation is stored.",
      },
      {
        title: "Replay coverage",
        subtitle: `supported ${booleanWord(replay.supported)} | healthy ${booleanWord(replay.healthy)}`,
        tone: replay.supported ? "info" : "outline",
        detail: `Replayed events ${formatNumber(replay.last_validation?.replayed_event_count)} | baseline switches ${formatNumber(replay.last_validation?.baseline_switch_count)}.`,
      },
    ], "No replay validation yet.");

    nodes.diagnosticAccount.innerHTML = renderSignalCards([
      {
        title: `Account backend ${readableMode(account.backend || "-")}`,
        subtitle: `connected ${booleanWord(account.connected)} | fresh ${booleanWord(account.fresh)} | ready ${booleanWord(account.ready)}`,
        tone: account.ready ? "success" : account.connected ? "warning" : "danger",
        detail: account.current_blocking_reason
          ? `Current blocking reason: ${account.current_blocking_reason}.`
          : `Last refresh ${account.last_refresh_timestamp ? formatRelativeAge(account.last_refresh_timestamp) : "-"}.`,
      },
      {
        title: "Baseline posture",
        subtitle: readableState(account.baseline_takeover?.status),
        tone: account.baseline_takeover?.status ? "info" : "outline",
        detail: `Baseline source ${account.baseline_takeover?.baseline_source || "-"} | recovery ${readableState(account.recovery?.recovery_state)}.`,
      },
    ], "No account state yet.");

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

    nodes.diagnosticMetrics.innerHTML = renderSignalCards([
      {
        title: "Decision cadence",
        subtitle: `${formatNumber(metrics.decision_cycle_count)} cycles | ${formatNumber(metrics.order_intent_count)} intents`,
        tone: "info",
        detail: `Latest decision ${runtime.last_decision_timestamp ? formatRelativeAge(runtime.last_decision_timestamp) : "-"} | uptime ${formatDuration(runtime.uptime_seconds)}.`,
      },
      {
        title: "Execution throughput",
        subtitle: `${formatNumber(metrics.fill_count)} fills | ${formatNumber(metrics.current_open_order_count)} open orders`,
        tone: metrics.current_open_order_count > 0 ? "warning" : "success",
        detail: `Latest fill ${fillFreshnessLabel(state.data.executionLatest?.latest_fill)}.`,
      },
      {
        title: "Recent activity",
        subtitle: `${formatNumber(metrics.rejection_count)} rejection${pluralize(metrics.rejection_count)}`,
        tone: metrics.rejection_count > 0 ? "warning" : "info",
        detail: decisionActivityLabel(state.data.latestDecision || {}, state.data.executionLatest?.latest_order, state.data.executionLatest?.latest_fill),
      },
    ], "No runtime metrics yet.");
  }

function renderRuntimeProfiles() {
  const profiles = state.data.runtimeProfiles || {};
  const controlEnabled = state.data.authProviders?.runtime_profile_control_enabled === true;
  const payload = profiles.current_runtime_payload || {};
  setRuntimeProfilesViewEnabled(false);
  nodes.runtimeProfileSummary.innerHTML = renderFactGrid([
    ["Control Mode", "Environment file switch"],
    ["Profile Source", readableState(profiles.profile_source || state.data.runtime?.profile_source || "env_fallback")],
    ["Default Symbol", payload.default_symbol || "-"],
    ["Allowed Symbols", listOrDash(payload.allowed_symbols)],
    ["Product Type", readableState(payload.trading_product_type || "-")],
    ["Margin Mode", readableState(payload.margin_mode || "-")],
  ]);
  nodes.runtimeProfileSupervisor.innerHTML = emptyState(
    controlEnabled
      ? "Browser-managed runtime profile control is enabled."
      : "Runtime profile control in the UI is disabled. Switch posture by replacing .env with .env.spot.backup or .env.derivatives.primary, then restart the service."
  );
  nodes.runtimeProfileImpact.innerHTML = emptyState("No browser-managed revisions in env-switch mode.");
  nodes.runtimeProfileTable.innerHTML = emptyState("Runtime profile drafts are disabled in env-switch mode.");
  nodes.runtimeProfileRevisionSelect.innerHTML = `<option value="">Env switch mode</option>`;
  nodes.runtimeProfilePermissionNote.textContent = "Runtime posture changes now come from the env preset files, not the browser control plane.";
  setRuntimeProfileFormEnabled(false);
}

function renderOperators() {
  const providers = state.data.authProviders || {};
  const runtimeAuth = state.data.runtime?.operator_auth || {};
  const operatorUsers = state.data.operatorUsers || {};
  const users = operatorUsers.users || [];
  const canAdmin = operatorCanAdmin();

  nodes.operatorSummary.innerHTML = renderFactGrid([
    ["Auth Enabled", booleanWord(providers.auth_enabled)],
    ["Session Enabled", booleanWord(providers.session_enabled)],
    ["Database Backed", booleanWord(providers.database_backed)],
    ["Stored Users", formatNumber(providers.stored_user_count)],
    ["Configured Roles", listOrDash(providers.configured_roles)],
    ["API-Key Compatibility", booleanWord(providers.api_key_compatibility_enabled)],
    ["Current Identity", state.data.session?.identity || "-"],
    ["Current Role", readableState(state.data.session?.role)],
  ]);

  nodes.operatorBootstrap.innerHTML = renderFactGrid([
    ["Bootstrap Enabled", booleanWord(runtimeAuth.bootstrap_enabled)],
    ["Bootstrap Configured", booleanWord(runtimeAuth.bootstrap_configured)],
    ["Bootstrap Pending", booleanWord(operatorUsers.bootstrap_pending ?? providers.bootstrap_pending)],
    ["Enabled Users", formatNumber(operatorUsers.enabled_user_count)],
    ["Enabled Admins", formatNumber(operatorUsers.enabled_admin_count)],
    ["Unsafe Local Write", booleanWord(runtimeAuth.unsafe_write_without_auth)],
    ["Session Source", readableState(state.data.session?.auth_source)],
    ["Admin Access", canAdmin ? "granted" : "not available"],
  ]);

  setOperatorCreateFormEnabled(canAdmin);
  if (!canAdmin) {
    nodes.operatorPermissionNote.textContent = "Admin access is required to manage operator accounts.";
    nodes.operatorUsersTable.innerHTML = emptyState("Sign in as an admin to create, edit, disable, or delete operator users.");
    return;
  }

  nodes.operatorPermissionNote.textContent = "Admin session active. Changes are written to the operator user table immediately.";
  nodes.operatorUsersTable.innerHTML = renderTable(
    ["Username", "Role", "Status", "Last Login", "Updated", "Session", "Actions"],
    users.map((user) => ([
      `<div><strong>${escapeHtml(user.username || "-")}</strong><div class="mono">${escapeHtml(user.user_id || "-")}</div></div>`,
      miniBadge(user.role || "-", user.role === "admin" ? "danger" : user.role === "operator" ? "info" : "outline"),
      `<div>${miniBadge(user.enabled ? "enabled" : "disabled", user.enabled ? "success" : "warning")}${user.protected_last_admin ? '<div class="table-meta">last enabled admin</div>' : ""}</div>`,
      escapeHtml(formatMaybeTimestamp(user.last_login_at)),
      escapeHtml(formatMaybeTimestamp(user.updated_at || user.created_at)),
      user.is_current_session_user ? miniBadge("current session", "info") : '<span class="table-meta">other account</span>',
      renderOperatorUserActions(user),
    ])),
    "No operator users are stored yet."
  );
}

function renderOperatorUserActions(user) {
  const toggleDisabled = user.protected_last_admin || user.is_current_session_user;
  const deleteDisabled = user.protected_last_admin || user.is_current_session_user;
  return `
    <div class="table-actions">
      <button class="table-button" data-role-user="${escapeHtml(user.username)}" data-current-role="${escapeHtml(user.role || "")}">Role</button>
      <button class="table-button" data-password-user="${escapeHtml(user.username)}">Password</button>
      <button class="table-button" data-toggle-user="${escapeHtml(user.username)}" data-next-enabled="${String(!user.enabled)}" ${toggleDisabled ? "disabled" : ""}>${user.enabled ? "Disable" : "Enable"}</button>
      <button class="table-button" data-delete-user="${escapeHtml(user.username)}" ${deleteDisabled ? "disabled" : ""}>Delete</button>
    </div>
  `;
}

function setOperatorCreateFormEnabled(enabled) {
  nodes.operatorCreateUsername.disabled = !enabled;
  nodes.operatorCreatePassword.disabled = !enabled;
  nodes.operatorCreateRole.disabled = !enabled;
  nodes.operatorCreateEnabled.disabled = !enabled;
  nodes.operatorCreateButton.disabled = !enabled;
}

function setRuntimeProfileFormEnabled(enabled) {
  nodes.runtimeProfileRevisionSelect.disabled = !enabled;
  nodes.runtimeProfileLabel.disabled = !enabled;
  nodes.runtimeProfileDefaultSymbol.disabled = !enabled;
  nodes.runtimeProfileAllowedSymbols.disabled = !enabled;
  nodes.runtimeProfileProductType.disabled = !enabled;
  nodes.runtimeProfileMarginMode.disabled = !enabled;
  nodes.runtimeProfileDefaultOrderQty.disabled = !enabled;
  nodes.runtimeProfileMaxNotional.disabled = !enabled;
  nodes.runtimeProfileMaxPositionQty.disabled = !enabled;
  nodes.runtimeProfileMaxOpenOrders.disabled = !enabled;
  nodes.runtimeProfileDefaultLeverage.disabled = !enabled;
  nodes.runtimeProfileMaxLeverage.disabled = !enabled;
  nodes.runtimeProfileShortBias.disabled = !enabled;
  nodes.runtimeProfileDynamicLeverage.disabled = !enabled;
  nodes.runtimeProfileActivationNote.disabled = !enabled;
  nodes.runtimeProfileCreateButton.disabled = !enabled;
  nodes.runtimeProfileSaveButton.disabled = !enabled;
  nodes.runtimeProfileStageButton.disabled = !enabled;
  nodes.runtimeProfileCancelPendingButton.disabled = !enabled;
  nodes.runtimeProfileRestartButton.disabled = !enabled;
}

function selectedRuntimeProfileRevision() {
  const revisionId = nodes.runtimeProfileRevisionSelect.value;
  return (state.data.runtimeProfiles?.revisions || []).find((revision) => revision.revision_id === revisionId) || null;
}

function populateRuntimeProfileDraftForm() {
  const revision = selectedRuntimeProfileRevision();
  const payload = revision?.payload || state.data.runtimeProfiles?.current_runtime_payload || {};
  nodes.runtimeProfileLabel.value = revision?.profile_label || "";
  nodes.runtimeProfileDefaultSymbol.value = payload.default_symbol || "";
  nodes.runtimeProfileAllowedSymbols.value = (payload.allowed_symbols || []).join(",");
  nodes.runtimeProfileProductType.value = payload.trading_product_type || "spot";
  nodes.runtimeProfileMarginMode.value = payload.margin_mode || "cash";
  nodes.runtimeProfileDefaultOrderQty.value = payload.default_order_qty ?? "";
  nodes.runtimeProfileMaxNotional.value = payload.max_notional_per_symbol ?? "";
  nodes.runtimeProfileMaxPositionQty.value = payload.max_abs_position_qty ?? "";
  nodes.runtimeProfileMaxOpenOrders.value = payload.max_open_orders ?? "";
  nodes.runtimeProfileDefaultLeverage.value = payload.default_target_leverage ?? "";
  nodes.runtimeProfileMaxLeverage.value = payload.max_target_leverage ?? "";
  nodes.runtimeProfileShortBias.checked = Boolean(payload.strategy_short_bias_enabled);
  nodes.runtimeProfileDynamicLeverage.checked = Boolean(payload.strategy_dynamic_leverage_enabled);
  nodes.runtimeProfileActivationNote.value = revision?.activation_note || "";
}

function runtimeProfilePayloadFromForm() {
  return {
    default_symbol: nodes.runtimeProfileDefaultSymbol.value.trim(),
    allowed_symbols: nodes.runtimeProfileAllowedSymbols.value.split(",").map((item) => item.trim()).filter(Boolean),
    trading_product_type: nodes.runtimeProfileProductType.value,
    margin_mode: nodes.runtimeProfileMarginMode.value,
    default_order_qty: Number(nodes.runtimeProfileDefaultOrderQty.value || 0),
    max_notional_per_symbol: Number(nodes.runtimeProfileMaxNotional.value || 0),
    max_abs_position_qty: Number(nodes.runtimeProfileMaxPositionQty.value || 0),
    max_open_orders: Number(nodes.runtimeProfileMaxOpenOrders.value || 0),
    default_target_leverage: Number(nodes.runtimeProfileDefaultLeverage.value || 1),
    max_target_leverage: Number(nodes.runtimeProfileMaxLeverage.value || 1),
    strategy_short_bias_enabled: nodes.runtimeProfileShortBias.checked,
    strategy_dynamic_leverage_enabled: nodes.runtimeProfileDynamicLeverage.checked,
  };
}

function renderRuntimeProfileImpact(revision) {
  const diff = revision.diff || {};
  const postureChange = diff.classification === "product_posture_change" || diff.classification === "account_interpretation_change";
  return `
    <div class="signal-card">
      <div class="signal-head">
        <span class="signal-title">${escapeHtml(revision.profile_label || "-")}</span>
        ${miniBadge(readableState(diff.classification || revision.change_classification || "-"), postureChange ? "warning" : "info")}
      </div>
      <div class="detail-meta">${escapeHtml(revision.revision_id || "-")}</div>
      <div class="signal-copy">${escapeHtml((revision.diff_narrative || ["No human-readable diff available."]).join(" "))}</div>
      ${renderDetailFacts([
        ["Changed Fields", listOrDash(diff.changed_fields)],
        ["Activation Note", revision.activation_note || "-"],
        ["Stage Guard", postureChange ? "Open-order preflight will run before staging." : "Restart-gated parameter update."],
      ])}
    </div>
  `;
}

function renderRuntimeProfileActions(revision) {
  return `
    <div class="table-actions">
      <button class="table-button" data-select-runtime-profile="${escapeHtml(revision.revision_id)}">Edit</button>
      <button class="table-button" data-stage-runtime-profile="${escapeHtml(revision.revision_id)}" ${revision.is_active ? "disabled" : ""}>Stage</button>
    </div>
  `;
}

async function createRuntimeProfileDraft() {
  const profileLabel = nodes.runtimeProfileLabel.value.trim() || "Runtime profile draft";
  try {
    const response = await requestJson("/runtime-profiles/drafts", {
      method: "POST",
      body: { profile_label: profileLabel },
    });
    flash(`Draft ${response.revision.profile_label} created.`, "info");
    await refreshDashboard({ manual: true });
    nodes.runtimeProfileRevisionSelect.value = response.revision.revision_id;
    populateRuntimeProfileDraftForm();
    setActiveView("runtime-profiles");
  } catch (error) {
    flash(`Draft creation failed: ${normalizeError(error, "Runtime profile draft creation failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
}

async function saveRuntimeProfileDraft() {
  const revision = selectedRuntimeProfileRevision();
  if (!revision) {
    flash("Create or select a runtime profile draft first.", "warning");
    renderAlerts();
    return;
  }
  try {
    await requestJson(`/runtime-profiles/revisions/${encodeURIComponent(revision.revision_id)}`, {
      method: "PATCH",
      body: {
        profile_label: nodes.runtimeProfileLabel.value.trim(),
        activation_note: nodes.runtimeProfileActivationNote.value.trim() || null,
        payload: runtimeProfilePayloadFromForm(),
      },
    });
    flash("Runtime profile draft saved.", "info");
    await refreshDashboard({ manual: true });
    nodes.runtimeProfileRevisionSelect.value = revision.revision_id;
    populateRuntimeProfileDraftForm();
  } catch (error) {
    flash(`Runtime profile save failed: ${normalizeError(error, "Runtime profile save failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
}

async function stageRuntimeProfileDraft() {
  const revision = selectedRuntimeProfileRevision();
  if (!revision) {
    flash("Select a runtime profile draft before staging it.", "warning");
    renderAlerts();
    return;
  }
  if (!window.confirm("Stage this runtime profile for activation? A restart will be required before it can take effect.")) {
    return;
  }
  try {
    await requestJson(`/runtime-profiles/revisions/${encodeURIComponent(revision.revision_id)}/stage`, {
      method: "POST",
      body: { activation_note: nodes.runtimeProfileActivationNote.value.trim() || null },
    });
    flash("Runtime profile staged. Restart the managed API to activate it.", "warning");
    await refreshDashboard({ manual: true });
  } catch (error) {
    flash(`Runtime profile staging failed: ${normalizeError(error, "Runtime profile staging failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
}

async function cancelPendingRuntimeProfile() {
  try {
    await requestJson("/runtime-profiles/pending/cancel", { method: "POST" });
    flash("Pending runtime profile activation canceled.", "info");
    await refreshDashboard({ manual: true });
  } catch (error) {
    flash(`Cancel pending failed: ${normalizeError(error, "Runtime profile cancel failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
}

async function requestRuntimeProfileRestart() {
  if (!window.confirm("Request a managed restart? This is only effective when the API is running under the managed supervisor.")) {
    return;
  }
  try {
    await requestJson("/runtime-profiles/restart", { method: "POST" });
    flash("Managed restart requested.", "warning");
    await refreshDashboard({ manual: true });
  } catch (error) {
    flash(`Restart request failed: ${normalizeError(error, "Restart request failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
}

async function createOperatorUser() {
  const username = nodes.operatorCreateUsername.value.trim();
  const password = nodes.operatorCreatePassword.value;
  const role = nodes.operatorCreateRole.value;
  const enabled = nodes.operatorCreateEnabled.checked;
  if (!username || !password) {
    flash("Username and password are required to create an operator user.", "warning");
    renderAlerts();
    return;
  }
  nodes.operatorCreateButton.disabled = true;
  nodes.operatorCreateButton.textContent = "Creating...";
  try {
    await requestJson("/auth/users", {
      method: "POST",
      body: { username, password, role, enabled },
    });
    nodes.operatorCreateForm.reset();
    nodes.operatorCreateRole.value = "viewer";
    nodes.operatorCreateEnabled.checked = true;
    flash(`Operator user ${username} created.`, "info");
    await refreshDashboard({ manual: true });
  } catch (error) {
    flash(`User creation failed: ${normalizeError(error, "Operator user creation failed").message}`, "danger");
    renderDashboard({ manual: true });
  } finally {
    nodes.operatorCreateButton.disabled = !operatorCanAdmin();
    nodes.operatorCreateButton.textContent = "Create User";
  }
}

async function toggleOperatorUser(username, nextEnabled) {
  if (!username) {
    return;
  }
  const actionLabel = nextEnabled ? "enable" : "disable";
  if (!window.confirm(`${actionLabel === "enable" ? "Enable" : "Disable"} operator user ${username}?`)) {
    return;
  }
  await patchOperatorUser(username, { enabled: nextEnabled }, `Operator user ${username} ${nextEnabled ? "enabled" : "disabled"}.`);
}

async function updateOperatorUserRole(username, currentRole) {
  if (!username) {
    return;
  }
  const nextRole = (window.prompt("Enter the new role: viewer, operator, or admin.", currentRole || "viewer") || "").trim();
  if (!nextRole || nextRole === currentRole) {
    return;
  }
  if (!["viewer", "operator", "admin"].includes(nextRole)) {
    flash("Role must be one of viewer, operator, or admin.", "warning");
    renderAlerts();
    return;
  }
  await patchOperatorUser(username, { role: nextRole }, `Operator user ${username} role updated to ${nextRole}.`);
}

async function resetOperatorUserPassword(username) {
  if (!username) {
    return;
  }
  const password = window.prompt(`Enter a new password for ${username}.`, "");
  if (password === null) {
    return;
  }
  if (!password) {
    flash("Password reset requires a non-empty password.", "warning");
    renderAlerts();
    return;
  }
  await patchOperatorUser(username, { password }, `Password reset for ${username} completed.`);
}

async function deleteOperatorUser(username) {
  if (!username) {
    return;
  }
  if (!window.confirm(`Delete operator user ${username}? This removes the stored login account.`)) {
    return;
  }
  try {
    await requestJson(`/auth/users/${encodeURIComponent(username)}`, { method: "DELETE" });
    flash(`Operator user ${username} deleted.`, "info");
    await refreshDashboard({ manual: true });
  } catch (error) {
    flash(`User deletion failed: ${normalizeError(error, "Operator user deletion failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
}

async function patchOperatorUser(username, payload, successMessage) {
  try {
    await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
      method: "PATCH",
      body: payload,
    });
    flash(successMessage, "info");
    await refreshDashboard({ manual: true });
  } catch (error) {
    flash(`User update failed: ${normalizeError(error, "Operator user update failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
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
        detailCard("Mismatch Summary", detail.mismatch_summary || {}, {
          narrative: reconciliationNarrative(detail),
          facts: [
            ["Mismatch Categories", listOrDash(detail.reconciliation?.mismatch_categories)],
            ["Mismatch Reasons", listOrDash(detail.reconciliation?.mismatch_reasons)],
            ["Recommended Action", detail.reconciliation?.recommended_operator_action || "-"],
          ],
        }),
        detailCard("Reconciliation Payload", detail.reconciliation || {}),
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
      detailCard("System Posture", { health: state.data.health || {}, mode: state.data.mode || {} }, {
        narrative: systemNarrative(),
        facts: [
          ["Runtime Profile", readableMode(state.data.mode?.runtime_profile?.name)],
          ["Product Type", readableMode(state.data.mode?.environment_capabilities?.product_type)],
          ["Execution Route", state.data.mode?.execution_route || "-"],
          ["Blockers", String((state.data.blockers?.blockers || []).length)],
        ],
      }),
      detailCard("Recovery", state.data.systemRecovery || {}, {
        narrative: recoveryNarrative(),
      }),
      detailCard("Blockers", state.data.blockers || {}, {
        narrative: blockerNarrative(state.data.blockers?.blockers || []),
      }),
      detailCard("Account", state.data.accountState || {}, {
        narrative: accountNarrative(state.data.accountState || {}),
      }),
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
      detailCard("Recovery Summary", state.data.systemRecovery || {}, {
        narrative: recoveryNarrative(),
        facts: [
          ["Resume Blockers", listOrDash(recovery.resume_blocked_reasons)],
          ["Last Resume Status", readableState(recovery.last_resume_status)],
          ["Last Rebaseline", formatMaybeTimestamp(recovery.last_rebaseline_at)],
        ],
      }),
      detailCard("Baseline Takeover", baseline || {}, {
        narrative: baselineNarrative(baseline || {}),
      }),
      detailCard("Account State", state.data.accountState || {}, {
        narrative: accountNarrative(state.data.accountState || {}),
      }),
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
      detailCard("Runtime Summary", state.data.runtime || {}, {
        narrative: runtimeNarrative(),
        facts: [
          ["Profile", readableMode(state.data.runtime?.runtime_profile?.name || state.data.mode?.runtime_profile?.name)],
          ["Product", readableMode(state.data.runtime?.environment_capabilities?.product_type || state.data.mode?.environment_capabilities?.product_type)],
          ["Margin", readableMode(state.data.runtime?.environment_capabilities?.margin_model || state.data.mode?.environment_capabilities?.margin_model)],
          ["Directionality", readableMode(state.data.runtime?.environment_capabilities?.position_directionality || state.data.mode?.environment_capabilities?.position_directionality)],
        ],
      }),
      detailCard("Recovery", state.data.systemRecovery || {}, {
        narrative: recoveryNarrative(),
      }),
      detailCard("Metrics", state.data.metrics || {}, {
        narrative: metricsNarrative(state.data.metrics || {}),
      }),
      detailCard("Replay", state.data.replayStatus || {}, {
        narrative: replayNarrative(state.data.replayStatus || {}),
      }),
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
      detailCard("Portfolio Snapshot", state.data.portfolio?.portfolio || {}, {
        narrative: portfolioNarrative(state.data.portfolio?.portfolio || {}),
        facts: portfolioDetailFacts(state.data.portfolio?.portfolio || {}),
      }),
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
          detailCard("Decision Summary", detail, {
            narrative: decisionNarrative(detail),
            facts: [
              ["Position Intent", readableMode(detail.position_target?.position_intent)],
              ["Exposure Side", readableMode(detail.position_target?.target_exposure_side)],
              ["Risk Rejections", listOrDash(detail.risk_decision?.rejection_reasons)],
              ["Execution Outcome", decisionExecutionOutcome(detail)],
            ],
          }),
          detailCard("Decision Context", detail.decision_context || {}, {
            narrative: decisionContextNarrative(detail.decision_context || {}),
          }),
          detailCard("Baseline Assessment", detail.baseline_assessment || {}, {
            narrative: baselineAssessmentNarrative(detail.baseline_assessment || {}),
          }),
          detailCard("AI Assessment", detail.ai_assessment || {}, {
            narrative: aiAssessmentNarrative(detail.ai_assessment || {}),
          }),
          detailCard("Target / Policy / Risk", {
            position_target: detail.position_target || null,
            policy_decision: detail.policy_decision || null,
            risk_decision: detail.risk_decision || null,
          }, {
            narrative: targetPolicyRiskNarrative(detail),
          }),
          detailCard("Execution Chain", {
            execution_plan: detail.execution_plan || null,
            order_intents: detail.order_intents || [],
            order_updates: detail.order_updates || [],
            fills: detail.fills || [],
            portfolio_snapshot: detail.portfolio_snapshot || null,
            reconciliations: detail.reconciliations || [],
          }, {
            narrative: executionChainNarrative(detail),
          }),
          detailCard("Audit", detail.audit || {}, {
            narrative: auditNarrative(detail.audit || {}),
          }),
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
          detailCard("Order Summary", detail.order || {}, {
            narrative: orderNarrative(detail.order || {}, detail.fills || []),
            facts: [
              ["Position Intent", detail.order?.submission_payload?.positionIntent || "-"],
              ["Exchange Order ID", detail.order?.exchange_order_id || "-"],
              ["Filled Qty", formatNumber(detail.order?.filled_qty)],
              ["Average Fill", formatNumber(detail.order?.average_fill_price)],
            ],
          }),
          detailCard("Linked Fills", detail.fills || [], {
            narrative: linkedFillsNarrative(detail.fills || []),
          }),
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
          detailCard("Fill Summary", detail.fill || {}, {
            narrative: fillNarrative(detail.fill || {}),
            facts: [
              ["Intent", readableMode(detail.fill?.position_intent)],
              ["Exposure Side", readableMode(detail.fill?.exposure_side)],
              ["Fee Currency", detail.fill?.fee_currency || "-"],
              ["Occurred", formatMaybeTimestamp(detail.fill?.exchange_timestamp)],
            ],
          }),
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
  const outcome = decisionActivityLabel(latestDecision, latestOrder, latestFill, decisionOutcomeLabel(summary));

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
          ["Position Intent", target?.position_intent || "-"],
          ["Exposure Side", readableMode(target?.target_exposure_side || "-")],
          ["Product", readableMode(target?.product_type || latestDecision.decision_context?.product_type || "-")],
          ["Margin", readableMode(target?.margin_mode || "-")],
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
          ["Product", readableMode(context.product_type)],
          ["Exposure Side", readableMode(context.current_exposure_side)],
          ["Target Leverage", formatNumber(target.target_leverage)],
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

function decisionNarrative(detail) {
  const context = detail.decision_context || {};
  const target = detail.position_target || {};
  const policy = detail.policy_decision || {};
  const risk = detail.risk_decision || {};
  const summary = detail.summary || {};
  return [
    `${context.symbol || "This symbol"} was evaluated on the ${context.timeframe || "-"} timeframe at ${formatMaybeTimestamp(context.as_of_ts)}.`,
    `The system wanted to ${readableMode(target.position_intent || "hold")} and move exposure from ${formatSigned(target.current_position_qty)} to ${formatSigned(target.target_position_qty)}.`,
    policy.execution_allowed
      ? risk.approved
        ? `Both policy and risk approved the action. ${decisionExecutionOutcome(detail)}`
        : `Policy allowed the idea, but risk blocked it because of ${listOrDash(risk.rejection_reasons)}.`
      : `Policy blocked the action because of ${listOrDash(policy.rejection_reasons)}.`,
  ];
}

function decisionContextNarrative(context) {
  return [
    `The runtime was in ${readableMode(context.mode)} mode with current exposure ${readableMode(context.current_exposure_side)} ${formatSigned(context.current_position_qty)}.`,
    `This decision used product type ${readableMode(context.product_type)} and current target leverage ${formatNumber(context.current_target_leverage)}.`,
  ];
}

function baselineAssessmentNarrative(baseline) {
  return [
    `The baseline model saw the regime as ${readableMode(baseline.regime)} with a ${readableMode(baseline.direction_bias)} bias.`,
    `Confidence was ${formatNumber(baseline.confidence)} and suggested position scale was ${formatNumber(baseline.suggested_position_scale)}.`,
    `Main reason codes: ${listOrDash(baseline.reason_codes)}.`,
  ];
}

function aiAssessmentNarrative(assessment) {
  return [
    `AI mode was ${readableMode(assessment.operating_mode)}.`,
    assessment.fallback_used
      ? `The system used a fallback assessment because ${readableMode(assessment.fallback_reason)}.`
      : `The provider response was accepted with calibrated confidence ${formatNumber(assessment.calibrated_confidence)}.`,
    `Directional edge was ${formatSigned(assessment.directional_edge)} with expected volatility ${formatNumber(assessment.expected_volatility)}.`,
  ];
}

function targetPolicyRiskNarrative(detail) {
  const target = detail.position_target || {};
  const policy = detail.policy_decision || {};
  const risk = detail.risk_decision || {};
  return [
    `Target intent was ${readableMode(target.position_intent)} on ${readableMode(target.product_type)} with ${readableMode(target.margin_mode)} margin semantics.`,
    `Policy ${policy.execution_allowed ? "allowed" : "blocked"} the action. Risk ${risk.approved ? "approved" : "blocked"} it.`,
    `Risk constraints applied: ${listOrDash(risk.constraints_applied)}.`,
  ];
}

function executionChainNarrative(detail) {
  const intents = detail.order_intents || [];
  const orders = detail.order_updates || [];
  const fills = detail.fills || [];
  const reconciliations = detail.reconciliations || [];
  return [
    `This decision produced ${intents.length} intent${pluralize(intents.length)}, ${orders.length} order update${pluralize(orders.length)}, and ${fills.length} fill${pluralize(fills.length)}.`,
    fills.length
      ? `A portfolio snapshot and reconciliation were generated after execution. Reconciliation count: ${reconciliations.length}.`
      : `No fill was ingested yet, so downstream portfolio and reconciliation effects may still be pending.`,
  ];
}

function auditNarrative(audit) {
  return [
    `Audit links the full chain from decision context through execution and reconciliation for decision ${audit.decision_id || "-"}.`,
    `Linked refs: ${audit.order_intent_refs?.length || 0} intent${pluralize(audit.order_intent_refs?.length || 0)}, ${audit.order_state_refs?.length || 0} order update${pluralize(audit.order_state_refs?.length || 0)}, ${audit.fill_event_refs?.length || 0} fill${pluralize(audit.fill_event_refs?.length || 0)}.`,
  ];
}

function orderNarrative(order, fills) {
  return [
    `This order was sent as a ${order.submission_payload?.ordType || "market"} ${order.submission_payload?.side || "-"} on ${order.symbol || "-"} through ${order.venue || "-"}.`,
    `Current lifecycle state is ${readableMode(order.status)} with requested quantity ${formatNumber(order.requested_qty)} and filled quantity ${formatNumber(order.filled_qty)}.`,
    fills.length
      ? `There are ${fills.length} linked fill${pluralize(fills.length)} for this order.`
      : `No linked fills are stored for this order yet.`,
  ];
}

function linkedFillsNarrative(fills) {
  if (!fills.length) {
    return ["No fills have been linked to this order yet."];
  }
  const totalQty = fills.reduce((sum, fill) => sum + Number(fill.fill_qty || 0), 0);
  return [
    `${fills.length} fill${pluralize(fills.length)} are linked to this order.`,
    `Total executed quantity across linked fills is ${formatNumber(totalQty)}.`,
  ];
}

function fillNarrative(fill) {
  return [
    `This fill executed a ${fill.side || "-"} on ${fill.symbol || "-"} at ${formatNumber(fill.fill_price)} for quantity ${formatNumber(fill.fill_qty)}.`,
    `It represents ${readableMode(fill.position_intent)} on ${readableMode(fill.product_type)} and affects ${readableMode(fill.exposure_side)} exposure.`,
    `Fees were charged as ${formatNumber(fill.fee_amount)} ${fill.fee_currency || ""}`.trim(),
  ];
}

function reconciliationNarrative(detail) {
  const reconciliation = detail.reconciliation || {};
  const severity = readableMode(reconciliation.severity);
  if (!reconciliation.mismatch_categories?.length) {
    return [
      `This reconciliation report is ${severity}. The system did not find any mismatch that currently requires intervention.`,
      reconciliation.exchange_comparison_enabled
        ? "Exchange comparison was enabled for this report."
        : "This report focused on local reconstruction consistency rather than exchange-side comparison.",
    ];
  }
  return [
    `This reconciliation report is ${severity} and found ${reconciliation.mismatch_categories.length} mismatch category${pluralize(reconciliation.mismatch_categories.length)}.`,
    `Main mismatch reasons: ${listOrDash(reconciliation.mismatch_reasons)}.`,
    `Recommended operator action: ${reconciliation.recommended_operator_action || "review the mismatch payload"}.`,
  ];
}

function systemNarrative() {
  const health = state.data.health || {};
  const mode = state.data.mode || {};
  return [
    `The runtime is currently ${readableMode(health.runtime_state)} in ${readableMode(mode.operating_state)}.`,
    health.execution_blocked
      ? `Execution is blocked because of ${listOrDash((state.data.blockers?.blockers || []).map((item) => item.blocker))}.`
      : "Execution is currently allowed. There are no active blockers stopping the mainline.",
  ];
}

function recoveryNarrative() {
  const recovery = recoveryData();
  return [
    `Recovery state is ${readableMode(recovery.recovery_state)}.`,
    recovery.safe_to_trade
      ? "The runtime currently considers trading safe."
      : "The runtime does not currently consider trading safe.",
    recovery.review_required
      ? `Operator review is still required because of ${listOrDash(recovery.resume_blocked_reasons)}.`
      : `Resume blockers: ${listOrDash(recovery.resume_blocked_reasons)}.`,
  ];
}

function baselineNarrative(baseline) {
  return [
    `Baseline status is ${readableMode(baseline.status || baseline.baseline_status)}.`,
    `It was created from ${readableMode(baseline.baseline_kind || baseline.baseline_source || "current account state")}.`,
  ];
}

function accountNarrative(account) {
  return [
    `Account backend is ${readableMode(account.backend || account.account_source || "-")} and freshness is ${booleanWord(account.fresh)}.`,
    `Current account read posture is ${account.ready ? "ready" : "not ready"} with blockers ${listOrDash(account.blockers)}.`,
  ];
}

function runtimeNarrative() {
  const runtime = state.data.runtime || {};
  const mode = state.data.mode || {};
  const environment = runtime.environment_capabilities || mode.environment_capabilities || {};
  return [
    `Runtime profile is ${readableMode(runtime.runtime_profile?.name || mode.runtime_profile?.name)}.`,
    `Execution route is ${environment.execution_route || mode.execution_route || "-"} with ${readableMode(environment.product_type)} / ${readableMode(environment.margin_model)} semantics.`,
    `Directionality is ${readableMode(environment.position_directionality)} and leverage support is ${readableMode(environment.leverage_support)}.`,
  ];
}

function metricsNarrative(metrics) {
  return [
    `The runtime has processed ${formatNumber(metrics.decision_cycle_count)} decision cycles and emitted ${formatNumber(metrics.order_intent_count)} order intents.`,
    `It has ingested ${formatNumber(metrics.fill_count)} fills and recorded ${formatNumber(metrics.rejection_count)} rejection${pluralize(metrics.rejection_count)}.`,
  ];
}

function replayNarrative(replayStatus) {
  return [
    replayStatus.last_validation
      ? `The most recent replay validation ran at ${formatMaybeTimestamp(replayStatus.last_validation.validated_at)}.`
      : "No replay validation has been run recently.",
    `Recent validations stored: ${formatNumber((replayStatus.recent_validations || []).length)}.`,
  ];
}

function portfolioNarrative(portfolio) {
  const primary = trackedPortfolioPosition(portfolio);
  return [
    `Total equity is ${formatNumber(portfolio.total_equity)} with gross exposure ${formatNumber(portfolio.gross_exposure)} and net exposure ${formatSigned(portfolio.net_exposure)}.`,
    primary
      ? `Primary tracked position is ${readableMode(primary.exposure_side)} ${formatSigned(primary.position_qty)} on ${primary.symbol}.`
      : "No active tracked position is currently stored in the portfolio snapshot.",
  ];
}

function portfolioDetailFacts(portfolio) {
  const primary = trackedPortfolioPosition(portfolio);
  return [
    ["Primary Symbol", primary?.symbol || "-"],
    ["Primary Side", readableMode(primary?.exposure_side || "-")],
    ["Primary Qty", primary ? formatSigned(primary.position_qty) : "-"],
    ["Margin Usage", formatNumber(portfolio.margin_usage)],
    ["Tracked Positions", String((portfolio.positions || []).length)],
  ];
}

function blockerNarrative(blockers) {
  if (!blockers.length) {
    return ["There are no current blocker records on the runtime."];
  }
  return [
    `${blockers.length} blocker${pluralize(blockers.length)} are currently recorded.`,
    `Most important blocker: ${blockers[0].blocker}. Recommended action: ${blockers[0].recommended_action}.`,
  ];
}

function decisionExecutionOutcome(detail) {
  const summary = detail.summary || {};
  const result = summary.execution_result || {};
  if (result.fill_count > 0) {
    return `${result.fill_count} fill${pluralize(result.fill_count)} were ingested for this decision.`;
  }
  if (result.order_count > 0) {
    return `${result.order_count} order${pluralize(result.order_count)} were created but no fill has been ingested yet.`;
  }
  return "No execution was created for this decision.";
}

function buildTimeline({ paperLocal = false } = {}) {
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

  if (!paperLocal && latestRebaselineAction) {
    items.push({
      title: "Operator rebaseline",
      subtitle: latestRebaselineAction.status || "-",
      timestamp: latestRebaselineAction.created_at,
      tone: recovery.review_required ? "warning" : latestRebaselineAction.status === "rebaseline_completed" ? "info" : "danger",
      detail: latestRebaselineAction.reason || "Operator accepted a new trusted baseline.",
    });
  }

  if (!paperLocal && latestResumeAction) {
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
          ${renderDetailNarrative(section.narrative || [])}
          ${renderDetailFacts(section.facts || [])}
          ${renderRawDetail(section.value)}
        </section>
      `).join("")
    : `<div class="empty-state">No detail sections available.</div>`;
  setDrawerContent(title, summaryHtml, bodyHtml);
  openDrawer();
}

function detailCard(title, value, options = {}) {
  return {
    title,
    value,
    narrative: options.narrative || [],
    facts: options.facts || [],
  };
}

function renderDetailNarrative(lines) {
  if (!lines.length) {
    return "";
  }
  return `<div class="detail-prose">${lines.map((line) => `<p>${escapeHtml(line)}</p>`).join("")}</div>`;
}

function renderDetailFacts(facts) {
  if (!facts.length) {
    return "";
  }
  return `<div class="detail-grid">${facts.map(([key, value]) => `
    <div class="detail-grid-row">
      <span class="detail-key">${escapeHtml(key)}</span>
      <strong class="detail-value">${escapeHtml(value == null || value === "" ? "-" : String(value))}</strong>
    </div>
  `).join("")}</div>`;
}

function renderRawDetail(value) {
  if (value == null || (typeof value === "object" && Object.keys(value).length === 0) || (Array.isArray(value) && value.length === 0)) {
    return `<div class="detail-empty-note">No raw payload for this section.</div>`;
  }
  return `
    <details class="detail-raw">
      <summary>Raw JSON</summary>
      <pre class="detail-json">${escapeHtml(JSON.stringify(value, null, 2))}</pre>
    </details>
  `;
}

function recentDecisionHeadline(item) {
  const intent = readableMode(item.position_target?.position_intent || item.position_intent || "hold");
  const symbol = item.symbol || "tracked symbol";
  const delta = Number(item.position_target?.delta_position_qty ?? item.target_delta_qty ?? item.delta_position_qty ?? 0);
  if (Math.abs(delta) < 1e-12 || intent === "hold") {
    return `Hold ${symbol}`;
  }
  const side = delta > 0 ? "increase" : "reduce";
  return `${capitalizeWord(intent)} ${symbol} and ${side} exposure`;
}

function recentDecisionNarrative(item) {
  const target = item.position_target || {};
  const current = formatSigned(target.current_position_qty ?? item.current_position_qty ?? 0);
  const next = formatSigned(target.target_position_qty ?? item.target_position_qty ?? 0);
  return `Exposure would move from ${current} to ${next} with ${readableMode(target.product_type || item.product_type || "-")} semantics.`;
}

function recentDecisionOutcome(item) {
  if (item.policy_result === false) {
    return `Policy stopped the idea because of ${listOrDash(item.policy_rejection_reasons || item.policy_decision?.rejection_reasons)}.`;
  }
  if (item.risk_result === false) {
    return `Risk stopped the idea because of ${listOrDash(item.risk_rejection_reasons || item.risk_decision?.rejection_reasons)}.`;
  }
  const orderCount = Number(item.execution_result?.order_count ?? item.order_count ?? 0);
  const fillCount = Number(item.execution_result?.fill_count ?? item.fill_count ?? 0);
  if (fillCount > 0) {
    return `${fillCount} fill${pluralize(fillCount)} landed from this decision.`;
  }
  if (orderCount > 0) {
    return `${orderCount} order${pluralize(orderCount)} were created and are still syncing.`;
  }
  return "The decision passed safety checks but did not need to place an order.";
}

function recentOrderHeadline(order) {
  const side = readableMode(order.submission_payload?.side || order.side || "-");
  const type = readableMode(order.submission_payload?.ordType || order.order_type || "market");
  return `${capitalizeWord(side)} ${formatNumber(order.requested_qty)} ${order.symbol || "-" } as a ${type} order`;
}

function recentOrderNarrative(order) {
  const intent = readableMode(order.submission_payload?.positionIntent || order.position_intent || "-");
  return `This order carries ${intent} intent through ${order.venue || order.submission_mode || "-"} on ${readableMode(order.product_type || "-")} rules.`;
}

function recentOrderStateSummary(order) {
  return `${order.venue || order.submission_mode || "-"} | filled ${formatNumber(order.filled_qty)} of ${formatNumber(order.requested_qty)}`;
}

function recentFillHeadline(fill) {
  const side = readableMode(fill.side || "-");
  return `${capitalizeWord(side)} ${formatNumber(fill.fill_qty)} ${fill.symbol || "-"}`;
}

function recentFillNarrative(fill) {
  return `Executed at ${formatNumber(fill.fill_price)} with ${readableMode(fill.position_intent || "-")} intent on ${readableMode(fill.product_type || "-")}.`;
}

function recentFillImpactSummary(fill) {
  return `${formatNumber(fill.fill_qty)} @ ${formatNumber(fill.fill_price)} | ${readableMode(fill.exposure_side || "flat")}`;
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

function trackedSymbol() {
  return state.data.latestDecision?.decision_context?.symbol
    || state.data.latestFill?.symbol
    || state.data.executionLatest?.latest_fill?.symbol
    || state.data.executionLatest?.latest_order?.symbol
    || state.data.runtime?.symbols?.[0]
    || null;
}

function trackedPortfolioPosition(portfolio) {
  if (!portfolio?.positions?.length) {
    return null;
  }
  const symbol = trackedSymbol();
  return portfolio.positions.find((position) => position.symbol === symbol) || portfolio.positions[0] || null;
}

function positionPosture(position) {
  if (!position) {
    return "flat";
  }
  return `${readableMode(position.exposure_side || "flat")} ${formatNumber(position.position_qty)}`;
}

function fillFreshnessLabel(fill) {
  if (!fill?.ingestion_timestamp) {
    return "-";
  }
  return `${formatMaybeTimestamp(fill.ingestion_timestamp)} | ${formatRelativeAge(fill.ingestion_timestamp)}`;
}

function decisionActivityLabel(latestDecision, latestOrder, latestFill, fallback = "No decision outcome.") {
  const summary = latestDecision?.summary || {};
  const result = summary.execution_result || {};
  const target = latestDecision?.position_target || {};
  if (result.fill_count > 0 && latestFill) {
    return `Latest decision ${readableMode(target.position_intent || "executed")} and produced a fill ${formatRelativeAge(latestFill.ingestion_timestamp)}.`;
  }
  if (result.order_count > 0 && latestOrder) {
    return `Latest decision submitted ${readableMode(target.position_intent || "an order")}; awaiting or syncing execution state.`;
  }
  if (target.position_intent === "hold" || Math.abs(Number(target.delta_position_qty || 0)) < 1e-12) {
    return "Latest decision is holding current posture. No trade was required.";
  }
  if (summary.risk_result === false) {
    return "Latest decision was blocked by risk controls.";
  }
  if (summary.policy_result === false) {
    return "Latest decision was blocked by policy controls.";
  }
  return fallback;
}

function runtimeProfileData() {
  return state.data.runtime?.runtime_profile || state.data.mode?.runtime_profile || state.data.health?.runtime_profile || {};
}

function environmentData() {
  return state.data.runtime?.environment_capabilities || state.data.mode?.environment_capabilities || state.data.health?.environment_capabilities || {};
}

function recoveryPolicyData() {
  return state.data.runtime?.recovery_policy || state.data.mode?.recovery_policy || state.data.health?.recovery_policy || {};
}

function applyOverviewProfile(runtimeProfile) {
  const paperLocal = runtimeProfile.name === "paper_local";
  nodes.overviewPostureTitle.textContent = paperLocal ? "Local Paper Posture" : "System Posture";
  nodes.overviewPostureCopy.textContent = paperLocal
    ? "Shared decision and risk core with local paper execution. Exchange repair controls stay out of the landing view."
    : "Health, readiness, and execution gating.";
  nodes.overviewRecoveryTitle.textContent = "Recovery Control";
  nodes.overviewRecoveryCopy.textContent = "Baseline takeover, review posture, and whether resume is actually safe.";
  nodes.overviewRecoveryPanel.hidden = paperLocal;
  nodes.inspectRecoveryButton.hidden = paperLocal;
}

function executionHeadline(runtimeProfile, environment, mode) {
  if (runtimeProfile.name === "paper_local") {
    return "Local Paper Execution";
  }
  if (environment.exchange_submission_enabled) {
    return "Exchange Submit Enabled";
  }
  return mode.exchange_submit_allowed ? "Exchange Armed" : "Guarded Exchange";
}

function executionModeBadge(environment, mode) {
  if (!environment.exchange_coupled) {
    return "paper fills";
  }
  return mode.guarded_execution_dry_run ? "dry run" : environment.exchange_submission_enabled ? "submit enabled" : "guarded";
}

function executionModeTone(environment, mode) {
  if (!environment.exchange_coupled) {
    return "outline";
  }
  return mode.guarded_execution_dry_run ? "warning" : environment.exchange_submission_enabled ? "success" : "info";
}

function executionCopy(runtimeProfile, environment, mode) {
  if (runtimeProfile.name === "paper_local") {
    return "Orders stay inside the local paper adapter in this profile. No exchange submission occurs.";
  }
  return mode.exchange_submit_allowed ? "All exchange submission gates are clear." : listOrDash(mode.submit_blocked_reasons);
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

function formatRelativeAge(value) {
  if (!value) return "-";
  const deltaSeconds = Math.max(0, Math.round((Date.now() - dateValue(value)) / 1000));
  if (!Number.isFinite(deltaSeconds)) return "-";
  if (deltaSeconds < 60) return `${deltaSeconds}s ago`;
  if (deltaSeconds < 3600) return `${Math.floor(deltaSeconds / 60)}m ago`;
  if (deltaSeconds < 86400) return `${Math.floor(deltaSeconds / 3600)}h ago`;
  return `${Math.floor(deltaSeconds / 86400)}d ago`;
}

function pluralize(count) {
  return Number(count) === 1 ? "" : "s";
}

function capitalizeWord(value) {
  const text = value == null ? "" : String(value);
  return text ? `${text.charAt(0).toUpperCase()}${text.slice(1)}` : "-";
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
