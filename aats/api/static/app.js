const AUTO_REFRESH_MS = 15000;
const BACKGROUND_REFRESH_MS = 60000;
const CORE_PANEL_SPECS = [
  ["session", "/auth/session"],
  ["authProviders", "/auth/providers"],
  ["health", "/system/health"],
  ["mode", "/system/mode"],
  ["runtime", "/system/runtime"],
  ["systemRecovery", "/system/recovery"],
];
const VIEW_PANEL_SPECS = {
  overview: [
    ["blockers", "/system/blockers"],
    ["metrics", "/system/metrics"],
    ["portfolio", "/portfolio/latest"],
    ["latestDecision", "/decision/latest"],
    ["executionLatest", "/execution/latest"],
    ["reconciliationLatest", "/reconciliation/latest"],
    ["accountState", "/account/state"],
  ],
  decisions: [
    ["latestDecision", "/decision/latest"],
    ["recentDecisions", "/decision/recent?limit=8"],
  ],
  execution: [
    ["metrics", "/system/metrics"],
    ["latestDecision", "/decision/latest"],
    ["executionLatest", "/execution/latest"],
    ["recentOrders", "/orders/recent?limit=8"],
    ["recentFills", "/fills/recent?limit=8"],
    ["executionErrors", "/execution/errors"],
  ],
  diagnostics: [
    ["blockers", "/system/blockers"],
    ["metrics", "/system/metrics"],
    ["latestDecision", "/decision/latest"],
    ["executionLatest", "/execution/latest"],
    ["reconciliationLatest", "/reconciliation/latest"],
    ["replayStatus", "/replay/status"],
    ["accountState", "/account/state"],
  ],
  "runtime-profiles": [],
  operators: [],
};
const VIEW_REQUIRED_KEYS = {
  overview: ["blockers", "metrics", "portfolio", "latestDecision", "executionLatest", "reconciliationLatest", "accountState"],
  decisions: ["latestDecision", "recentDecisions"],
  execution: ["metrics", "latestDecision", "executionLatest", "recentOrders", "recentFills", "executionErrors"],
  diagnostics: ["blockers", "metrics", "latestDecision", "executionLatest", "reconciliationLatest", "replayStatus", "accountState"],
  "runtime-profiles": [],
  operators: [],
};

const state = {
  activeView: "overview",
  refreshing: false,
  refreshTimer: null,
  lastRefreshAt: null,
  flashMessage: null,
  panelErrors: {},
  data: {},
};

const UI_TERM_MAP = {
  anonymous: "未登录",
  authenticated: "已登录",
  unknown: "未知",
  ok: "正常",
  healthy: "健康",
  degraded: "降级",
  blocked: "阻断",
  halted: "已暂停",
  ready: "就绪",
  enabled: "已启用",
  disabled: "未启用",
  active: "生效中",
  pending: "待处理",
  recovered: "已恢复",
  review_required: "需人工复核",
  resume_blocked: "恢复受阻",
  normal_operation: "正常运行",
  rebaseline_completed: "基线已重建",
  submitted: "已提交",
  submitting: "提交中",
  created: "已创建",
  filled: "已成交",
  partially_filled: "部分成交",
  canceled: "已撤单",
  cancel_pending: "撤单中",
  failed: "失败",
  rejected: "已拒绝",
  local: "本地",
  exchange: "交易所",
  paper_local: "本地模拟",
  exchange_simulated_derivatives: "交易所模拟合约",
  guarded_live: "受保护运行",
  derivatives: "合约",
  spot: "现货",
  cross: "全仓",
  isolated: "逐仓",
  cash: "现货现金",
  buy: "买入",
  sell: "卖出",
  market: "市价",
  limit: "限价",
  long: "多头",
  short: "空头",
  flat: "空仓",
  hold: "持有",
  executed: "已执行",
  an_order: "订单",
  open_long: "开多",
  reduce_long: "减多仓",
  close_long: "平多仓",
  open_short: "开空",
  reduce_short: "减空仓",
  close_short: "平空仓",
  reverse_to_long: "反手开多",
  reverse_to_short: "反手开空",
  trend: "趋势",
  breakout: "突破",
  range: "震荡",
  uncertain: "不确定",
  operator: "操作员",
  admin: "管理员",
  viewer: "只读用户",
  session: "会话",
  local_only: "仅本地",
  bi_directional: "双向",
  supported: "支持",
  yes: "是",
  no: "否",
  env_fallback: "环境配置",
  current_account_state: "当前账户状态",
  execution: "执行",
  replay: "回放",
};

const PANEL_LABELS = {
  session: "登录会话",
  authProviders: "认证能力",
  health: "系统健康",
  mode: "运行模式",
  runtime: "运行状态",
  systemRecovery: "系统恢复",
  blockers: "阻断项",
  metrics: "运行指标",
  portfolio: "账户快照",
  latestDecision: "最新决策",
  executionLatest: "执行概况",
  reconciliationLatest: "最新对账",
  accountState: "账户状态",
  recentDecisions: "最近决策",
  recentOrders: "最近订单",
  recentFills: "最近成交",
  executionErrors: "执行异常",
  replayStatus: "回放状态",
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
  nodes.reconcileButton.addEventListener("click", () => void runAction("/reconciliation/validate", { reason: "ui_manual_validate" }, "已发起人工对账检查。"));
  nodes.rebaselineButton.addEventListener("click", () => void runDangerousAction({
    path: "/system/rebaseline",
    body: { reason: "ui_manual_rebaseline" },
    successMessage: "基线重建已完成。当前交易所账户状态已被接受为新的可信基线，系统仍会保持暂停，直到恢复成功。",
    confirmMessage: "现在执行基线重建吗？这会接受当前交易所账户状态为新的可信基线，但不会自动恢复交易。",
  }));
  nodes.resumeButton.addEventListener("click", () => void runAction("/system/resume", { reason: "ui_manual_resume" }, "已发起恢复请求，系统会重新评估当前是否具备交易条件。"));
  nodes.haltButton.addEventListener("click", () => void runDangerousAction({
    path: "/system/halt",
    body: { reason: "ui_manual_halt" },
    successMessage: "系统已安全暂停。",
    confirmMessage: "确认要安全暂停系统并停止交易资格吗？",
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
  document.addEventListener("visibilitychange", () => {
    if (!nodes.autoRefreshToggle.checked) {
      return;
    }
    if (document.hidden) {
      cancelScheduledRefresh();
      scheduleRefresh();
      return;
    }
    void refreshDashboard();
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
    const resolveStuckOrderButton = target.closest("[data-resolve-stuck-order]");
    if (resolveStuckOrderButton instanceof HTMLElement) {
      void resolveStuckOrder(resolveStuckOrderButton.dataset.resolveStuckOrder || "");
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
  renderActiveView();
  if (!viewDataReady(viewName) && !state.refreshing) {
    void refreshDashboard();
  }
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

  const specs = dedupePanelSpecs([
    ...CORE_PANEL_SPECS,
    ...viewPanelSpecs(state.activeView),
  ]);
  const results = await Promise.all(specs.map(([key, path]) => fetchPanel(key, path)));
  applyPanelResults(results);

  if (operatorCanAdmin()) {
    const adminSpecs = [];
    if (state.activeView === "operators") {
      adminSpecs.push(["operatorUsers", "/auth/users"]);
    }
    if (state.activeView === "runtime-profiles" && state.data.authProviders?.runtime_profile_control_enabled === true) {
      adminSpecs.push(["runtimeProfiles", "/runtime-profiles"]);
    }
    if (adminSpecs.length) {
      applyPanelResults(await Promise.all(adminSpecs.map(([key, path]) => fetchPanel(key, path))));
    }
    if (state.activeView !== "operators") {
      delete state.data.operatorUsers;
      delete state.panelErrors.operatorUsers;
    }
    if (state.activeView !== "runtime-profiles" || state.data.authProviders?.runtime_profile_control_enabled !== true) {
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
    message: localizeErrorMessage(error?.message || fallbackMessage),
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
    flash(`操作失败：${normalizeError(error, `调用 ${path} 失败`).message}`, "danger");
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
  nodes.refreshButton.textContent = busy ? "刷新中..." : "立即刷新";
  updateActionAccess();
}

function scheduleRefresh() {
  cancelScheduledRefresh();
  if (!nodes.autoRefreshToggle.checked) {
    return;
  }
  const delay = document.hidden ? BACKGROUND_REFRESH_MS : AUTO_REFRESH_MS;
  state.refreshTimer = window.setTimeout(() => void refreshDashboard(), delay);
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
  renderActiveView();
  nodes.lastRefreshLabel.textContent = state.lastRefreshAt
    ? `上次刷新：${formatDateTime(state.lastRefreshAt)}${manual ? " | 手动" : ""}`
    : "尚未刷新";
}

function viewPanelSpecs(viewName) {
  return VIEW_PANEL_SPECS[viewName] || [];
}

function dedupePanelSpecs(specs) {
  const seen = new Set();
  return specs.filter(([key]) => {
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function applyPanelResults(results) {
  results.forEach((result) => {
    if (result.ok) {
      state.data[result.key] = result.data;
      delete state.panelErrors[result.key];
      return;
    }
    state.panelErrors[result.key] = result.error;
  });
}

function viewDataReady(viewName) {
  const required = VIEW_REQUIRED_KEYS[viewName] || [];
  return required.every((key) => key in state.data || key in state.panelErrors);
}

function renderActiveView() {
  if (state.activeView === "overview") {
    renderOverview();
    return;
  }
  if (state.activeView === "decisions") {
    renderDecisions();
    return;
  }
  if (state.activeView === "execution") {
    renderExecution();
    return;
  }
  if (state.activeView === "diagnostics") {
    renderDiagnostics();
    return;
  }
  if (state.activeView === "runtime-profiles") {
    renderRuntimeProfiles();
    return;
  }
  if (state.activeView === "operators") {
    renderOperators();
  }
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
  nodes.overviewDecisionSpotlight.innerHTML = emptyState("正在等待系统运行数据。");
  nodes.overviewPosture.innerHTML = emptyState("正在等待系统状态数据。");
  nodes.overviewPortfolio.innerHTML = emptyState("正在等待账户与仓位快照。");
  nodes.overviewBlockers.innerHTML = emptyState("暂未获取阻断项数据。");
  nodes.overviewRecovery.innerHTML = emptyState("暂未获取恢复状态。");
  nodes.overviewMetrics.innerHTML = emptyState("暂未获取运行指标。");
  nodes.overviewTimeline.innerHTML = emptyState("暂未获取最近运行记录。");
  nodes.decisionSpotlight.innerHTML = emptyState("暂未获取最新决策。");
  nodes.decisionTable.innerHTML = emptyState("暂未获取最近决策。");
  nodes.executionSpotlight.innerHTML = emptyState("暂未获取执行状态。");
  nodes.orderTable.innerHTML = emptyState("暂未获取最近订单。");
  nodes.fillTable.innerHTML = emptyState("暂未获取最近成交。");
  nodes.executionErrorsPanel.innerHTML = emptyState("暂未获取执行异常。");
  nodes.diagnosticReconciliation.innerHTML = emptyState("暂未获取对账报告。");
  nodes.diagnosticRecovery.innerHTML = emptyState("暂未获取恢复状态。");
  nodes.diagnosticReplay.innerHTML = emptyState("暂未获取回放校验结果。");
  nodes.diagnosticAccount.innerHTML = emptyState("暂未获取账户状态。");
  nodes.diagnosticBlockers.innerHTML = emptyState("暂未获取阻断历史。");
  nodes.diagnosticMetrics.innerHTML = emptyState("暂未获取运行指标。");
  nodes.runtimeProfileSummary.innerHTML = emptyState("暂未获取运行配置状态。");
  nodes.runtimeProfileSupervisor.innerHTML = emptyState("暂未获取托管重启状态。");
  nodes.runtimeProfileImpact.innerHTML = emptyState("暂未获取配置差异说明。");
  nodes.runtimeProfileTable.innerHTML = emptyState("暂未获取运行配置版本。");
  nodes.runtimeProfilePermissionNote.textContent = "正在检查运行配置权限。";
  nodes.operatorSummary.innerHTML = emptyState("暂未获取认证状态。");
  nodes.operatorBootstrap.innerHTML = emptyState("暂未获取初始化状态。");
  nodes.operatorUsersTable.innerHTML = emptyState("暂未获取操作员账户数据。");
  nodes.operatorPermissionNote.textContent = "正在检查管理员权限。";
  nodes.runtimeProfileRevisionSelect.innerHTML = `<option value="">未选择草稿</option>`;
  setRuntimeProfileFormEnabled(false);
  setOperatorCreateFormEnabled(false);
  setDrawerContent("明细查看", `<div class="empty-state">点击表格中的查看按钮，或输入编号后打开详情。</div>`, `<div class="empty-state">选中具体项目后，这里会显示完整明细。</div>`);
}

function updateAuthStateChip() {
  const session = state.data.session || {};
  const denied = Object.values(state.panelErrors).some((error) => error?.status === 401 || error?.status === 403);
  let text = "未登录";
  let tone = "outline";
  if (denied) {
    text = "访问被拒绝";
    tone = "danger";
  } else if (session.authenticated) {
    text = session.auth_source === "session" ? "会话有效" : readableState(session.auth_source || "authenticated");
    tone = "success";
  } else if (session.auth_enabled) {
    text = "需要登录";
    tone = "warning";
  } else {
    text = "本地访问";
    tone = "neutral";
  }
  setStatusChip(nodes.authStateChip, text, tone);
  nodes.sessionIdentityValue.textContent = session.identity || "当前未建立会话";
  nodes.sessionRoleValue.textContent = `角色：${readableState(session.role || "anonymous")}`;
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
      return `当前会话角色为${readableState(session.role || "operator")}，允许执行控制操作。`;
    }
    return "当前是只读会话。需要操作员或管理员权限才能执行控制操作。";
  }
  if (operatorAuth.unsafe_write_without_auth) {
    return "当前是本地开发模式，未登录也允许执行写入操作。";
  }
  return "当前已锁定写入操作。只有配置好操作员认证，或显式开启本地不安全写入后才可操作。";
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
      ? "允许提交"
      : environment.exchange_submission_possible
      ? "受保护提交通道"
      : "本地模拟",
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
  nodes.stripOverallMeta.textContent = health.execution_blocked ? listOrDash(health.submit_blocked_reasons || health.blockers?.map((item) => item.blocker)) : "当前没有阻断项";

  nodes.stripModeValue.textContent = readableMode(runtimeProfile.name || mode.operating_state || mode.mode || "unknown");
  nodes.stripModeMeta.textContent = `${readableMode(mode.operating_state || "-")} | ${readableMode(environment.product_type || mode.trading_product_type || "-")} | ${readableMode(environment.margin_model || mode.margin_mode || "-")}`;

  nodes.stripExecutionValue.textContent = environment.exchange_submission_enabled
    ? "交易所提交通道已开启"
    : environment.exchange_submission_possible
    ? "受保护提交通道"
    : "本地模拟";
  nodes.stripExecutionMeta.textContent = environment.exchange_submission_enabled
    ? `${environment.exchange_submission_target || "交易所"} | ${readableMode(environment.position_directionality)} | 最大杠杆 ${formatNumber(mode.max_target_leverage || 1)}`
    : `${listOrDash(mode.submit_blocked_reasons)} | ${readableMode(environment.position_directionality)}`;

  if (runtimeProfile.name === "paper_local") {
    nodes.stripRecoveryValue.textContent = "本地模拟";
    nodes.stripRecoveryMeta.textContent = recovery.safe_to_trade
      ? "当前运行姿态不需要交易所基线接管"
      : recoverySummaryLine(recovery);
  } else {
    nodes.stripRecoveryValue.textContent = readableState(recovery.recovery_state);
    nodes.stripRecoveryMeta.textContent = recoverySummaryLine(recovery);
  }

  nodes.stripFreshnessValue.textContent = freshnessSummary(freshness);
  nodes.stripFreshnessMeta.textContent = `行情 ${booleanWord(freshness.market_fresh)} | 账户 ${booleanWord(account.fresh)} | 对账 ${booleanWord(freshness.reconciliation_fresh)}`;

  nodes.stripEquityValue.textContent = portfolio ? formatNumber(portfolio.total_equity) : "-";
  nodes.stripEquityMeta.textContent = portfolio
    ? `总敞口 ${formatNumber(portfolio.gross_exposure)} | ${positionPosture(primaryPosition)}`
    : "暂未获取账户快照";
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
    banners.push({ tone: "danger", message: "系统当前已暂停，恢复成功前不会允许继续交易。" });
  } else if (health.runtime_state === "blocked") {
    const lead = blockers[0];
    banners.push({
      tone: "warning",
      message: lead ? `交易被阻断：${localizeErrorMessage(lead.blocker)}。${localizeErrorMessage(lead.recommended_action || "")}` : "当前存在安全阻断条件，系统暂不允许交易。",
    });
  } else if (recovery.review_required) {
    banners.push({
      tone: "warning",
      message: recoveryPolicyData().operator_rebaseline_supported
        ? "恢复前需要先重建基线。请先接受当前交易所状态为新的可信基线，再恢复自动交易。"
        : "当前恢复状态仍需人工复核，暂不应继续信任下一次自动操作。",
    });
  } else if (recovery.recovery_state === "rebaseline_completed" && !recovery.safe_to_trade) {
    banners.push({
      tone: "info",
      message: "新的基线已经准备好，但系统仍保持暂停，直到恢复成功。",
    });
  } else if (health.runtime_state === "degraded") {
    banners.push({ tone: "info", message: "系统当前处于降级状态。请先检查数据新鲜度、阻断项和对账结果，再决定是否继续信任自动交易。" });
  }

  Object.entries(state.panelErrors).slice(0, 4).forEach(([panel, error]) => {
    banners.push({
      tone: error.status === 401 || error.status === 403 ? "danger" : "warning",
      message: `${PANEL_LABELS[panel] || panel}加载失败：${error.message}`,
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
  nodes.overviewPosture.innerHTML = renderFactGrid(
    paperLocal
      ? [
          ["运行状态", readableState(health.runtime_state)],
          ["运行配置", readableMode(runtimeProfile.name)],
          ["产品类型", readableMode(environment.product_type)],
          ["保证金模式", readableMode(environment.margin_model)],
          ["行情来源", environment.market_data_source_kind || "-"],
          ["执行通道", environment.execution_route || mode.execution_route || "-"],
          ["持仓方向模式", readableMode(environment.position_directionality)],
          ["杠杆支持", readableMode(environment.leverage_support)],
          ["仅本地运行", booleanWord(environment.local_only)],
          ["是否连接交易所", booleanWord(environment.exchange_coupled)],
          ["账户观测", booleanWord(state.data.accountState?.read_enabled)],
          ["是否暂停", booleanWord(health.halted)],
        ]
      : [
          ["总体状态", readableState(health.overall_status)],
          ["运行状态", readableState(health.runtime_state)],
          ["运行配置", readableMode(runtimeProfile.name)],
          ["产品类型", readableMode(environment.product_type)],
          ["保证金模式", readableMode(environment.margin_model)],
          ["持仓方向模式", readableMode(environment.position_directionality)],
          ["杠杆支持", readableMode(environment.leverage_support)],
          ["运行姿态", readableMode(mode.operating_state)],
          ["执行通道", environment.execution_route || mode.execution_route || "-"],
          ["提交通道目标", environment.exchange_submission_target || "-"],
          ["允许提交到交易所", booleanWord(mode.exchange_submit_allowed)],
          ["需要人工审批", booleanWord(policyProfile.requires_human_approval)],
        ]
  );

  nodes.overviewPortfolio.innerHTML = renderFactGrid([
    ["总权益", portfolio ? formatNumber(portfolio.total_equity) : "-"],
    ["已实现收益", portfolio ? formatSigned(portfolio.realized_pnl) : "-"],
    ["未实现收益", portfolio ? formatSigned(portfolio.unrealized_pnl) : "-"],
    ["总敞口", portfolio ? formatNumber(portfolio.gross_exposure) : "-"],
    ["净敞口", portfolio ? formatSigned(portfolio.net_exposure) : "-"],
    ["主跟踪标的", trackedSymbol() || "-"],
    ["主持仓方向", primaryPosition ? readableMode(primaryPosition.exposure_side) : "-"],
    ["主持仓数量", primaryPosition ? formatSigned(primaryPosition.position_qty) : "-"],
    ["主持仓目标杠杆", primaryPosition ? formatNumber(primaryPosition.target_leverage) : "-"],
    ["保证金占用", portfolio ? formatNumber(portfolio.margin_usage) : "-"],
    ["USDT 余额", portfolio ? formatNumber(portfolio.balances?.USDT) : "-"],
    ["更新时间", state.data.portfolio?.latest_update_timestamp ? formatDateTime(state.data.portfolio.latest_update_timestamp) : "-"],
  ]);

  nodes.overviewBlockerStamp.textContent = blockers.length ? `${blockers.length} 个阻断项` : "无阻断";
  nodes.overviewBlockers.innerHTML = renderSignalCards(
    blockers.map((item) => ({
      title: localizeErrorMessage(item.blocker),
      subtitle: `${readableMode(item.subsystem)} | ${item.submit_only ? "仅阻断提交" : "阻断执行路径"}`,
      tone: item.affects_execution ? "warning" : "info",
      detail: localizeErrorMessage(item.recommended_action),
    })),
    "当前没有阻断项。"
  );

  nodes.overviewRecovery.innerHTML = renderFactGrid([
    ["恢复状态", readableState(recovery.recovery_state)],
    ["允许继续交易", booleanWord(recovery.safe_to_trade)],
    ["允许恢复运行", booleanWord(recovery.resume_eligible)],
    ["需要人工复核", booleanWord(recovery.review_required)],
    ["可执行基线重建", booleanWord(recovery.rebaseline_available)],
    ["恢复阻断原因", listOrDashLocalized(recovery.resume_blocked_reasons)],
    ["基线状态", readableState(baseline.status)],
    ["基线类型", readableState(baseline.baseline_kind)],
    ["已导入基线", booleanWord(baseline.baseline_imported)],
    ["基线导入时间", formatMaybeTimestamp(baseline.baseline_imported_at)],
    ["最近一次重建基线", formatMaybeTimestamp(baseline.last_rebaseline_at)],
    ["基线时挂单数", formatNumber(baseline.open_order_count)],
  ]);

  nodes.overviewMetrics.innerHTML = renderFactGrid([
    ["决策轮次", formatNumber(metrics.decision_cycle_count)],
    ["订单意图数", formatNumber(metrics.order_intent_count)],
    ["当前挂单数", formatNumber(metrics.current_open_order_count)],
    ["成交数", formatNumber(metrics.fill_count)],
    ["拒单数", formatNumber(metrics.rejection_count)],
    ["对账差异数", formatNumber(metrics.reconciliation_mismatch_count)],
    ["近期执行异常数", formatNumber((metrics.recent_execution_errors || []).length)],
    ["最近决策时间", formatMaybeTimestamp(latestDecision.decision_context?.as_of_ts)],
    ["最近成交", fillFreshnessLabel(executionLatest.latest_fill)],
    ["最近动作", decisionActivityLabel(latestDecision, executionLatest.latest_order, executionLatest.latest_fill)],
    ["账户数据新鲜", booleanWord(state.data.accountState?.fresh)],
  ]);

  nodes.overviewTimeline.innerHTML = renderTimeline(buildTimeline({ paperLocal }));
}

function renderDecisions() {
  const latestDecision = state.data.latestDecision || {};
  const recentDecisions = state.data.recentDecisions?.decisions || [];

  nodes.decisionSpotlight.innerHTML = renderDecisionInvestigation(latestDecision);
  nodes.decisionTable.innerHTML = renderTable(
    ["决策", "意图", "结果", "时间", "查看"],
    recentDecisions.map((item) => ([
      `<div class="cell-stack"><strong>${escapeHtml(item.symbol || "-")}</strong><div class="table-meta">${escapeHtml(item.timeframe || "-")} | ${escapeHtml(item.decision_id || "-")}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(recentDecisionHeadline(item))}</strong><div class="table-meta">${escapeHtml(recentDecisionNarrative(item))}</div></div>`,
      `<div class="cell-stack"><div class="table-inline-badges">${miniBadge(item.policy_result ? "策略放行" : "策略阻断", item.policy_result ? "success" : "danger")}${miniBadge(item.risk_result ? "风控通过" : "风控阻断", item.risk_result ? "success" : "danger")}${miniBadge(recentDecisionRequiresTrade(item) ? "需要交易" : "无需交易", recentDecisionRequiresTrade(item) ? "info" : "outline")}</div><div class="table-meta">${escapeHtml(recentDecisionOutcome(item))}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(formatRelativeAge(item.decision_time))}</strong><div class="table-meta">${escapeHtml(formatMaybeTimestamp(item.decision_time))}</div></div>`,
      item.decision_id ? `<button class="table-button" data-inspect-decision="${escapeHtml(item.decision_id)}">查看</button>` : "",
    ])),
    "暂无最近决策。"
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
          ["执行就绪", booleanWord(readiness.ready)],
          ["产品类型", readableMode(environment.product_type)],
          ["保证金模式", readableMode(environment.margin_model)],
          ["持仓方向模式", readableMode(environment.position_directionality)],
          ["最近订单", latestOrder ? `${readableState(latestOrder.status)} | ${latestOrder.client_order_id}` : "-"],
          ["最近成交", latestFill ? `${formatNumber(latestFill.fill_qty)} @ ${formatNumber(latestFill.fill_price)}` : "-"],
          ["最近交易意图", readableMode(latestFill?.position_intent || latestOrder?.submission_payload?.positionIntent || state.data.latestDecision?.position_target?.position_intent || "-")],
          ["最近成交新鲜度", fillFreshnessLabel(latestFill)],
          ["对账状态", latestReconciliation ? readableState(latestReconciliation.severity) : "-"],
          ["恢复状态", readableState(execution.recovery?.recovery_state)],
          ["当前挂单数", formatNumber(state.data.metrics?.current_open_order_count)],
        ])}
      </div>
    `;

  nodes.orderTable.innerHTML = renderTable(
    ["订单", "含义", "状态", "更新时间", "操作"],
    recentOrders.map((order) => ([
      `<div class="cell-stack"><strong>${escapeHtml(order.symbol || "-")}</strong><div class="table-meta">${escapeHtml(order.client_order_id || "-")}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(recentOrderHeadline(order))}</strong><div class="table-meta">${escapeHtml(recentOrderNarrative(order))}</div></div>`,
      `<div class="cell-stack"><div class="table-inline-badges">${miniBadge(order.status || "-", toneForOrderStatus(order.status))}</div><div class="table-meta">${escapeHtml(recentOrderStateSummary(order))}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(formatRelativeAge(order.last_update_ts || order.created_at))}</strong><div class="table-meta">${escapeHtml(formatMaybeTimestamp(order.last_update_ts || order.created_at))}</div></div>`,
      renderOrderActions(order),
    ])),
    "暂无最近订单。"
  );

  nodes.fillTable.innerHTML = renderTable(
    ["成交", "发生了什么", "收益影响", "写入时间", "操作"],
    recentFills.map((fill) => ([
      `<div class="cell-stack"><strong>${escapeHtml(fill.symbol || "-")}</strong><div class="table-meta">${escapeHtml(fill.fill_id || "-")}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(recentFillHeadline(fill))}</strong><div class="table-meta">${escapeHtml(recentFillNarrative(fill))}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(recentFillImpactSummary(fill))}</strong><div class="table-meta">${escapeHtml(`手续费 ${formatNumber(fill.fee_amount)} ${fill.fee_currency || ""}`.trim())}</div></div>`,
      `<div class="cell-stack"><strong>${escapeHtml(formatRelativeAge(fill.ingestion_timestamp))}</strong><div class="table-meta">${escapeHtml(formatMaybeTimestamp(fill.ingestion_timestamp))}</div></div>`,
      fill.fill_id ? `<button class="table-button" data-inspect-fill="${escapeHtml(fill.fill_id)}">查看</button>` : "",
    ])),
    "暂无最近成交。"
  );

  nodes.executionErrorsPanel.innerHTML = renderSignalCards(
    errors.slice(0, 6).map((item) => ({
      title: localizeErrorMessage(item.message || item.status || "execution issue"),
      subtitle: `${readableMode(item.subsystem || "execution")} | ${formatMaybeTimestamp(item.timestamp)}`,
      tone: item.severity === "error" ? "danger" : "warning",
      detail: [item.decision_id, item.order_id].filter(Boolean).join(" | ") || "暂无关联编号",
    })),
    "近期没有执行异常。"
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
          <h3 class="hero-title">${escapeHtml(readableState(reconciliation.severity))}</h3>
        </div>
        <div class="runtime-badges">
          ${miniBadge(reconciliation.halt_required ? "需要暂停" : "可继续运行", reconciliation.halt_required ? "danger" : "success")}
          ${miniBadge(reconciliation.exchange_comparison_enabled ? "已比对交易所" : "仅本地校验", reconciliation.exchange_comparison_enabled ? "info" : "outline")}
        </div>
      </div>
      <p class="hero-copy">${escapeHtml(listOrDashLocalized(mismatchSummary?.mismatch_reasons) || "-")}</p>
      ${renderFactGrid([
        ["差异原因", listOrDashLocalized(mismatchSummary?.mismatch_reasons)],
        ["差异类别", listOrDashLocalized(mismatchSummary?.mismatch_categories)],
        ["安全影响", listOrDashLocalized(mismatchSummary?.safety_impacts)],
        ["需要人工复核", booleanWord(reconciliation.review_required)],
        ["建议动作", localizeErrorMessage(mismatchSummary?.recommended_operator_action || "-")],
        ["最近校验时间", latestValidation?.validated_at ? formatDateTime(latestValidation.validated_at) : "-"],
        ["是否已比对交易所", booleanWord(reconciliation.exchange_comparison_enabled)],
      ])}
    </div>
  ` : emptyState("还没有对账报告。");

  nodes.diagnosticRecovery.innerHTML = renderSignalCards([
      {
        title: `恢复状态：${readableState(recovery.recovery_state)}`,
        subtitle: `可交易 ${booleanWord(recovery.safe_to_trade)} | 可恢复 ${booleanWord(recovery.resume_eligible)}`,
        tone: recovery.safe_to_trade ? "success" : recovery.review_required ? "warning" : "danger",
        detail: recovery.review_required
          ? `仍需人工复核。恢复阻断原因：${listOrDashLocalized(recovery.resume_blocked_reasons)}。`
          : recovery.safe_to_trade
            ? "当前恢复姿态已满足继续交易的要求。"
            : `交易仍被以下原因阻断：${listOrDashLocalized(recovery.resume_blocked_reasons)}。`,
      },
      {
        title: `基线状态：${readableState(baseline.status)}`,
        subtitle: `${readableState(baseline.baseline_kind)} | ${baseline.baseline_source || "-"}`,
        tone: baseline.status === "ready" || baseline.status === "accepted" ? "info" : "outline",
        detail: baseline.baseline_imported_at
          ? `${formatRelativeAge(baseline.baseline_imported_at)}导入。事件引用：${baseline.event_ref || "-"}。`
          : "还没有记录基线导入时间。",
      },
      {
        title: "人工恢复控制",
        subtitle: `可重建基线 ${booleanWord(recovery.rebaseline_available)} | 需要复核 ${booleanWord(recovery.review_required)}`,
        tone: recovery.rebaseline_available ? "warning" : "info",
        detail: baseline.last_rebaseline_event_ref
          ? `最近一次重建基线事件：${baseline.last_rebaseline_event_ref}。`
          : "还没有记录重建基线事件。",
      },
    ], "还没有恢复状态数据。");

  nodes.diagnosticReplay.innerHTML = renderSignalCards([
      {
        title: replay.last_validation ? "近期已执行回放校验" : "回放当前空闲",
        subtitle: replay.last_validation?.decision_id || "近期没有回放校验",
        tone: replay.healthy ? "success" : replay.supported ? "warning" : "outline",
        detail: replay.last_validation
          ? `${formatRelativeAge(replay.last_validation.validated_at)}完成校验，记录到 ${formatNumber(replay.last_validation.divergence_count)} 个偏差。`
          : "近期没有保存回放校验记录。",
      },
      {
        title: "回放覆盖情况",
        subtitle: `支持回放 ${booleanWord(replay.supported)} | 回放健康 ${booleanWord(replay.healthy)}`,
        tone: replay.supported ? "info" : "outline",
        detail: `回放事件数 ${formatNumber(replay.last_validation?.replayed_event_count)} | 基线切换次数 ${formatNumber(replay.last_validation?.baseline_switch_count)}。`,
      },
    ], "还没有回放校验记录。");

  nodes.diagnosticAccount.innerHTML = renderSignalCards([
      {
        title: `账户后端：${readableMode(account.backend || "-")}`,
        subtitle: `已连接 ${booleanWord(account.connected)} | 数据新鲜 ${booleanWord(account.fresh)} | 已就绪 ${booleanWord(account.ready)}`,
        tone: account.ready ? "success" : account.connected ? "warning" : "danger",
        detail: account.current_blocking_reason
          ? `当前阻断原因：${localizeErrorMessage(account.current_blocking_reason)}。`
          : `最近刷新：${account.last_refresh_timestamp ? formatRelativeAge(account.last_refresh_timestamp) : "-"}。`,
      },
      {
        title: "基线状态",
        subtitle: readableState(account.baseline_takeover?.status),
        tone: account.baseline_takeover?.status ? "info" : "outline",
        detail: `基线来源 ${account.baseline_takeover?.baseline_source || "-"} | 恢复状态 ${readableState(account.recovery?.recovery_state)}。`,
      },
    ], "还没有账户状态数据。");

  nodes.diagnosticBlockers.innerHTML = renderTimeline(
    blockerHistory.slice().reverse().map((item) => ({
      title: "阻断快照",
      subtitle: `${readableState(item.runtime_state || "-")} | ${readableMode(item.operating_state || "-")}`,
      timestamp: item.created_at,
      tone: item.execution_blocked ? "warning" : "info",
      detail: item.blockers?.length ? item.blockers.map((blocker) => localizeErrorMessage(blocker.blocker)).join("，") : "无阻断",
    })),
    "暂无阻断历史。"
  );

  nodes.diagnosticMetrics.innerHTML = renderSignalCards([
      {
        title: "决策节奏",
        subtitle: `${formatNumber(metrics.decision_cycle_count)} 次决策 | ${formatNumber(metrics.order_intent_count)} 个订单意图`,
        tone: "info",
        detail: `最近决策：${runtime.last_decision_timestamp ? formatRelativeAge(runtime.last_decision_timestamp) : "-"} | 已运行 ${formatDuration(runtime.uptime_seconds)}。`,
      },
      {
        title: "执行吞吐",
        subtitle: `${formatNumber(metrics.fill_count)} 笔成交 | ${formatNumber(metrics.current_open_order_count)} 笔挂单`,
        tone: metrics.current_open_order_count > 0 ? "warning" : "success",
        detail: `最近成交：${fillFreshnessLabel(state.data.executionLatest?.latest_fill)}。`,
      },
      {
        title: "近期活动",
        subtitle: `${formatNumber(metrics.rejection_count)} 次拒单`,
        tone: metrics.rejection_count > 0 ? "warning" : "info",
        detail: decisionActivityLabel(state.data.latestDecision || {}, state.data.executionLatest?.latest_order, state.data.executionLatest?.latest_fill),
      },
    ], "还没有运行指标。");
}

function renderRuntimeProfiles() {
  const profiles = state.data.runtimeProfiles || {};
  const controlEnabled = state.data.authProviders?.runtime_profile_control_enabled === true;
  const payload = profiles.current_runtime_payload || {};
  setRuntimeProfilesViewEnabled(false);
  nodes.runtimeProfileSummary.innerHTML = renderFactGrid([
    ["控制方式", "通过环境配置文件切换"],
    ["当前配置来源", readableState(profiles.profile_source || state.data.runtime?.profile_source || "env_fallback")],
    ["默认交易对", payload.default_symbol || "-"],
    ["允许交易的标的", listOrDash(payload.allowed_symbols)],
    ["产品类型", readableState(payload.trading_product_type || "-")],
    ["保证金模式", readableState(payload.margin_mode || "-")],
  ]);
  nodes.runtimeProfileSupervisor.innerHTML = emptyState(
    controlEnabled
      ? "当前已启用浏览器内的运行配置控制。"
      : "当前 UI 内的运行配置控制已关闭。请通过切换 .env、.env.spot 或 .env.derivatives 后再重启服务。"
  );
  nodes.runtimeProfileImpact.innerHTML = emptyState("当前环境文件切换模式下，没有浏览器内版本草稿。");
  nodes.runtimeProfileTable.innerHTML = emptyState("当前环境文件切换模式下，不支持浏览器内运行配置草稿。");
  nodes.runtimeProfileRevisionSelect.innerHTML = `<option value="">环境文件切换模式</option>`;
  nodes.runtimeProfilePermissionNote.textContent = "当前运行姿态来自环境配置文件，而不是浏览器控制平面。";
  setRuntimeProfileFormEnabled(false);
}

function renderOperators() {
  const providers = state.data.authProviders || {};
  const runtimeAuth = state.data.runtime?.operator_auth || {};
  const operatorUsers = state.data.operatorUsers || {};
  const users = operatorUsers.users || [];
  const canAdmin = operatorCanAdmin();

  nodes.operatorSummary.innerHTML = renderFactGrid([
    ["启用认证", booleanWord(providers.auth_enabled)],
    ["启用会话登录", booleanWord(providers.session_enabled)],
    ["使用数据库账户", booleanWord(providers.database_backed)],
    ["已存储账户数", formatNumber(providers.stored_user_count)],
    ["已配置角色", listOrDashLocalized(providers.configured_roles)],
    ["兼容 API Key", booleanWord(providers.api_key_compatibility_enabled)],
    ["当前身份", state.data.session?.identity || "-"],
    ["当前角色", readableState(state.data.session?.role)],
  ]);

  nodes.operatorBootstrap.innerHTML = renderFactGrid([
    ["已启用用户数", formatNumber(operatorUsers.enabled_user_count)],
    ["已启用管理员数", formatNumber(operatorUsers.enabled_admin_count)],
    ["允许本地无认证写入", booleanWord(runtimeAuth.unsafe_write_without_auth)],
    ["当前会话来源", readableState(state.data.session?.auth_source)],
    ["管理员权限", canAdmin ? "已授予" : "不可用"],
  ]);

  setOperatorCreateFormEnabled(canAdmin);
  if (!canAdmin) {
    nodes.operatorPermissionNote.textContent = "管理操作员账户需要管理员权限。";
    nodes.operatorUsersTable.innerHTML = emptyState("请先以管理员身份登录，然后再创建、修改、停用或删除操作员账户。");
    return;
  }

  nodes.operatorPermissionNote.textContent = "当前已使用管理员会话登录，修改会立即写入操作员用户表。";
  nodes.operatorUsersTable.innerHTML = renderTable(
    ["用户名", "角色", "状态", "最近登录", "最近更新", "当前会话", "操作"],
    users.map((user) => ([
      `<div><strong>${escapeHtml(user.username || "-")}</strong><div class="mono">${escapeHtml(user.user_id || "-")}</div></div>`,
      miniBadge(user.role || "-", user.role === "admin" ? "danger" : user.role === "operator" ? "info" : "outline"),
      `<div>${miniBadge(user.enabled ? "已启用" : "已停用", user.enabled ? "success" : "warning")}${user.protected_last_admin ? '<div class="table-meta">最后一个已启用管理员</div>' : ""}</div>`,
      escapeHtml(formatMaybeTimestamp(user.last_login_at)),
      escapeHtml(formatMaybeTimestamp(user.updated_at || user.created_at)),
      user.is_current_session_user ? miniBadge("当前会话", "info") : '<span class="table-meta">其他账户</span>',
      renderOperatorUserActions(user),
    ])),
    "当前还没有存储任何操作员账户。"
  );
}

function renderOperatorUserActions(user) {
  const toggleDisabled = user.protected_last_admin || user.is_current_session_user;
  const deleteDisabled = user.protected_last_admin || user.is_current_session_user;
  return `
    <div class="table-actions">
      <button class="table-button" data-role-user="${escapeHtml(user.username)}" data-current-role="${escapeHtml(user.role || "")}">修改角色</button>
      <button class="table-button" data-password-user="${escapeHtml(user.username)}">重置密码</button>
      <button class="table-button" data-toggle-user="${escapeHtml(user.username)}" data-next-enabled="${String(!user.enabled)}" ${toggleDisabled ? "disabled" : ""}>${user.enabled ? "停用" : "启用"}</button>
      <button class="table-button" data-delete-user="${escapeHtml(user.username)}" ${deleteDisabled ? "disabled" : ""}>删除</button>
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
  const profileLabel = nodes.runtimeProfileLabel.value.trim() || "运行配置草稿";
  try {
    const response = await requestJson("/runtime-profiles/drafts", {
      method: "POST",
      body: { profile_label: profileLabel },
    });
    flash(`已创建草稿：${response.revision.profile_label}。`, "info");
    await refreshDashboard({ manual: true });
    nodes.runtimeProfileRevisionSelect.value = response.revision.revision_id;
    populateRuntimeProfileDraftForm();
    setActiveView("runtime-profiles");
  } catch (error) {
    flash(`创建运行配置草稿失败：${normalizeError(error, "Runtime profile draft creation failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
}

async function saveRuntimeProfileDraft() {
  const revision = selectedRuntimeProfileRevision();
  if (!revision) {
    flash("请先创建或选择一个运行配置草稿。", "warning");
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
    flash("运行配置草稿已保存。", "info");
    await refreshDashboard({ manual: true });
    nodes.runtimeProfileRevisionSelect.value = revision.revision_id;
    populateRuntimeProfileDraftForm();
  } catch (error) {
    flash(`保存运行配置草稿失败：${normalizeError(error, "Runtime profile save failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
}

async function stageRuntimeProfileDraft() {
  const revision = selectedRuntimeProfileRevision();
  if (!revision) {
    flash("请先选择需要进入待激活状态的运行配置草稿。", "warning");
    renderAlerts();
    return;
  }
  if (!window.confirm("确认将这份运行配置设为待激活吗？只有重启后才会真正生效。")) {
    return;
  }
  try {
    await requestJson(`/runtime-profiles/revisions/${encodeURIComponent(revision.revision_id)}/stage`, {
      method: "POST",
      body: { activation_note: nodes.runtimeProfileActivationNote.value.trim() || null },
    });
    flash("运行配置已进入待激活状态。请重启托管 API 以使其生效。", "warning");
    await refreshDashboard({ manual: true });
  } catch (error) {
    flash(`设为待激活失败：${normalizeError(error, "Runtime profile staging failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
}

async function cancelPendingRuntimeProfile() {
  try {
    await requestJson("/runtime-profiles/pending/cancel", { method: "POST" });
    flash("已取消待激活的运行配置。", "info");
    await refreshDashboard({ manual: true });
  } catch (error) {
    flash(`取消待激活配置失败：${normalizeError(error, "Runtime profile cancel failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
}

async function requestRuntimeProfileRestart() {
  if (!window.confirm("确认发起托管重启吗？只有当前 API 由托管启动器运行时，这个操作才会生效。")) {
    return;
  }
  try {
    await requestJson("/runtime-profiles/restart", { method: "POST" });
    flash("已发起托管重启请求。", "warning");
    await refreshDashboard({ manual: true });
  } catch (error) {
    flash(`发起重启请求失败：${normalizeError(error, "Restart request failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
}

async function createOperatorUser() {
  const username = nodes.operatorCreateUsername.value.trim();
  const password = nodes.operatorCreatePassword.value;
  const role = nodes.operatorCreateRole.value;
  const enabled = nodes.operatorCreateEnabled.checked;
  if (!username || !password) {
    flash("创建操作员账户时必须填写用户名和密码。", "warning");
    renderAlerts();
    return;
  }
  nodes.operatorCreateButton.disabled = true;
  nodes.operatorCreateButton.textContent = "创建中...";
  try {
    await requestJson("/auth/users", {
      method: "POST",
      body: { username, password, role, enabled },
    });
    nodes.operatorCreateForm.reset();
    nodes.operatorCreateRole.value = "viewer";
    nodes.operatorCreateEnabled.checked = true;
    flash(`已创建操作员账户：${username}。`, "info");
    await refreshDashboard({ manual: true });
  } catch (error) {
    flash(`创建操作员账户失败：${normalizeError(error, "Operator user creation failed").message}`, "danger");
    renderDashboard({ manual: true });
  } finally {
    nodes.operatorCreateButton.disabled = !operatorCanAdmin();
    nodes.operatorCreateButton.textContent = "创建账户";
  }
}

async function toggleOperatorUser(username, nextEnabled) {
  if (!username) {
    return;
  }
  const actionLabel = nextEnabled ? "enable" : "disable";
  if (!window.confirm(`确认要${actionLabel === "enable" ? "启用" : "停用"}操作员账户 ${username} 吗？`)) {
    return;
  }
  await patchOperatorUser(username, { enabled: nextEnabled }, `操作员账户 ${username} 已${nextEnabled ? "启用" : "停用"}。`);
}

async function updateOperatorUserRole(username, currentRole) {
  if (!username) {
    return;
  }
  const nextRole = (window.prompt("请输入新的角色：viewer、operator 或 admin。", currentRole || "viewer") || "").trim();
  if (!nextRole || nextRole === currentRole) {
    return;
  }
  if (!["viewer", "operator", "admin"].includes(nextRole)) {
    flash("角色只能填写 viewer、operator 或 admin。", "warning");
    renderAlerts();
    return;
  }
  await patchOperatorUser(username, { role: nextRole }, `操作员账户 ${username} 的角色已更新为 ${readableState(nextRole)}。`);
}

async function resetOperatorUserPassword(username) {
  if (!username) {
    return;
  }
  const password = window.prompt(`请输入 ${username} 的新密码。`, "");
  if (password === null) {
    return;
  }
  if (!password) {
    flash("重置密码时必须输入非空密码。", "warning");
    renderAlerts();
    return;
  }
  await patchOperatorUser(username, { password }, `已完成 ${username} 的密码重置。`);
}

async function deleteOperatorUser(username) {
  if (!username) {
    return;
  }
  if (!window.confirm(`确认删除操作员账户 ${username} 吗？这会移除这条已存储的登录账户记录。`)) {
    return;
  }
  try {
    await requestJson(`/auth/users/${encodeURIComponent(username)}`, { method: "DELETE" });
    flash(`已删除操作员账户：${username}。`, "info");
    await refreshDashboard({ manual: true });
  } catch (error) {
    flash(`删除操作员账户失败：${normalizeError(error, "Operator user deletion failed").message}`, "danger");
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
    flash(`更新操作员账户失败：${normalizeError(error, "Operator user update failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
}

async function inspectLatestDecision() {
  const decisionId = state.data.latestDecision?.decision_id;
  if (!decisionId) {
    flash("当前还没有最新决策可查看。", "warning");
    renderAlerts();
    return;
  }
  await inspectDecision(decisionId, { manual: true });
}

async function inspectLatestOrder() {
  const orderId = state.data.executionLatest?.latest_order?.client_order_id;
  if (!orderId) {
    flash("当前还没有最新订单可查看。", "warning");
    renderAlerts();
    return;
  }
  await inspectOrder(orderId, { manual: true });
}

async function inspectLatestFill() {
  const fillId = state.data.executionLatest?.latest_fill?.fill_id;
  if (!fillId) {
    flash("当前还没有最新成交可查看。", "warning");
    renderAlerts();
    return;
  }
  await inspectFill(fillId, { manual: true });
}

async function inspectLatestReconciliation() {
  const reconciliationId = state.data.reconciliationLatest?.reconciliation?.reconciliation_id;
  if (!reconciliationId) {
    flash("当前还没有对账报告可查看。", "warning");
    renderAlerts();
    return;
  }
  try {
    const detail = await requestJson(`/reconciliation/${encodeURIComponent(reconciliationId)}`);
    showStructuredDetail({
      title: "对账详情",
      summary: [
        ["对账编号", detail.reconciliation?.reconciliation_id],
        ["严重级别", readableState(detail.reconciliation?.severity)],
        ["是否需要暂停", booleanWord(detail.reconciliation?.halt_required)],
        ["是否包含交易所比对", booleanWord(detail.reconciliation?.exchange_comparison_enabled)],
      ],
      sections: [
        detailCard("差异摘要", detail.mismatch_summary || {}, {
          narrative: reconciliationNarrative(detail),
          facts: [
            ["差异类别", listOrDashLocalized(detail.reconciliation?.mismatch_categories)],
            ["差异原因", listOrDashLocalized(detail.reconciliation?.mismatch_reasons)],
            ["建议动作", localizeErrorMessage(detail.reconciliation?.recommended_operator_action || "-")],
          ],
        }),
        detailCard("对账原始内容", detail.reconciliation || {}),
      ],
    });
  } catch (error) {
    flash(`查询对账详情失败：${normalizeError(error, "Reconciliation lookup failed").message}`, "danger");
    renderAlerts();
  }
}

function inspectSystemDetail() {
  showStructuredDetail({
    title: "系统详情",
    summary: [
      ["运行状态", readableState(state.data.health?.runtime_state)],
      ["运行姿态", readableMode(state.data.mode?.operating_state)],
      ["执行是否阻断", booleanWord(state.data.health?.execution_blocked)],
      ["提交是否阻断", booleanWord(state.data.health?.submit_blocked)],
    ],
    sections: [
      detailCard("系统姿态", { health: state.data.health || {}, mode: state.data.mode || {} }, {
        narrative: systemNarrative(),
        facts: [
          ["运行配置", readableMode(state.data.mode?.runtime_profile?.name)],
          ["产品类型", readableMode(state.data.mode?.environment_capabilities?.product_type)],
          ["执行通道", state.data.mode?.execution_route || "-"],
          ["阻断项数量", String((state.data.blockers?.blockers || []).length)],
        ],
      }),
      detailCard("恢复状态", state.data.systemRecovery || {}, {
        narrative: recoveryNarrative(),
      }),
      detailCard("阻断项", state.data.blockers || {}, {
        narrative: blockerNarrative(state.data.blockers?.blockers || []),
      }),
      detailCard("账户状态", state.data.accountState || {}, {
        narrative: accountNarrative(state.data.accountState || {}),
      }),
    ],
  });
}

function inspectRecoveryDetail() {
  const recovery = recoveryData();
  const baseline = state.data.runtime?.baseline_takeover || state.data.accountState?.baseline_takeover || {};
  showStructuredDetail({
    title: "恢复详情",
    summary: [
      ["恢复状态", readableState(recovery.recovery_state)],
      ["允许继续交易", booleanWord(recovery.safe_to_trade)],
      ["允许恢复运行", booleanWord(recovery.resume_eligible)],
      ["需要人工复核", booleanWord(recovery.review_required)],
      ["可重建基线", booleanWord(recovery.rebaseline_available)],
      ["基线状态", readableState(baseline.status)],
    ],
    sections: [
      detailCard("恢复摘要", state.data.systemRecovery || {}, {
        narrative: recoveryNarrative(),
        facts: [
          ["恢复阻断原因", listOrDashLocalized(recovery.resume_blocked_reasons)],
          ["最近恢复结果", readableState(recovery.last_resume_status)],
          ["最近一次重建基线", formatMaybeTimestamp(recovery.last_rebaseline_at)],
        ],
      }),
      detailCard("基线接管", baseline || {}, {
        narrative: baselineNarrative(baseline || {}),
      }),
      detailCard("账户状态", state.data.accountState || {}, {
        narrative: accountNarrative(state.data.accountState || {}),
      }),
    ],
  });
}

function inspectRuntimeDetail() {
  showStructuredDetail({
    title: "运行详情",
    summary: [
      ["配置", state.data.mode?.config_profile],
      ["执行通道", state.data.mode?.execution_route],
      ["启动时间", state.data.runtime?.startup_timestamp],
      ["已运行时长", formatDuration(state.data.runtime?.uptime_seconds)],
    ],
    sections: [
      detailCard("运行摘要", state.data.runtime || {}, {
        narrative: runtimeNarrative(),
        facts: [
          ["运行配置", readableMode(state.data.runtime?.runtime_profile?.name || state.data.mode?.runtime_profile?.name)],
          ["产品类型", readableMode(state.data.runtime?.environment_capabilities?.product_type || state.data.mode?.environment_capabilities?.product_type)],
          ["保证金模式", readableMode(state.data.runtime?.environment_capabilities?.margin_model || state.data.mode?.environment_capabilities?.margin_model)],
          ["持仓方向模式", readableMode(state.data.runtime?.environment_capabilities?.position_directionality || state.data.mode?.environment_capabilities?.position_directionality)],
        ],
      }),
      detailCard("恢复状态", state.data.systemRecovery || {}, {
        narrative: recoveryNarrative(),
      }),
      detailCard("运行指标", state.data.metrics || {}, {
        narrative: metricsNarrative(state.data.metrics || {}),
      }),
      detailCard("回放校验", state.data.replayStatus || {}, {
        narrative: replayNarrative(state.data.replayStatus || {}),
      }),
    ],
  });
}

function inspectPortfolioDetail() {
  showStructuredDetail({
    title: "账户与仓位详情",
    summary: [
      ["总权益", formatNumber(state.data.portfolio?.portfolio?.total_equity)],
      ["总敞口", formatNumber(state.data.portfolio?.portfolio?.gross_exposure)],
      ["净敞口", formatSigned(state.data.portfolio?.portfolio?.net_exposure)],
      ["更新时间", formatMaybeTimestamp(state.data.portfolio?.latest_update_timestamp)],
    ],
    sections: [
      detailCard("账户快照", state.data.portfolio?.portfolio || {}, {
        narrative: portfolioNarrative(state.data.portfolio?.portfolio || {}),
        facts: portfolioDetailFacts(state.data.portfolio?.portfolio || {}),
      }),
    ],
  });
}

async function inspectDecision(decisionId, { manual = false } = {}) {
  if (!decisionId) {
    if (manual) {
      flash("请输入有效的决策编号。", "warning");
      renderAlerts();
    }
    return;
  }
  try {
    const detail = await requestJson(`/decision/${encodeURIComponent(decisionId)}`);
    nodes.decisionLookupInput.value = decisionId;
      showStructuredDetail({
        title: "决策详情",
      summary: [
        ["决策编号", detail.decision_id],
        ["标的", detail.decision_context?.symbol],
        ["周期", detail.decision_context?.timeframe],
        ["目标调整量", formatSigned(detail.position_target?.delta_position_qty)],
        ["策略是否放行", booleanWord(detail.policy_decision?.execution_allowed)],
        ["风控是否通过", booleanWord(detail.risk_decision?.approved)],
      ],
        sections: [
          detailCard("决策摘要", detail, {
            narrative: decisionNarrative(detail),
            facts: [
              ["仓位意图", readableMode(detail.position_target?.position_intent)],
              ["目标敞口方向", readableMode(detail.position_target?.target_exposure_side)],
              ["风控拒绝原因", listOrDashLocalized(detail.risk_decision?.rejection_reasons)],
              ["执行结果", decisionExecutionOutcome(detail)],
            ],
          }),
          detailCard("决策上下文", detail.decision_context || {}, {
            narrative: decisionContextNarrative(detail.decision_context || {}),
          }),
          detailCard("基线策略判断", detail.baseline_assessment || {}, {
            narrative: baselineAssessmentNarrative(detail.baseline_assessment || {}),
          }),
          detailCard("AI 判断", detail.ai_assessment || {}, {
            narrative: aiAssessmentNarrative(detail.ai_assessment || {}),
          }),
          detailCard("目标仓位 / 策略 / 风控", {
            position_target: detail.position_target || null,
            policy_decision: detail.policy_decision || null,
            risk_decision: detail.risk_decision || null,
          }, {
            narrative: targetPolicyRiskNarrative(detail),
          }),
          detailCard("执行链路", {
            execution_plan: detail.execution_plan || null,
            order_intents: detail.order_intents || [],
            order_updates: detail.order_updates || [],
            fills: detail.fills || [],
            portfolio_snapshot: detail.portfolio_snapshot || null,
            reconciliations: detail.reconciliations || [],
          }, {
            narrative: executionChainNarrative(detail),
          }),
          detailCard("审计链", detail.audit || {}, {
            narrative: auditNarrative(detail.audit || {}),
          }),
        ],
      });
  } catch (error) {
    flash(`查询决策详情失败：${normalizeError(error, "Decision lookup failed").message}`, "danger");
    renderAlerts();
  }
}

async function inspectOrder(orderId, { manual = false } = {}) {
  if (!orderId) {
    if (manual) {
      flash("请输入有效的订单编号。", "warning");
      renderAlerts();
    }
    return;
  }
  try {
    const detail = await requestJson(`/orders/${encodeURIComponent(orderId)}`);
    nodes.orderLookupInput.value = orderId;
    const stuckResolution = detail.stuck_submission_resolution || {};
      showStructuredDetail({
        title: "订单详情",
      actions: stuckResolution.eligible ? [
        `<button class="action-button action-warning" data-resolve-stuck-order="${escapeHtml(detail.order?.client_order_id || "")}">处理卡住的提交</button>`,
      ] : [],
      summary: [
        ["订单编号", detail.order?.client_order_id],
        ["决策编号", detail.order?.decision_id],
        ["标的", detail.order?.symbol],
        ["状态", readableState(detail.order?.status)],
        ["通道", detail.order?.venue],
        ["请求数量", formatNumber(detail.order?.requested_qty)],
      ],
        sections: [
          detailCard("订单摘要", detail.order || {}, {
            narrative: orderNarrative(detail.order || {}, detail.fills || [], stuckResolution),
            facts: [
              ["仓位意图", readableMode(detail.order?.submission_payload?.positionIntent || "-")],
              ["交易所订单号", detail.order?.exchange_order_id || "-"],
              ["已成交数量", formatNumber(detail.order?.filled_qty)],
              ["平均成交价", formatNumber(detail.order?.average_fill_price)],
              ["是否启动后遗留提交", booleanWord(stuckResolution.runtime_restarted_after_order)],
              ["是否可恢复处理", booleanWord(stuckResolution.eligible)],
              ["恢复状态", localizeErrorMessage(stuckResolution.reason_code || "ready")],
            ],
          }),
          detailCard("关联成交", detail.fills || [], {
            narrative: linkedFillsNarrative(detail.fills || []),
          }),
          detailCard("人工恢复建议", stuckResolution, {
            narrative: [
              localizeErrorMessage(stuckResolution.summary || "当前没有针对这笔订单的人工恢复建议。"),
            ],
          }),
        ],
      });
  } catch (error) {
    flash(`查询订单详情失败：${normalizeError(error, "Order lookup failed").message}`, "danger");
    renderAlerts();
  }
}

async function inspectFill(fillId, { manual = false } = {}) {
  if (!fillId) {
    if (manual) {
      flash("请输入有效的成交编号。", "warning");
      renderAlerts();
    }
    return;
  }
  try {
    const detail = await requestJson(`/fills/${encodeURIComponent(fillId)}`);
    nodes.fillLookupInput.value = fillId;
      showStructuredDetail({
        title: "成交详情",
      summary: [
        ["成交编号", detail.fill?.fill_id],
        ["决策编号", detail.fill?.decision_id],
        ["标的", detail.fill?.symbol],
        ["方向", readableMode(detail.fill?.side)],
        ["数量", formatNumber(detail.fill?.fill_qty)],
        ["价格", formatNumber(detail.fill?.fill_price)],
      ],
        sections: [
          detailCard("成交摘要", detail.fill || {}, {
            narrative: fillNarrative(detail.fill || {}),
            facts: [
              ["仓位意图", readableMode(detail.fill?.position_intent)],
              ["敞口方向", readableMode(detail.fill?.exposure_side)],
              ["手续费币种", detail.fill?.fee_currency || "-"],
              ["发生时间", formatMaybeTimestamp(detail.fill?.exchange_timestamp)],
            ],
          }),
        ],
      });
  } catch (error) {
    flash(`查询成交详情失败：${normalizeError(error, "Fill lookup failed").message}`, "danger");
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
    return emptyState("当前还没有最新决策。");
  }

  return `
    <div class="overview-hero">
      <div class="hero-header">
        <div>
          <p class="hero-id">${escapeHtml(latestDecision.decision_id)}</p>
          <h3 class="hero-title">${escapeHtml(latestDecision.decision_context?.symbol || "-")} | ${escapeHtml(latestDecision.decision_context?.timeframe || "-")}</h3>
        </div>
        <div class="runtime-badges">
          ${miniBadge(policy?.execution_allowed ? "策略放行" : "策略阻断", policy?.execution_allowed ? "success" : "danger")}
          ${miniBadge(risk?.approved ? "风控通过" : "风控阻断", risk?.approved ? "success" : "danger")}
          ${miniBadge(decisionTargetNeedsTrade(target) ? "需要调仓" : "继续持有", decisionTargetNeedsTrade(target) ? "info" : "outline")}
        </div>
      </div>
      <p class="hero-copy">${escapeHtml(outcome)}</p>
        ${renderFactGrid([
          ["仓位意图", readableMode(target?.position_intent || "-")],
          ["目标敞口方向", readableMode(target?.target_exposure_side || "-")],
          ["产品类型", readableMode(target?.product_type || latestDecision.decision_context?.product_type || "-")],
          ["保证金模式", readableMode(target?.margin_mode || "-")],
          ["目标调整量", formatSigned(target?.delta_position_qty)],
          ["目标持仓数量", formatSigned(target?.target_position_qty)],
          ["基线方向偏置", readableMode(baseline?.direction_bias || "-")],
          ["综合 Alpha 分数", formatSigned(baseline?.composite_alpha_score)],
          ["建议仓位比例", formatNumber(baseline?.suggested_position_scale)],
          ["波动率目标缩放", formatNumber(baseline?.volatility_target_scale)],
          ["最近订单状态", latestOrder ? readableState(latestOrder.status) : "-"],
          ["最近成交", latestFill ? `${formatNumber(latestFill.fill_qty)} @ ${formatNumber(latestFill.fill_price)}` : "-"],
      ])}
    </div>
  `;
}

function renderDecisionInvestigation(latestDecision) {
  if (!latestDecision.decision_id) {
    return emptyState("当前还没有决策详情。");
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
          ${miniBadge(policy.execution_allowed ? "策略放行" : "策略阻断", policy.execution_allowed ? "success" : "danger")}
          ${miniBadge(risk.approved ? "风控通过" : "风控拒绝", risk.approved ? "success" : "danger")}
        </div>
      </div>
        ${renderFactGrid([
          ["决策时间", formatMaybeTimestamp(context.as_of_ts)],
          ["产品类型", readableMode(context.product_type)],
          ["当前敞口方向", readableMode(context.current_exposure_side)],
          ["目标杠杆", formatNumber(target.target_leverage)],
          ["方向偏置", readableMode(baseline.direction_bias || "-")],
          ["基线置信度", formatNumber(baseline.confidence)],
          ["AI 模式", readableMode(aiAssessment.operating_mode || state.data.mode?.ai_operating_mode || "-")],
          ["目标持仓数量", formatSigned(target.target_position_qty)],
          ["目标调整量", formatSigned(target.delta_position_qty)],
          ["风控拒绝原因", listOrDashLocalized(risk.rejection_reasons)],
          ["最近对账结论", readableState(latestReconciliation?.severity || "-")],
      ])}
      <div class="signal-card">
        <div class="signal-head">
          <span class="signal-title">原因代码</span>
          <button class="table-button" data-inspect-decision="${escapeHtml(latestDecision.decision_id)}">打开审计链路</button>
        </div>
        <div class="signal-copy">${escapeHtml(listOrDashLocalized(baseline.reason_codes || aiAssessment.reason_codes || risk.rejection_reasons))}</div>
      </div>
    </div>
  `;
}

function decisionNarrative(detail) {
  const context = detail.decision_context || {};
  const target = detail.position_target || {};
  const policy = detail.policy_decision || {};
  const risk = detail.risk_decision || {};
  if (!decisionTargetNeedsTrade(target)) {
    return [
      `${context.symbol || "该标的"}在 ${context.timeframe || "-"} 周期、${formatMaybeTimestamp(context.as_of_ts)} 被评估。`,
      `当前敞口已经与目标一致，持仓为 ${formatSigned(target.current_position_qty ?? 0)}，因此不需要调仓。`,
      policy.execution_allowed
        ? risk.approved
          ? "策略层和风控层都认为继续持有当前仓位是可接受的。"
          : `策略层允许保持当前姿态，但风控仍给出了这些拒绝原因：${listOrDashLocalized(risk.rejection_reasons)}。`
        : `策略层因以下原因阻断了任何改动：${listOrDashLocalized(policy.rejection_reasons)}。`,
    ];
  }
  return [
    `${context.symbol || "该标的"}在 ${context.timeframe || "-"} 周期、${formatMaybeTimestamp(context.as_of_ts)} 被评估。`,
    `系统计划执行 ${readableMode(target.position_intent || "hold")}，将敞口从 ${formatSigned(target.current_position_qty)} 调整到 ${formatSigned(target.target_position_qty)}。`,
    policy.execution_allowed
      ? risk.approved
        ? `策略层与风控层都已放行。${decisionExecutionOutcome(detail)}`
        : `策略层允许该动作，但风控因以下原因阻断：${listOrDashLocalized(risk.rejection_reasons)}。`
      : `策略层因以下原因阻断了这次动作：${listOrDashLocalized(policy.rejection_reasons)}。`,
  ];
}

function decisionContextNarrative(context) {
  return [
    `系统当时处于 ${readableMode(context.mode)} 运行模式，当前敞口为 ${readableMode(context.current_exposure_side)} ${formatSigned(context.current_position_qty)}。`,
    `这次决策基于 ${readableMode(context.product_type)} 产品类型，并使用当前目标杠杆 ${formatNumber(context.current_target_leverage)}。`,
  ];
}

function baselineAssessmentNarrative(baseline) {
  return [
    `基线模型判断当前市场状态为 ${readableMode(baseline.regime)}，方向偏置为 ${readableMode(baseline.direction_bias)}。`,
    `当前置信度为 ${formatNumber(baseline.confidence)}，建议仓位比例为 ${formatNumber(baseline.suggested_position_scale)}。`,
    `主要原因代码：${listOrDashLocalized(baseline.reason_codes)}。`,
  ];
}

function aiAssessmentNarrative(assessment) {
  return [
    `AI 当前运行模式为 ${readableMode(assessment.operating_mode)}。`,
    assessment.fallback_used
      ? `由于 ${readableMode(assessment.fallback_reason)}，系统采用了回退判断。`
      : `模型返回已被接受，校准后置信度为 ${formatNumber(assessment.calibrated_confidence)}。`,
    `方向性边际为 ${formatSigned(assessment.directional_edge)}，预期波动率为 ${formatNumber(assessment.expected_volatility)}。`,
  ];
}

function targetPolicyRiskNarrative(detail) {
  const target = detail.position_target || {};
  const policy = detail.policy_decision || {};
  const risk = detail.risk_decision || {};
  return [
    `目标意图为 ${readableMode(target.position_intent)}，交易产品是 ${readableMode(target.product_type)}，保证金语义为 ${readableMode(target.margin_mode)}。`,
    `策略层${policy.execution_allowed ? "允许" : "阻断"}该动作，风控层${risk.approved ? "通过" : "阻断"}该动作。`,
    `生效的风控约束：${listOrDashLocalized(risk.constraints_applied)}。`,
  ];
}

function executionChainNarrative(detail) {
  const intents = detail.order_intents || [];
  const orders = detail.order_updates || [];
  const fills = detail.fills || [];
  const reconciliations = detail.reconciliations || [];
  return [
    `这次决策共产生 ${intents.length} 个意图、${orders.length} 条订单更新，以及 ${fills.length} 条成交记录。`,
    fills.length
      ? `执行后已生成账户快照和对账结果。相关对账次数：${reconciliations.length}。`
      : "当前还没有成交写入，因此下游账户快照和对账结果可能仍在等待。",
  ];
}

function auditNarrative(audit) {
  return [
    `审计链路串起了决策 ${audit.decision_id || "-"} 从上下文、执行到对账的完整过程。`,
    `当前已关联 ${audit.order_intent_refs?.length || 0} 个意图、${audit.order_state_refs?.length || 0} 条订单更新，以及 ${audit.fill_event_refs?.length || 0} 条成交事件。`,
  ];
}

function orderNarrative(order, fills, stuckResolution = {}) {
  const lines = [
    `这笔订单通过 ${order.venue || "-"} 向 ${order.symbol || "-"} 提交，类型为 ${readableMode(order.submission_payload?.ordType || "market")}，方向为 ${readableMode(order.submission_payload?.side || "-")}。`,
    `当前生命周期状态为 ${readableMode(order.status)}，请求数量 ${formatNumber(order.requested_qty)}，已成交数量 ${formatNumber(order.filled_qty)}。`,
    fills.length
      ? `当前已有 ${fills.length} 条关联成交记录。`
      : "当前还没有关联成交记录。",
  ];
  if (stuckResolution.eligible) {
    lines.push("这笔订单看起来像是服务重启前遗留的卡住提交，可在执行表中发起收敛处理。");
  } else if (stuckResolution.summary) {
    lines.push(localizeErrorMessage(stuckResolution.summary));
  }
  return lines;
}

function renderOrderActions(order) {
  if (!order.client_order_id) {
    return "";
  }
  const canWrite = operatorCanWrite();
  const inspectButton = `<button class="table-button" data-inspect-order="${escapeHtml(order.client_order_id)}">查看</button>`;
  if (!isRecoverableRestartSubmission(order)) {
    return inspectButton;
  }
  return `
    <div class="table-actions">
      ${inspectButton}
      <button class="table-button" data-resolve-stuck-order="${escapeHtml(order.client_order_id)}" ${canWrite ? "" : "disabled"}>处理卡住的提交</button>
    </div>
  `;
}

function isRecoverableRestartSubmission(order) {
  const startupTimestamp = state.data.runtime?.startup_timestamp;
  const lastUpdate = order?.last_update_ts || order?.created_at;
  if (!startupTimestamp || !lastUpdate) {
    return false;
  }
  if (order?.venue !== "OKX") {
    return false;
  }
  if (!["CREATED", "SUBMITTING"].includes(order?.status || "")) {
    return false;
  }
  if (order?.exchange_order_id) {
    return false;
  }
  return Date.parse(lastUpdate) < Date.parse(startupTimestamp);
}

async function resolveStuckOrder(orderId) {
  if (!orderId) {
    return;
  }
  if (!window.confirm("确认将这笔重启前卡住的提交收敛为 FAILED 吗？系统会先确认它不在最新交易所快照中。")) {
    return;
  }
  try {
    await requestJson(`/orders/${encodeURIComponent(orderId)}/resolve-stuck-submission`, {
      method: "POST",
      body: { reason: "ui_resolve_stuck_submission" },
    });
    flash(`已收敛卡住的提交 ${orderId}，并重新执行了对账。`, "warning");
    await refreshDashboard({ manual: true });
  } catch (error) {
    flash(`处理卡住提交失败：${normalizeError(error, "Stuck submission resolution failed").message}`, "danger");
    renderDashboard({ manual: true });
  }
}

function linkedFillsNarrative(fills) {
  if (!fills.length) {
    return ["当前还没有任何成交关联到这笔订单。"];
  }
  const totalQty = fills.reduce((sum, fill) => sum + Number(fill.fill_qty || 0), 0);
  return [
    `当前共有 ${fills.length} 条成交关联到这笔订单。`,
    `这些成交累计执行数量为 ${formatNumber(totalQty)}。`,
  ];
}

function fillNarrative(fill) {
  return [
    `这条成交在 ${fill.symbol || "-"} 上以 ${formatNumber(fill.fill_price)} 的价格成交了 ${formatNumber(fill.fill_qty)}，方向为 ${readableMode(fill.side || "-")}。`,
    `它代表 ${readableMode(fill.position_intent)} 的交易意图，作用于 ${readableMode(fill.product_type)} 产品，并影响 ${readableMode(fill.exposure_side)} 敞口。`,
    `手续费为 ${formatNumber(fill.fee_amount)} ${fill.fee_currency || ""}`.trim(),
  ];
}

function reconciliationNarrative(detail) {
  const reconciliation = detail.reconciliation || {};
  const severity = readableMode(reconciliation.severity);
  if (!reconciliation.mismatch_categories?.length) {
    return [
      `这份对账报告的结论是 ${severity}，系统当前没有发现需要人工介入的差异。`,
      reconciliation.exchange_comparison_enabled
        ? "这份报告已启用交易所侧比对。"
        : "这份报告只关注本地重建一致性，并未引入交易所侧比对。",
    ];
  }
  return [
    `这份对账报告的结论是 ${severity}，共发现 ${reconciliation.mismatch_categories.length} 类差异。`,
    `主要差异原因：${listOrDashLocalized(reconciliation.mismatch_reasons)}。`,
    `建议人工动作：${localizeErrorMessage(reconciliation.recommended_operator_action || "review the mismatch payload")}。`,
  ];
}

function systemNarrative() {
  const health = state.data.health || {};
  const mode = state.data.mode || {};
  return [
    `系统当前处于 ${readableMode(health.runtime_state)} 状态，运行姿态为 ${readableMode(mode.operating_state)}。`,
    health.execution_blocked
      ? `执行路径当前被阻断，原因包括：${listOrDashLocalized((state.data.blockers?.blockers || []).map((item) => item.blocker))}。`
      : "当前执行路径允许继续运行，没有主链阻断项。",
  ];
}

function recoveryNarrative() {
  const recovery = recoveryData();
  return [
    `恢复状态为 ${readableMode(recovery.recovery_state)}。`,
    recovery.safe_to_trade
      ? "系统当前认为继续交易是安全的。"
      : "系统当前认为继续交易仍不安全。",
    recovery.review_required
      ? `仍需人工复核，原因包括：${listOrDashLocalized(recovery.resume_blocked_reasons)}。`
      : `恢复阻断原因：${listOrDashLocalized(recovery.resume_blocked_reasons)}。`,
  ];
}

function baselineNarrative(baseline) {
  return [
    `基线状态为 ${readableMode(baseline.status || baseline.baseline_status)}。`,
    `这份基线来自 ${readableMode(baseline.baseline_kind || baseline.baseline_source || "current account state")}。`,
  ];
}

function accountNarrative(account) {
  return [
    `账户后端是 ${readableMode(account.backend || account.account_source || "-")}，数据新鲜度为 ${booleanWord(account.fresh)}。`,
    `当前账户读取状态${account.ready ? "已就绪" : "未就绪"}，阻断原因为 ${listOrDashLocalized(account.blockers)}。`,
  ];
}

function runtimeNarrative() {
  const runtime = state.data.runtime || {};
  const mode = state.data.mode || {};
  const environment = runtime.environment_capabilities || mode.environment_capabilities || {};
  return [
    `当前运行配置为 ${readableMode(runtime.runtime_profile?.name || mode.runtime_profile?.name)}。`,
    `执行通道为 ${environment.execution_route || mode.execution_route || "-"}，产品类型 / 保证金模式分别为 ${readableMode(environment.product_type)} / ${readableMode(environment.margin_model)}。`,
    `持仓方向模式为 ${readableMode(environment.position_directionality)}，杠杆支持为 ${readableMode(environment.leverage_support)}。`,
  ];
}

function metricsNarrative(metrics) {
  return [
    `系统累计处理了 ${formatNumber(metrics.decision_cycle_count)} 轮决策，并产生了 ${formatNumber(metrics.order_intent_count)} 个订单意图。`,
    `目前已经写入 ${formatNumber(metrics.fill_count)} 条成交，并记录了 ${formatNumber(metrics.rejection_count)} 次拒单。`,
  ];
}

function replayNarrative(replayStatus) {
  return [
    replayStatus.last_validation
      ? `最近一次回放校验执行于 ${formatMaybeTimestamp(replayStatus.last_validation.validated_at)}。`
      : "近期没有执行回放校验。",
    `当前已存储的最近回放校验数量：${formatNumber((replayStatus.recent_validations || []).length)}。`,
  ];
}

function portfolioNarrative(portfolio) {
  const primary = trackedPortfolioPosition(portfolio);
  return [
    `账户总权益为 ${formatNumber(portfolio.total_equity)}，总敞口 ${formatNumber(portfolio.gross_exposure)}，净敞口 ${formatSigned(portfolio.net_exposure)}。`,
    primary
      ? `当前主要跟踪持仓为 ${primary.symbol} 的 ${readableMode(primary.exposure_side)} ${formatSigned(primary.position_qty)}。`
      : "当前账户快照里没有活跃跟踪持仓。",
  ];
}

function portfolioDetailFacts(portfolio) {
  const primary = trackedPortfolioPosition(portfolio);
  return [
    ["主跟踪标的", primary?.symbol || "-"],
    ["主持仓方向", readableMode(primary?.exposure_side || "-")],
    ["主持仓数量", primary ? formatSigned(primary.position_qty) : "-"],
    ["保证金占用", formatNumber(portfolio.margin_usage)],
    ["跟踪持仓数量", String((portfolio.positions || []).length)],
  ];
}

function blockerNarrative(blockers) {
  if (!blockers.length) {
    return ["当前系统没有任何阻断记录。"];
  }
  return [
    `当前共记录了 ${blockers.length} 个阻断项。`,
    `最需要优先处理的阻断项是：${localizeErrorMessage(blockers[0].blocker)}。建议动作：${localizeErrorMessage(blockers[0].recommended_action)}。`,
  ];
}

function decisionExecutionOutcome(detail) {
  const summary = detail.summary || {};
  const result = summary.execution_result || {};
  if (!decisionTargetNeedsTrade(detail.position_target || {})) {
    return "当前敞口已经与目标一致，因此没有创建新的执行动作。";
  }
  if (result.fill_count > 0) {
    return `这次决策已写入 ${result.fill_count} 条成交记录。`;
  }
  if (result.order_count > 0) {
    return `这次决策已创建 ${result.order_count} 笔订单，但暂时还没有成交写入。`;
  }
  return "这次决策没有产生执行动作。";
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
      title: "最新决策完成",
      subtitle: latestDecision.decision_id,
      timestamp: latestDecision.decision_context?.as_of_ts,
      tone: latestDecision.risk_decision?.approved ? "info" : "warning",
      detail: decisionOutcomeLabel(latestDecision.summary),
    });
  }

  if (latestValidation) {
    items.push({
      title: "对账校验",
      subtitle: latestValidation.reconciliation_id || "-",
      timestamp: latestValidation.validated_at,
      tone: latestValidation.halt_required ? "danger" : "success",
      detail: listOrDashLocalized(latestValidation.mismatch_reasons),
    });
  }

  if (!paperLocal && latestRebaselineAction) {
    items.push({
      title: "人工重建基线",
      subtitle: readableState(latestRebaselineAction.status || "-"),
      timestamp: latestRebaselineAction.created_at,
      tone: recovery.review_required ? "warning" : latestRebaselineAction.status === "rebaseline_completed" ? "info" : "danger",
      detail: localizeErrorMessage(latestRebaselineAction.reason || "操作员接受了新的可信基线。"),
    });
  }

  if (!paperLocal && latestResumeAction) {
    items.push({
      title: "人工恢复运行",
      subtitle: readableState(latestResumeAction.status || "-"),
      timestamp: latestResumeAction.created_at,
      tone: latestResumeAction.status === "resumed" || latestResumeAction.status === "already_resumed" ? "success" : "warning",
      detail: localizeErrorMessage(latestResumeAction.reason || "操作员发起了恢复请求。"),
    });
  }

  errors.slice(0, 2).forEach((item) => {
    items.push({
      title: "执行异常",
      subtitle: item.order_id || item.decision_id || "-",
      timestamp: item.timestamp,
      tone: item.severity === "error" ? "danger" : "warning",
      detail: localizeErrorMessage(item.message || item.status || "execution issue"),
    });
  });

  blockerHistory.slice(-2).forEach((item) => {
    items.push({
      title: "阻断快照",
      subtitle: readableState(item.runtime_state || "-"),
      timestamp: item.created_at,
      tone: item.execution_blocked ? "warning" : "info",
      detail: item.blockers?.length ? item.blockers.map((blocker) => localizeErrorMessage(blocker.blocker)).join("，") : "无阻断",
    });
  });

  return items.sort((left, right) => dateValue(right.timestamp) - dateValue(left.timestamp)).slice(0, 6);
}

function renderTimeline(items, emptyText = "近期没有运行活动。") {
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

function showStructuredDetail({ title, summary = [], sections = [], actions = [] }) {
  const actionsHtml = actions.length
    ? `<div class="detail-card"><div class="table-actions">${actions.join("")}</div></div>`
    : "";
  const summaryHtml = summary.length
    ? `<div class="detail-card"><h3>摘要</h3><div class="detail-grid">${summary.map(([key, value]) => `
        <div class="detail-grid-row">
          <span class="detail-key">${escapeHtml(key)}</span>
          <strong class="detail-value">${escapeHtml(value == null || value === "" ? "-" : String(value))}</strong>
        </div>
      `).join("")}</div></div>`
    : `<div class="empty-state">当前没有可展示的摘要字段。</div>`;
  const bodyHtml = sections.length
    ? sections.map((section) => `
        <section class="detail-card">
          <h3>${escapeHtml(section.title)}</h3>
          ${renderDetailNarrative(section.narrative || [])}
          ${renderDetailFacts(section.facts || [])}
          ${renderRawDetail(section.value)}
        </section>
      `).join("")
    : `<div class="empty-state">当前没有可展示的详情分区。</div>`;
  setDrawerContent(title, `${actionsHtml}${summaryHtml}`, bodyHtml);
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
    return `<div class="detail-empty-note">这一部分没有原始载荷可展示。</div>`;
  }
  return `
    <details class="detail-raw">
      <summary>原始 JSON</summary>
      <pre class="detail-json">${escapeHtml(JSON.stringify(value, null, 2))}</pre>
    </details>
  `;
}

function recentDecisionHeadline(item) {
  const intent = readableMode(item.position_target?.position_intent || item.position_intent || "hold");
  const symbol = item.symbol || "当前跟踪标的";
  const delta = Number(item.position_target?.delta_position_qty ?? item.target_delta_qty ?? item.delta_position_qty ?? 0);
  if (Math.abs(delta) < 1e-12 || intent === "hold") {
    return `继续持有 ${symbol}`;
  }
  const side = delta > 0 ? "增加敞口" : "降低敞口";
  return `${capitalizeWord(intent)} ${symbol}，并${side}`;
}

function recentDecisionNarrative(item) {
  const target = item.position_target || {};
  const current = formatSigned(target.current_position_qty ?? item.current_position_qty ?? 0);
  const next = formatSigned(target.target_position_qty ?? item.target_position_qty ?? 0);
  if (!recentDecisionRequiresTrade(item)) {
    return `当前敞口已经与目标 ${next} 一致，这一轮决策保持原姿态不变。`;
  }
  return `系统计划把敞口从 ${current} 调整到 ${next}，并按 ${readableMode(target.product_type || item.product_type || "-")} 规则执行。`;
}

function recentDecisionOutcome(item) {
  if (item.policy_result === false) {
    return `策略层因以下原因阻断了这次想法：${listOrDashLocalized(item.policy_rejection_reasons || item.policy_decision?.rejection_reasons)}。`;
  }
  if (item.risk_result === false) {
    return `风控因以下原因阻断了这次想法：${listOrDashLocalized(item.risk_rejection_reasons || item.risk_decision?.rejection_reasons)}。`;
  }
  const orderCount = Number(item.execution_result?.order_count ?? item.order_count ?? 0);
  const fillCount = Number(item.execution_result?.fill_count ?? item.fill_count ?? 0);
  if (fillCount > 0) {
    return `这次决策已产生 ${fillCount} 条成交。`;
  }
  if (orderCount > 0) {
    return `这次决策已创建 ${orderCount} 笔订单，当前仍在同步状态。`;
  }
  if (!recentDecisionRequiresTrade(item)) {
    return "这次决策通过了安全检查，并正确地保持了当前仓位，没有下单。";
  }
  return "这次决策通过了安全检查，但还没有产生执行记录。";
}

function decisionTargetNeedsTrade(target) {
  return Math.abs(Number(target?.delta_position_qty ?? 0)) >= 1e-12;
}

function recentDecisionRequiresTrade(item) {
  return Math.abs(Number(item.position_target?.delta_position_qty ?? item.target_delta_qty ?? item.delta_position_qty ?? 0)) >= 1e-12;
}

function recentOrderHeadline(order) {
  const side = readableMode(order.submission_payload?.side || order.side || "-");
  const type = readableMode(order.submission_payload?.ordType || order.order_type || "market");
  return `${capitalizeWord(side)} ${formatNumber(order.requested_qty)} ${order.symbol || "-"}，订单类型为 ${type}`;
}

function recentOrderNarrative(order) {
  const intent = readableMode(order.submission_payload?.positionIntent || order.position_intent || "-");
  return `这笔订单在 ${readableMode(order.product_type || "-")} 规则下，通过 ${order.venue || order.submission_mode || "-"} 执行 ${intent} 意图。`;
}

function recentOrderStateSummary(order) {
  return `${order.venue || order.submission_mode || "-"} | 已成交 ${formatNumber(order.filled_qty)} / ${formatNumber(order.requested_qty)}`;
}

function recentFillHeadline(fill) {
  const side = readableMode(fill.side || "-");
  return `${capitalizeWord(side)} ${formatNumber(fill.fill_qty)} ${fill.symbol || "-"}`;
}

function recentFillNarrative(fill) {
  return `以 ${formatNumber(fill.fill_price)} 的价格成交，交易意图为 ${readableMode(fill.position_intent || "-")}，产品类型为 ${readableMode(fill.product_type || "-")}。`;
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
  if (!summary) return "当前没有决策结果。";
  const orderCount = summary.execution_result?.order_count ?? 0;
  const fillCount = summary.execution_result?.fill_count ?? 0;
  if (summary.risk_result === false) return "这次决策被风控拒绝执行。";
  if (summary.policy_result === false) return "这次决策被策略层阻断执行。";
  if (orderCount === 0) return "这次决策完成了评估，但没有创建执行意图。";
  if (fillCount === 0) return "这次决策已经创建执行意图，但还没有成交写入。";
  return "这次决策已经进入执行链路，并产生了成交。";
}

function freshnessSummary(freshness) {
  const parts = [
    freshness.market_fresh ? "行情" : null,
    freshness.account_fresh ? "账户" : null,
    freshness.reconciliation_fresh ? "对账" : null,
  ].filter(Boolean);
  return parts.length === 3 ? "全部新鲜" : parts.length ? `${parts.join(" + ")} 新鲜` : "数据陈旧";
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

function decisionActivityLabel(latestDecision, latestOrder, latestFill, fallback = "当前没有决策结果。") {
  const summary = latestDecision?.summary || {};
  const result = summary.execution_result || {};
  const target = latestDecision?.position_target || {};
  if (result.fill_count > 0 && latestFill) {
    return `最近一次决策执行了 ${readableMode(target.position_intent || "executed")}，并在 ${formatRelativeAge(latestFill.ingestion_timestamp)} 产生了成交。`;
  }
  if (result.order_count > 0 && latestOrder) {
    return `最近一次决策已提交 ${readableMode(target.position_intent || "an order")}，当前正在等待或同步执行状态。`;
  }
  if (target.position_intent === "hold" || Math.abs(Number(target.delta_position_qty || 0)) < 1e-12) {
    return "最近一次决策选择继续持有当前姿态，不需要发起交易。";
  }
  if (summary.risk_result === false) {
    return "最近一次决策被风控阻断。";
  }
  if (summary.policy_result === false) {
    return "最近一次决策被策略层阻断。";
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
  nodes.overviewPostureTitle.textContent = paperLocal ? "本地模拟姿态" : "系统姿态";
  nodes.overviewPostureCopy.textContent = paperLocal
    ? "当前使用与实盘相同的决策和风控核心，但执行停留在本地模拟，不涉及交易所修复动作。"
    : "查看健康度、运行就绪状态，以及执行通道是否被阻断。";
  nodes.overviewRecoveryTitle.textContent = "恢复控制";
  nodes.overviewRecoveryCopy.textContent = "查看基线接管、人工复核姿态，以及当前是否真的可以安全恢复运行。";
  nodes.overviewRecoveryPanel.hidden = paperLocal;
  nodes.inspectRecoveryButton.hidden = paperLocal;
}

function executionHeadline(runtimeProfile, environment, mode) {
  if (runtimeProfile.name === "paper_local") {
    return "本地模拟执行";
  }
  if (environment.exchange_submission_enabled) {
    return "已开启交易所提交";
  }
  return mode.exchange_submit_allowed ? "交易所提交通道就绪" : "交易所提交通道受保护";
}

function executionModeBadge(environment, mode) {
  if (!environment.exchange_coupled) {
    return "本地模拟成交";
  }
  return mode.guarded_execution_dry_run ? "仅演练不提交" : environment.exchange_submission_enabled ? "允许真实提交" : "受保护模式";
}

function executionModeTone(environment, mode) {
  if (!environment.exchange_coupled) {
    return "outline";
  }
  return mode.guarded_execution_dry_run ? "warning" : environment.exchange_submission_enabled ? "success" : "info";
}

function executionCopy(runtimeProfile, environment, mode) {
  if (runtimeProfile.name === "paper_local") {
    return "当前配置下，所有订单都会停留在本地模拟适配器中，不会提交到交易所。";
  }
  return mode.exchange_submit_allowed ? "当前所有交易所提交门都已放行。" : listOrDashLocalized(mode.submit_blocked_reasons);
}

function recoverySummaryLine(recovery) {
  const parts = [];
  if (recovery.resume_eligible) {
    parts.push("允许恢复运行");
  } else if (recovery.review_required) {
    parts.push("需要人工复核");
  } else if (recovery.recovery_state) {
    parts.push(readableState(recovery.recovery_state));
  }
  if (recovery.resume_blocked_reasons?.length) {
    parts.push(listOrDashLocalized(recovery.resume_blocked_reasons));
  }
  return parts.length ? parts.join(" | ") : "-";
}

function formatDuration(seconds) {
  const number = Number(seconds);
  if (!Number.isFinite(number)) return "-";
  if (number < 60) return `${Math.round(number)} 秒`;
  if (number < 3600) return `${Math.floor(number / 60)} 分 ${Math.round(number % 60)} 秒`;
  return `${Math.floor(number / 3600)} 小时 ${Math.floor((number % 3600) / 60)} 分`;
}

function formatRelativeAge(value) {
  if (!value) return "-";
  const deltaSeconds = Math.max(0, Math.round((Date.now() - dateValue(value)) / 1000));
  if (!Number.isFinite(deltaSeconds)) return "-";
  if (deltaSeconds < 60) return `${deltaSeconds} 秒前`;
  if (deltaSeconds < 3600) return `${Math.floor(deltaSeconds / 60)} 分钟前`;
  if (deltaSeconds < 86400) return `${Math.floor(deltaSeconds / 3600)} 小时前`;
  return `${Math.floor(deltaSeconds / 86400)} 天前`;
}

function pluralize(count) {
  return Number(count) === 1 ? "" : "s";
}

function capitalizeWord(value) {
  const text = value == null ? "" : String(value);
  return text ? translateUiTerm(text) : "-";
}

function readableState(value) {
  return value ? translateUiTerm(value) : "-";
}

function readableMode(value) {
  return value ? translateUiTerm(value) : "-";
}

function listOrDash(value) {
  if (!value || (Array.isArray(value) && value.length === 0)) return "-";
  return Array.isArray(value) ? value.join(", ") : String(value);
}

function listOrDashLocalized(value) {
  if (!value || (Array.isArray(value) && value.length === 0)) return "-";
  return Array.isArray(value) ? value.map((item) => localizeErrorMessage(item)).join("，") : localizeErrorMessage(value);
}

function booleanWord(value) {
  if (value === true) return "是";
  if (value === false) return "否";
  return "-";
}

function booleanShort(value) {
  if (value === true) return "正常";
  if (value === false) return "阻断";
  return "未知";
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (Number.isNaN(number)) return String(value);
  const magnitude = Math.abs(number);
  if (magnitude === 0) return "0";
  if (magnitude >= 1000) return trimTrailingZeros(number.toFixed(2));
  if (magnitude >= 1) return trimTrailingZeros(number.toFixed(4));
  if (magnitude >= 0.0001) return trimTrailingZeros(number.toFixed(6));
  if (magnitude >= 0.000001) return trimTrailingZeros(number.toFixed(8));
  return number.toExponential(2);
}

function formatSigned(value) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (Number.isNaN(number)) return String(value);
  return `${number > 0 ? "+" : ""}${formatNumber(number)}`;
}

function trimTrailingZeros(value) {
  return value.replace(/(\.\d*?[1-9])0+$/u, "$1").replace(/\.0+$/u, "");
}

function formatDateTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function translateUiTerm(value) {
  const text = value == null ? "" : String(value).trim();
  if (!text) {
    return "-";
  }
  const normalized = text.toLowerCase().replaceAll(/[\s-]+/g, "_");
  return UI_TERM_MAP[normalized] || text.replaceAll("_", " ");
}

function localizeErrorMessage(message) {
  const text = String(message || "").trim();
  if (!text) {
    return "未知错误";
  }
  const exactMap = {
    "execution issue": "执行异常",
    "review the mismatch payload": "人工检查差异内容",
    "Runtime profile draft creation failed": "创建运行配置草稿失败",
    "Runtime profile save failed": "保存运行配置草稿失败",
    "Runtime profile staging failed": "设为待激活失败",
    "Runtime profile cancel failed": "取消待激活运行配置失败",
    "Restart request failed": "发起重启请求失败",
    "Operator user creation failed": "创建操作员账户失败",
    "Operator user update failed": "更新操作员账户失败",
    "Operator user deletion failed": "删除操作员账户失败",
    "Reconciliation lookup failed": "查询对账详情失败",
    "Decision lookup failed": "查询决策详情失败",
    "Order lookup failed": "查询订单详情失败",
    "Fill lookup failed": "查询成交详情失败",
    "Stuck submission resolution failed": "处理卡住提交失败",
    "ready": "就绪",
  };
  if (exactMap[text]) {
    return exactMap[text];
  }
  const direct = translateUiTerm(text);
  if (direct !== text) {
    return direct;
  }
  const rewritten = text
    .replace(/^Failed to load (.+)$/i, "加载 $1 失败")
    .replace(/^Current blocking reason:/i, "当前阻断原因：")
    .replace(/^No raw payload for this section\.$/i, "这一部分没有原始载荷可展示。");
  if (rewritten !== text) {
    return rewritten;
  }
  if (/^[a-z0-9_,\s-]+$/i.test(text) && text.includes("_")) {
    return text.split(",").map((item) => translateUiTerm(item.trim())).join("，");
  }
  return text;
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
