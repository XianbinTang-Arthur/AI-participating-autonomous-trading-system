const AUTO_REFRESH_MS = 5000;
const API_KEY_STORAGE_KEY = "aats.operator.apiKey";
const DEFAULT_PAGE = "system";
const NAVIGATION = {
  system: {
    label: "System",
    sections: [
      { id: "system-overview-section", label: "Overview" },
      { id: "system-runtime-section", label: "Runtime" },
    ],
  },
  trading: {
    label: "Trading",
    sections: [
      { id: "trading-portfolio-section", label: "Portfolio" },
      { id: "trading-decisions-section", label: "Decisions" },
    ],
  },
  execution: {
    label: "Execution",
    sections: [
      { id: "execution-summary-section", label: "Summary" },
      { id: "execution-orders-section", label: "Orders & Fills" },
    ],
  },
  diagnostics: {
    label: "Diagnostics",
    sections: [
      { id: "diagnostics-reconciliation-section", label: "Reconciliation" },
      { id: "diagnostics-inspector-section", label: "Inspector" },
    ],
  },
};

const state = {
  apiKey: window.localStorage.getItem(API_KEY_STORAGE_KEY) || "",
  refreshing: false,
  refreshTimer: null,
  lastRefreshAt: null,
  flashMessage: null,
  panelErrors: {},
  data: {},
  navigation: {
    page: initialPage(),
    sectionId: null,
  },
  selection: {
    decisionId: null,
    orderId: null,
    fillId: null,
  },
};

const nodes = mapNodes({
  runtimeStateChip: "runtimeStateChip",
  operatingStateChip: "operatingStateChip",
  executionRouteChip: "executionRouteChip",
  authStateChip: "authStateChip",
  apiKeyInput: "apiKeyInput",
  saveApiKeyButton: "saveApiKeyButton",
  clearApiKeyButton: "clearApiKeyButton",
  spotlightDecision: "spotlightDecision",
  spotlightFill: "spotlightFill",
  spotlightOpenOrders: "spotlightOpenOrders",
  spotlightReplay: "spotlightReplay",
  refreshButton: "refreshButton",
  haltButton: "haltButton",
  resumeButton: "resumeButton",
  reconcileButton: "reconcileButton",
  autoRefreshToggle: "autoRefreshToggle",
  lastRefreshLabel: "lastRefreshLabel",
  bannerContainer: "bannerContainer",
  secondaryNav: "secondaryNav",
  systemHealthStamp: "systemHealthStamp",
  overallStatusValue: "overallStatusValue",
  overallBlockersValue: "overallBlockersValue",
  executionModeValue: "executionModeValue",
  executionBlockedValue: "executionBlockedValue",
  freshnessValue: "freshnessValue",
  runtimeUptimeValue: "runtimeUptimeValue",
  storageModeValue: "storageModeValue",
  auditReplayValue: "auditReplayValue",
  subsystemsList: "subsystemsList",
  blockersList: "blockersList",
  runtimeStamp: "runtimeStamp",
  runtimeFacts: "runtimeFacts",
  metricsFacts: "metricsFacts",
  portfolioStamp: "portfolioStamp",
  portfolioFacts: "portfolioFacts",
  balancesList: "balancesList",
  positionsList: "positionsList",
  decisionStamp: "decisionStamp",
  decisionSummary: "decisionSummary",
  replayStatus: "replayStatus",
  recentDecisionsTable: "recentDecisionsTable",
  executionStamp: "executionStamp",
  executionSummary: "executionSummary",
  executionErrorsList: "executionErrorsList",
  openOrdersTable: "openOrdersTable",
  fillsTable: "fillsTable",
  reconciliationStamp: "reconciliationStamp",
  reconciliationSummary: "reconciliationSummary",
  reconciliationMismatches: "reconciliationMismatches",
  decisionLookupInput: "decisionLookupInput",
  orderLookupInput: "orderLookupInput",
  fillLookupInput: "fillLookupInput",
  loadDecisionButton: "loadDecisionButton",
  loadOrderButton: "loadOrderButton",
  loadFillButton: "loadFillButton",
  decisionInspector: "decisionInspector",
  orderInspector: "orderInspector",
  fillInspector: "fillInspector",
});

const tabButtons = Array.from(document.querySelectorAll(".tab-button"));
const tabPanels = Array.from(document.querySelectorAll(".tab-panel"));
const primaryNavButtons = Array.from(document.querySelectorAll(".primary-nav-button"));
const pageNodes = Array.from(document.querySelectorAll(".workspace-page"));

init();

function init() {
  nodes.apiKeyInput.value = state.apiKey;
  updateAuthStateChip();
  bindEvents();
  setActivePage(state.navigation.page, { updateHash: false, scrollToTop: false });
  setActiveTab("decision");
  renderEmptyInspectors();
  refreshDashboard();
}

function bindEvents() {
  nodes.saveApiKeyButton.addEventListener("click", saveApiKey);
  nodes.clearApiKeyButton.addEventListener("click", clearApiKey);
  nodes.refreshButton.addEventListener("click", () => refreshDashboard({ manual: true }));
  nodes.haltButton.addEventListener("click", () => runAction("/system/halt", { reason: "ui_manual_halt" }, "System halted."));
  nodes.resumeButton.addEventListener("click", () => runAction("/system/resume", { reason: "ui_manual_resume" }, "Resume requested. Blockers were re-evaluated."));
  nodes.reconcileButton.addEventListener("click", () => runAction("/reconciliation/validate", { reason: "ui_manual_validate" }, "Reconciliation validation requested."));
  nodes.autoRefreshToggle.addEventListener("change", () => {
    if (nodes.autoRefreshToggle.checked) {
      scheduleRefresh();
    } else {
      cancelScheduledRefresh();
    }
  });
  nodes.loadDecisionButton.addEventListener("click", () => void inspectDecision(nodes.decisionLookupInput.value.trim(), { focusTab: true, manual: true }));
  nodes.loadOrderButton.addEventListener("click", () => void inspectOrder(nodes.orderLookupInput.value.trim(), { focusTab: true, manual: true }));
  nodes.loadFillButton.addEventListener("click", () => void inspectFill(nodes.fillLookupInput.value.trim(), { focusTab: true, manual: true }));
  bindEnter(nodes.decisionLookupInput, () => void inspectDecision(nodes.decisionLookupInput.value.trim(), { focusTab: true, manual: true }));
  bindEnter(nodes.orderLookupInput, () => void inspectOrder(nodes.orderLookupInput.value.trim(), { focusTab: true, manual: true }));
  bindEnter(nodes.fillLookupInput, () => void inspectFill(nodes.fillLookupInput.value.trim(), { focusTab: true, manual: true }));
  primaryNavButtons.forEach((button) => button.addEventListener("click", () => setActivePage(button.dataset.page || DEFAULT_PAGE)));
  nodes.secondaryNav.addEventListener("click", (event) => {
    const target = event.target instanceof HTMLElement ? event.target.closest("[data-section-id]") : null;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    event.preventDefault();
    activateSection(target.dataset.sectionId || null);
  });
  window.addEventListener("hashchange", handleHashChange);
  tabButtons.forEach((button) => button.addEventListener("click", () => setActiveTab(button.dataset.tab || "decision")));
  document.addEventListener("click", (event) => {
    const target = event.target instanceof HTMLElement ? event.target.closest("[data-inspect-kind]") : null;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const kind = target.dataset.inspectKind;
    const identifier = target.dataset.inspectId;
    if (!kind || !identifier) {
      return;
    }
    if (kind === "decision") {
      void inspectDecision(identifier, { focusTab: true, manual: true });
    } else if (kind === "order") {
      void inspectOrder(identifier, { focusTab: true, manual: true });
    } else if (kind === "fill") {
      void inspectFill(identifier, { focusTab: true, manual: true });
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

function initialPage() {
  const raw = window.location.hash.replace("#", "").trim().toLowerCase();
  return Object.prototype.hasOwnProperty.call(NAVIGATION, raw) ? raw : DEFAULT_PAGE;
}

function handleHashChange() {
  const nextPage = initialPage();
  if (nextPage !== state.navigation.page) {
    setActivePage(nextPage, { updateHash: false, scrollToTop: false });
  }
}

function setActivePage(page, { updateHash = true, scrollToTop = true } = {}) {
  const nextPage = Object.prototype.hasOwnProperty.call(NAVIGATION, page) ? page : DEFAULT_PAGE;
  state.navigation.page = nextPage;
  state.navigation.sectionId = NAVIGATION[nextPage].sections[0]?.id || null;
  primaryNavButtons.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.page === nextPage);
  });
  pageNodes.forEach((node) => {
    node.classList.toggle("is-active", node.dataset.page === nextPage);
  });
  renderSecondaryNavigation();
  if (updateHash && window.location.hash !== `#${nextPage}`) {
    window.location.hash = nextPage;
  }
  if (scrollToTop) {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
}

function renderSecondaryNavigation() {
  const pageConfig = NAVIGATION[state.navigation.page] || NAVIGATION[DEFAULT_PAGE];
  nodes.secondaryNav.innerHTML = pageConfig.sections.map((section, index) => `
    <button
      class="secondary-nav-link ${index === 0 ? "is-active" : ""}"
      data-section-id="${escapeHtml(section.id)}"
      type="button"
    >
      ${escapeHtml(section.label)}
    </button>
  `).join("");
}

function activateSection(sectionId) {
  if (!sectionId) {
    return;
  }
  state.navigation.sectionId = sectionId;
  const sectionNode = document.getElementById(sectionId);
  if (sectionNode) {
    sectionNode.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  Array.from(nodes.secondaryNav.querySelectorAll("[data-section-id]")).forEach((button) => {
    button.classList.toggle("is-active", button.getAttribute("data-section-id") === sectionId);
  });
}

async function refreshDashboard({ manual = false } = {}) {
  if (state.refreshing) {
    return;
  }
  state.refreshing = true;
  setActionButtonsBusy(true);
  cancelScheduledRefresh();

  const specs = [
    ["health", "/system/health"],
    ["mode", "/system/mode"],
    ["runtime", "/system/runtime"],
    ["blockers", "/system/blockers"],
    ["metrics", "/system/metrics"],
    ["portfolio", "/portfolio/latest"],
    ["balances", "/balances"],
    ["positions", "/positions"],
    ["accountState", "/account/state"],
    ["latestDecision", "/decision/latest"],
    ["recentDecisions", "/decision/recent?limit=8"],
    ["replayStatus", "/replay/status"],
    ["executionLatest", "/execution/latest"],
    ["openOrders", "/orders/open"],
    ["recentFills", "/fills/recent?limit=8"],
    ["executionErrors", "/execution/errors"],
    ["reconciliationLatest", "/reconciliation/latest"],
    ["reconciliationMismatches", "/reconciliation/mismatches?limit=8"],
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
  await refreshSelectedInspectors();
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
  if (state.apiKey) {
    headers.set("X-AATS-API-Key", state.apiKey);
  }
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

function setActionButtonsBusy(busy) {
  [nodes.refreshButton, nodes.haltButton, nodes.resumeButton, nodes.reconcileButton].forEach((node) => {
    node.disabled = busy;
  });
  nodes.refreshButton.textContent = busy ? "Refreshing..." : "Refresh now";
}

function scheduleRefresh() {
  cancelScheduledRefresh();
  if (!nodes.autoRefreshToggle.checked) {
    return;
  }
  state.refreshTimer = window.setTimeout(() => {
    void refreshDashboard();
  }, AUTO_REFRESH_MS);
}

function cancelScheduledRefresh() {
  if (state.refreshTimer !== null) {
    window.clearTimeout(state.refreshTimer);
    state.refreshTimer = null;
  }
}

function saveApiKey() {
  state.apiKey = nodes.apiKeyInput.value.trim();
  if (state.apiKey) {
    window.localStorage.setItem(API_KEY_STORAGE_KEY, state.apiKey);
    flash("API key saved locally.", "info");
  } else {
    window.localStorage.removeItem(API_KEY_STORAGE_KEY);
    flash("API key cleared.", "info");
  }
  updateAuthStateChip();
  void refreshDashboard({ manual: true });
}

function clearApiKey() {
  nodes.apiKeyInput.value = "";
  state.apiKey = "";
  window.localStorage.removeItem(API_KEY_STORAGE_KEY);
  flash("API key cleared.", "info");
  updateAuthStateChip();
  void refreshDashboard({ manual: true });
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

function flash(message, tone = "info") {
  state.flashMessage = { message, tone, timestamp: new Date() };
}

function updateAuthStateChip() {
  const authEnabled = Boolean(state.data.runtime?.operator_auth_enabled);
  const denied = Object.values(state.panelErrors).some((error) => error?.status === 401 || error?.status === 403);
  let text = "anonymous";
  let tone = "outline";
  if (denied) {
    text = "access denied";
    tone = "danger";
  } else if (authEnabled && state.apiKey) {
    text = "key loaded";
    tone = "success";
  } else if (authEnabled) {
    text = "auth required";
    tone = "warning";
  } else if (state.apiKey) {
    text = "key cached";
    tone = "neutral";
  }
  nodes.authStateChip.textContent = text;
  nodes.authStateChip.className = `chip ${chipClass(tone)}`;
}

function renderDashboard({ manual = false } = {}) {
  updateAuthStateChip();
  renderBanners();
  renderSystemOverview();
  renderRuntime();
  renderPortfolio();
  renderDecision();
  renderExecution();
  renderReconciliation();
  renderSpotlight();
  nodes.lastRefreshLabel.textContent = state.lastRefreshAt
    ? `Last refresh ${formatDateTime(state.lastRefreshAt)}${manual ? " | manual" : ""}`
    : "Not refreshed yet";
}

function renderBanners() {
  const banners = [];
  const health = state.data.health || {};
  const blockers = state.data.blockers?.blockers || [];

  if (state.flashMessage) {
    banners.push({ tone: state.flashMessage.tone, message: state.flashMessage.message });
  }
  if (health.runtime_state === "halted") {
    banners.push({ tone: "danger", message: "The runtime is halted. Execution is stopped." });
  } else if (health.runtime_state === "blocked") {
    banners.push({ tone: "warning", message: "Execution is currently blocked by active blockers." });
  } else if (health.runtime_state === "degraded") {
    banners.push({ tone: "info", message: "The runtime is degraded. Inspect warnings and freshness." });
  }
  blockers.slice(0, 3).forEach((blocker) => {
    banners.push({
      tone: blocker.affects_execution ? "warning" : "info",
      message: `${blocker.subsystem}: ${blocker.blocker} | ${blocker.recommended_action}`,
    });
  });
  Object.entries(state.panelErrors).slice(0, 4).forEach(([panel, error]) => {
    banners.push({
      tone: error.status === 401 || error.status === 403 ? "danger" : "warning",
      message: `Panel ${panel} failed: ${error.message}`,
    });
  });

  nodes.bannerContainer.innerHTML = banners.length
    ? banners.slice(0, 6).map((banner) => `<div class="banner banner-${banner.tone}">${escapeHtml(banner.message)}</div>`).join("")
    : "";
}

function renderSystemOverview() {
  const health = state.data.health || {};
  const mode = state.data.mode || {};
  const blockers = state.data.blockers?.blockers || [];
  const fresh = health.freshness || {};
  const runtime = state.data.runtime || {};

  setStamp(nodes.systemHealthStamp, health.last_success_timestamps?.market || health.last_success_timestamps?.reconciliation);
  nodes.overallStatusValue.textContent = readableState(health.runtime_state || health.overall_status);
  nodes.overallBlockersValue.textContent = blockers.length ? `${blockers.length} blockers` : "No active blockers";
  nodes.executionModeValue.textContent = readableMode(mode.operating_state || health.operating_state);
  nodes.executionBlockedValue.textContent = mode.execution_blocked
    ? `blocked | ${mode.blocked_reason || "unknown"}`
    : (mode.submit_blocked ? "submit-only blocked" : "execution ready");
  nodes.freshnessValue.textContent = [
    fresh.market_fresh ? "market fresh" : "market stale",
    fresh.account_fresh ? "account fresh" : "account stale",
    fresh.reconciliation_fresh ? "recon fresh" : "recon stale",
  ].join(" | ");
  nodes.runtimeUptimeValue.textContent = runtime.uptime_seconds != null ? `uptime ${formatDuration(runtime.uptime_seconds)}` : "-";
  nodes.storageModeValue.textContent = runtime.storage_mode || "-";
  nodes.auditReplayValue.textContent = health.subsystems?.audit_replay
    ? `${health.subsystems.audit_replay.audit_record_count || 0} audits / ${health.subsystems.audit_replay.fresh ? "replay warm" : "replay idle"}`
    : "-";

  nodes.runtimeStateChip.textContent = readableState(health.runtime_state || "unknown");
  nodes.runtimeStateChip.className = `chip ${runtimeStateClass(health.runtime_state)}`;
  nodes.operatingStateChip.textContent = readableMode(mode.operating_state || "unknown");
  nodes.operatingStateChip.className = `chip ${chipClass(mode.submit_blocked ? "warning" : "outline")}`;
  nodes.executionRouteChip.textContent = mode.execution_route || mode.execution_backend || "unknown";
  nodes.executionRouteChip.className = `chip ${chipClass(mode.exchange_submit_allowed ? "success" : "outline")}`;

  renderKeyList(nodes.subsystemsList, [
    summaryKeyRow("Market Data", subsystemSummary(health.subsystems?.market_data)),
    summaryKeyRow("Account State", subsystemSummary(health.subsystems?.account_state)),
    summaryKeyRow("Execution Adapter", subsystemSummary(health.subsystems?.execution_adapter)),
    summaryKeyRow("Reconciliation", subsystemSummary(health.subsystems?.reconciliation)),
    summaryKeyRow("Storage", subsystemSummary(health.subsystems?.storage)),
    summaryKeyRow("Audit / Replay", subsystemSummary(health.subsystems?.audit_replay)),
  ]);

  renderStackList(
    nodes.blockersList,
    blockers,
    (item) => `
      <div class="item">
        <div class="item-title">
          <span>${escapeHtml(item.blocker)}</span>
          <span class="chip ${chipClass(item.affects_execution ? "danger" : "warning")}">${item.affects_execution ? "execution" : "submit-only"}</span>
        </div>
        <div class="item-meta">${escapeHtml(item.subsystem)} | ${escapeHtml(item.recommended_action || "Inspect subsystem status.")}</div>
      </div>
    `,
    "No active blockers."
  );
}

function renderRuntime() {
  const runtime = state.data.runtime || {};
  const mode = state.data.mode || {};
  const metrics = state.data.metrics || {};
  setStamp(nodes.runtimeStamp, runtime.startup_timestamp);

  renderKeyList(nodes.runtimeFacts, [
    summaryKeyRow("Profile", mode.config_profile || "-"),
    summaryKeyRow("Mode", readableMode(mode.mode)),
    summaryKeyRow("Operating State", readableMode(mode.operating_state)),
    summaryKeyRow("Market Backend", mode.market_data_backend || "-"),
    summaryKeyRow("Account Backend", mode.account_backend || "-"),
    summaryKeyRow("Execution Backend", mode.execution_backend || "-"),
    summaryKeyRow("AI Mode", mode.ai_operating_mode || "-"),
    summaryKeyRow("Symbols", listOrDash(runtime.symbols)),
    summaryKeyRow("Timeframes", listOrDash(runtime.enabled_timeframes)),
    summaryKeyRow("Decision Cadence", runtime.decision_cadence ? `15m=${runtime.decision_cadence.decision_min_interval_seconds_15m}s | 1h=${runtime.decision_cadence.decision_min_interval_seconds_1h}s` : "-"),
    summaryKeyRow("Last Decision", formatMaybeTimestamp(runtime.last_decision_timestamp)),
    summaryKeyRow("Last Fill", formatMaybeTimestamp(runtime.last_fill_timestamp)),
    summaryKeyRow("Last Reconciliation", formatMaybeTimestamp(runtime.last_reconciliation_timestamp)),
  ]);

  renderKeyList(nodes.metricsFacts, [
    summaryKeyRow("Decision Cycles", formatNumber(metrics.decision_cycle_count)),
    summaryKeyRow("Order Intents", formatNumber(metrics.order_intent_count)),
    summaryKeyRow("Fills", formatNumber(metrics.fill_count)),
    summaryKeyRow("Rejections", formatNumber(metrics.rejection_count)),
    summaryKeyRow("Recon Mismatches", formatNumber(metrics.reconciliation_mismatch_count)),
    summaryKeyRow("Open Orders", formatNumber(metrics.current_open_order_count)),
    summaryKeyRow("Exposure", metrics.exposure_summary ? `gross ${formatNumber(metrics.exposure_summary.gross_exposure)} | net ${formatSigned(metrics.exposure_summary.net_exposure)}` : "-"),
  ]);
}

function renderPortfolio() {
  const portfolio = state.data.portfolio?.portfolio;
  const balances = state.data.balances || {};
  const positions = state.data.positions || {};
  const accountState = state.data.accountState || {};

  setStamp(nodes.portfolioStamp, state.data.portfolio?.latest_update_timestamp);
  renderKeyList(nodes.portfolioFacts, [
    summaryKeyRow("Snapshot", formatMaybeTimestamp(portfolio?.snapshot_ts)),
    summaryKeyRow("Total Equity", formatNumber(portfolio?.total_equity)),
    summaryKeyRow("Realized PnL", formatSigned(portfolio?.realized_pnl)),
    summaryKeyRow("Unrealized PnL", formatSigned(portfolio?.unrealized_pnl)),
    summaryKeyRow("Gross Exposure", formatNumber(portfolio?.gross_exposure)),
    summaryKeyRow("Net Exposure", formatSigned(portfolio?.net_exposure)),
    summaryKeyRow("Account Fresh", accountState.fresh ? "yes" : "no"),
    summaryKeyRow("Account Status", accountState.current_blocking_reason || (accountState.ready ? "ready" : "not ready")),
  ]);

  const balanceItems = [];
  Object.entries(balances.local_balances || {}).forEach(([asset, amount]) => {
    balanceItems.push({ title: `Local ${asset}`, meta: `amount ${formatNumber(amount)}` });
  });
  (balances.exchange_balances || []).slice(0, 8).forEach((item) => {
    balanceItems.push({
      title: `Exchange ${item.asset || item.ccy || "-"}`,
      meta: `available ${formatNumber(item.available || item.avail_bal || item.bal)} | frozen ${formatNumber(item.frozen || item.frozen_bal)}`,
    });
  });
  renderStackList(nodes.balancesList, balanceItems, renderSimpleItem, "No balance data.");

  const positionItems = [];
  (positions.local_positions || []).forEach((position) => {
    positionItems.push({
      title: `Local ${position.symbol || "-"}`,
      meta: `qty ${formatSigned(position.quantity)} | avg ${formatNumber(position.avg_entry_price)} | unrealized ${formatSigned(position.unrealized_pnl)}`,
    });
  });
  (positions.exchange_positions || []).slice(0, 8).forEach((position) => {
    positionItems.push({
      title: `Exchange ${position.symbol || position.inst_id || "-"}`,
      meta: `qty ${formatSigned(position.quantity || position.position_qty || position.pos)} | side ${position.side || "-"}`,
    });
  });
  renderStackList(nodes.positionsList, positionItems, renderSimpleItem, "No positions.");
}

function renderDecision() {
  const latest = state.data.latestDecision || {};
  const summary = latest.summary || {};
  const recent = state.data.recentDecisions?.decisions || [];
  const replay = state.data.replayStatus || {};

  setStamp(nodes.decisionStamp, latest.decision_context?.as_of_ts || summary.decision_time);
  renderStackList(
    nodes.decisionSummary,
    [
      {
        title: latest.decision_id || "No decision yet",
        meta: latest.decision_context ? `${latest.decision_context.symbol} | ${latest.decision_context.timeframe} | ${formatMaybeTimestamp(latest.decision_context.as_of_ts)}` : "Waiting for decision events",
      },
      {
        title: `Target ${formatSigned(latest.position_target?.delta_position_qty)}`,
        meta: `policy ${booleanText(latest.policy_decision?.execution_allowed)} | risk ${booleanText(latest.risk_decision?.approved)}`,
      },
      {
        title: `Baseline ${latest.baseline_assessment?.market_bias || "-"}`,
        meta: `confidence ${formatNumber(latest.baseline_assessment?.confidence)} | AI ${latest.ai_assessment?.assessment_mode || "n/a"}`,
      },
      {
        title: `Execution ${summary.execution_result?.order_count || 0} intents / ${summary.execution_result?.fill_count || 0} fills`,
        meta: summary.execution_result?.reconciled ? "reconciled" : "awaiting reconciliation",
      },
    ],
    renderSimpleItem,
    "No decision data."
  );

  renderStackList(
    nodes.replayStatus,
    [
      {
        title: replay.healthy === undefined ? "Replay unavailable" : (replay.healthy ? "Replay healthy" : "Replay divergence"),
        meta: replay.last_validation ? `divergence ${replay.last_validation.divergence_count} | validated ${formatMaybeTimestamp(replay.last_validation.validated_at)}` : "No replay validation yet",
      },
      {
        title: "Decision Chain",
        meta: replay.last_validation ? `${(replay.last_validation.decision_chain_issues || []).length} issues` : "-",
      },
      {
        title: "Execution Chain",
        meta: replay.last_validation ? `${(replay.last_validation.execution_chain_issues || []).length} issues` : "-",
      },
    ],
    renderSimpleItem,
    "No replay status."
  );

  renderTable(nodes.recentDecisionsTable, {
    columns: [
      { key: "decision_id", label: "Decision" },
      { key: "symbol", label: "Symbol" },
      { key: "decision_time", label: "Time" },
      { key: "target_delta_qty", label: "Target Delta" },
      { key: "policy_result", label: "Policy" },
      { key: "risk_result", label: "Risk" },
      { key: "execution_result", label: "Execution" },
    ],
    rows: recent.map((item) => ({
      decision_id: inspectButton("decision", item.decision_id, item.decision_id),
      symbol: item.symbol || "-",
      decision_time: formatMaybeTimestamp(item.decision_time),
      target_delta_qty: formatSigned(item.target_delta_qty),
      policy_result: booleanText(item.policy_result),
      risk_result: booleanText(item.risk_result),
      execution_result: `${item.execution_result?.order_count || 0} / ${item.execution_result?.fill_count || 0}`,
    })),
    emptyText: "No recent decisions.",
  });
}

function renderExecution() {
  const execution = state.data.executionLatest || {};
  const readiness = execution.execution || {};
  const errors = state.data.executionErrors?.errors || [];
  const openOrders = state.data.openOrders || {};
  const fills = state.data.recentFills?.fills || [];

  setStamp(nodes.executionStamp, execution.latest_order?.last_update_ts || execution.latest_fill?.ingestion_timestamp);
  renderKeyList(nodes.executionSummary, [
    summaryKeyRow("Route", execution.mode?.execution_route || execution.mode?.execution_backend || "-"),
    summaryKeyRow("Submit Allowed", execution.mode?.exchange_submit_allowed ? "yes" : "no"),
    summaryKeyRow("Submit Blocked", execution.mode?.submit_blocked ? listOrDash(execution.mode?.submit_blocked_reasons) : "no"),
    summaryKeyRow("Adapter Ready", booleanText(readiness.ready)),
    summaryKeyRow("Exchange Target", readiness.exchange_submit_target || execution.mode?.exchange_submit_target || "-"),
    summaryKeyRow("Latest Order", execution.latest_order ? `${execution.latest_order.status} | ${execution.latest_order.client_order_id}` : "-"),
    summaryKeyRow("Latest Fill", execution.latest_fill ? `${execution.latest_fill.fill_id} | ${formatSigned(execution.latest_fill.quantity)}` : "-"),
    summaryKeyRow("Recovery", execution.recovery?.safe_startup ? "safe" : "review required"),
  ]);

  renderStackList(
    nodes.executionErrorsList,
    errors.slice(0, 6),
    (item) => `
      <div class="item">
        <div class="item-title">
          <span>${escapeHtml(item.message || item.status || "execution error")}</span>
          <span class="chip ${chipClass(item.severity === "error" ? "danger" : "warning")}">${escapeHtml(item.severity || "warning")}</span>
        </div>
        <div class="item-meta">${escapeHtml(item.subsystem || "execution")} | ${escapeHtml(formatMaybeTimestamp(item.timestamp))}${item.order_id ? ` | ${escapeHtml(item.order_id)}` : ""}</div>
      </div>
    `,
    "No execution errors."
  );

  renderTable(nodes.openOrdersTable, {
    columns: [
      { key: "order", label: "Order" },
      { key: "symbol", label: "Symbol" },
      { key: "side", label: "Side" },
      { key: "quantity", label: "Qty" },
      { key: "status", label: "Status" },
      { key: "exchange", label: "Exchange" },
    ],
    rows: (openOrders.local_open_orders || []).map((order) => ({
      order: inspectButton("order", order.client_order_id, order.client_order_id),
      symbol: order.symbol || "-",
      side: order.side || "-",
      quantity: formatNumber(order.quantity),
      status: order.status || "-",
      exchange: order.exchange_order_id || "-",
    })),
    emptyText: "No open orders.",
  });

  renderTable(nodes.fillsTable, {
    columns: [
      { key: "fill", label: "Fill" },
      { key: "decision", label: "Decision" },
      { key: "side", label: "Side" },
      { key: "quantity", label: "Qty" },
      { key: "price", label: "Price" },
      { key: "time", label: "Exchange Time" },
    ],
    rows: fills.map((fill) => ({
      fill: inspectButton("fill", fill.fill_id, fill.fill_id),
      decision: fill.decision_id || "-",
      side: fill.side || "-",
      quantity: formatNumber(fill.quantity),
      price: formatNumber(fill.price),
      time: formatMaybeTimestamp(fill.exchange_timestamp || fill.ingestion_timestamp),
    })),
    emptyText: "No recent fills.",
  });
}

function renderReconciliation() {
  const latest = state.data.reconciliationLatest || {};
  const report = latest.reconciliation;
  const mismatches = state.data.reconciliationMismatches?.mismatches || [];

  setStamp(nodes.reconciliationStamp, report?.as_of_ts);
  renderStackList(
    nodes.reconciliationSummary,
    [
      {
        title: report ? `${report.severity} | ${report.reconciliation_id}` : "No reconciliation report",
        meta: report ? `${report.halt_required ? "halt required" : "no halt"} | ${formatMaybeTimestamp(report.as_of_ts)}` : "Waiting for reconciliation output",
      },
      {
        title: "Mismatch Reasons",
        meta: latest.mismatch_summary?.mismatch_reasons?.length ? latest.mismatch_summary.mismatch_reasons.join(" | ") : "clean",
      },
      {
        title: "Safety Impact",
        meta: latest.mismatch_summary?.safety_impacts?.length ? latest.mismatch_summary.safety_impacts.join(" | ") : "none",
      },
      {
        title: "Last Validation",
        meta: latest.latest_validation ? `${latest.latest_validation.trigger} | ${formatMaybeTimestamp(latest.latest_validation.validated_at)}` : "No validation yet",
      },
    ],
    renderSimpleItem,
    "No reconciliation data."
  );

  renderStackList(
    nodes.reconciliationMismatches,
    mismatches,
    (item) => `
      <div class="item">
        <div class="item-title">
          <span>${escapeHtml(item.severity || "-")}</span>
          <span class="chip ${chipClass(item.halt_required ? "danger" : "warning")}">${item.halt_required ? "halt" : "review"}</span>
        </div>
        <div class="item-meta">${escapeHtml((item.mismatch_reasons || []).join(" | ") || "no mismatch reasons")}</div>
      </div>
    `,
    "No recent mismatches."
  );
}

function renderSpotlight() {
  const latestDecision = state.data.latestDecision || {};
  const latestFill = state.data.executionLatest?.latest_fill;
  const replay = state.data.replayStatus || {};
  const openOrders = state.data.openOrders?.local_open_orders || [];
  nodes.spotlightDecision.textContent = latestDecision.decision_id || "-";
  nodes.spotlightFill.textContent = latestFill ? `${formatNumber(latestFill.quantity)} @ ${formatNumber(latestFill.price)}` : "-";
  nodes.spotlightOpenOrders.textContent = String(openOrders.length);
  nodes.spotlightReplay.textContent = replay.last_validation ? (replay.healthy ? "healthy" : `divergence ${replay.last_validation.divergence_count}`) : "idle";
}

async function refreshSelectedInspectors() {
  const actions = [];
  if (!state.selection.decisionId && state.data.latestDecision?.decision_id) {
    state.selection.decisionId = state.data.latestDecision.decision_id;
    nodes.decisionLookupInput.value = state.selection.decisionId;
  }
  if (state.selection.decisionId) {
    actions.push(inspectDecision(state.selection.decisionId, { silent: true }));
  }
  if (state.selection.orderId) {
    actions.push(inspectOrder(state.selection.orderId, { silent: true }));
  }
  if (state.selection.fillId) {
    actions.push(inspectFill(state.selection.fillId, { silent: true }));
  }
  if (actions.length) {
    await Promise.all(actions);
  }
}

async function inspectDecision(decisionId, options = {}) {
  if (!decisionId) {
    if (options.manual) {
      flash("Enter a valid decision ID.", "warning");
      renderBanners();
    }
    return;
  }
  try {
    const detail = await requestJson(`/decision/${encodeURIComponent(decisionId)}`);
    state.selection.decisionId = decisionId;
    nodes.decisionLookupInput.value = decisionId;
    if (options.focusTab) {
      setActivePage("trading", { scrollToTop: false });
      setActiveTab("decision");
    }
    renderDecisionInspector(detail);
  } catch (error) {
    if (!options.silent) {
      flash(`Decision lookup failed: ${normalizeError(error, "Decision lookup failed").message}`, "danger");
      renderBanners();
    }
  }
}

async function inspectOrder(orderId, options = {}) {
  if (!orderId) {
    if (options.manual) {
      flash("Enter a valid order ID.", "warning");
      renderBanners();
    }
    return;
  }
  try {
    const detail = await requestJson(`/orders/${encodeURIComponent(orderId)}`);
    state.selection.orderId = orderId;
    nodes.orderLookupInput.value = orderId;
    if (options.focusTab) {
      setActivePage("execution", { scrollToTop: false });
      setActiveTab("order");
    }
    renderOrderInspector(detail);
  } catch (error) {
    if (!options.silent) {
      flash(`Order lookup failed: ${normalizeError(error, "Order lookup failed").message}`, "danger");
      renderBanners();
    }
  }
}

async function inspectFill(fillId, options = {}) {
  if (!fillId) {
    if (options.manual) {
      flash("Enter a valid fill ID.", "warning");
      renderBanners();
    }
    return;
  }
  try {
    const detail = await requestJson(`/fills/${encodeURIComponent(fillId)}`);
    state.selection.fillId = fillId;
    nodes.fillLookupInput.value = fillId;
    if (options.focusTab) {
      setActivePage("execution", { scrollToTop: false });
      setActiveTab("fill");
    }
    renderFillInspector(detail);
  } catch (error) {
    if (!options.silent) {
      flash(`Fill lookup failed: ${normalizeError(error, "Fill lookup failed").message}`, "danger");
      renderBanners();
    }
  }
}

function renderDecisionInspector(detail) {
  nodes.decisionInspector.innerHTML = [
    detailBlock("Decision Summary", renderDetailGrid([
      summaryKeyRow("Decision ID", detail.decision_id || "-"),
      summaryKeyRow("Symbol", detail.decision_context?.symbol || "-"),
      summaryKeyRow("Timeframe", detail.decision_context?.timeframe || "-"),
      summaryKeyRow("As Of", formatMaybeTimestamp(detail.decision_context?.as_of_ts)),
      summaryKeyRow("Policy", booleanText(detail.policy_decision?.execution_allowed)),
      summaryKeyRow("Risk", booleanText(detail.risk_decision?.approved)),
      summaryKeyRow("Target Qty", formatNumber(detail.position_target?.target_position_qty)),
      summaryKeyRow("Target Delta", formatSigned(detail.position_target?.delta_position_qty)),
    ])),
    detailBlock("Assessments", renderDetailGrid([
      summaryKeyRow("Baseline Bias", detail.baseline_assessment?.market_bias || "-"),
      summaryKeyRow("Baseline Confidence", formatNumber(detail.baseline_assessment?.confidence)),
      summaryKeyRow("AI Mode", detail.ai_assessment?.assessment_mode || detail.ai_assessment?.provider || "n/a"),
      summaryKeyRow("AI Confidence", formatNumber(detail.ai_assessment?.confidence)),
    ])),
    detailBlock("Execution Chain", renderDetailGrid([
      summaryKeyRow("Execution Plan", detail.execution_plan?._event_id || "-"),
      summaryKeyRow("Order Intents", formatNumber(detail.order_intents?.length)),
      summaryKeyRow("Order Updates", formatNumber(detail.order_updates?.length)),
      summaryKeyRow("Fills", formatNumber(detail.fills?.length)),
      summaryKeyRow("Portfolio Snapshot", detail.portfolio_snapshot?._event_id || "-"),
      summaryKeyRow("Reconciliations", formatNumber(detail.reconciliations?.length)),
    ])),
    detailBlock("Audit Refs", `<pre class="detail-json mono">${escapeHtml(JSON.stringify({
      decision_context_ref: detail.audit?.decision_context_ref,
      baseline_assessment_ref: detail.audit?.baseline_assessment_ref,
      ai_market_assessment_ref: detail.audit?.ai_market_assessment_ref,
      position_target_ref: detail.audit?.position_target_ref,
      policy_decision_ref: detail.audit?.policy_decision_ref,
      risk_decision_ref: detail.audit?.risk_decision_ref,
      execution_plan_ref: detail.audit?.execution_plan_ref,
      order_intent_refs: detail.audit?.order_intent_refs,
      order_state_refs: detail.audit?.order_state_refs,
      fill_event_refs: detail.audit?.fill_event_refs,
      portfolio_delta_ref: detail.audit?.portfolio_delta_ref,
      reconciliation_refs: detail.audit?.reconciliation_refs,
    }, null, 2))}</pre>`),
  ].join("");
}

function renderOrderInspector(detail) {
  const order = detail.order || {};
  const fills = detail.fills || [];
  nodes.orderInspector.innerHTML = [
    detailBlock("Order Summary", renderDetailGrid([
      summaryKeyRow("Order ID", order.client_order_id || "-"),
      summaryKeyRow("Decision ID", order.decision_id || "-"),
      summaryKeyRow("Symbol", order.symbol || "-"),
      summaryKeyRow("Side", order.side || "-"),
      summaryKeyRow("Status", order.status || "-"),
      summaryKeyRow("Quantity", formatNumber(order.quantity)),
      summaryKeyRow("Filled Qty", formatNumber(order.filled_quantity)),
      summaryKeyRow("Remaining Qty", formatNumber(order.remaining_quantity)),
      summaryKeyRow("Avg Fill Price", formatNumber(order.average_fill_price)),
      summaryKeyRow("Exchange Order ID", order.exchange_order_id || "-"),
    ])),
    detailBlock("Lifecycle", renderDetailGrid([
      summaryKeyRow("Created", formatMaybeTimestamp(order.created_at)),
      summaryKeyRow("Submitted", formatMaybeTimestamp(order.submitted_at)),
      summaryKeyRow("Last Update", formatMaybeTimestamp(order.last_update_ts)),
      summaryKeyRow("Cancel Requested", formatMaybeTimestamp(order.cancel_requested_at)),
      summaryKeyRow("Cancel Reason", order.cancel_reason || "-"),
      summaryKeyRow("Execution Error", order.execution_error || "-"),
    ])),
    detailBlock("Linked Fills", fills.length ? fills.map((fill) => `
      <div class="item">
        <div class="item-title">
          <button class="table-row-button mono" data-inspect-kind="fill" data-inspect-id="${escapeHtml(fill.fill_id)}">${escapeHtml(fill.fill_id)}</button>
          <span class="chip ${chipClass("success")}">${escapeHtml(fill.side || "-")}</span>
        </div>
        <div class="item-meta">${formatNumber(fill.quantity)} @ ${formatNumber(fill.price)} | ${formatMaybeTimestamp(fill.exchange_timestamp || fill.ingestion_timestamp)}</div>
      </div>
    `).join("") : `<div class="empty">No fills for this order.</div>`),
  ].join("");
}

function renderFillInspector(detail) {
  const fill = detail.fill || {};
  nodes.fillInspector.innerHTML = [
    detailBlock("Fill Summary", renderDetailGrid([
      summaryKeyRow("Fill ID", fill.fill_id || "-"),
      summaryKeyRow("Decision ID", fill.decision_id || "-"),
      summaryKeyRow("Order ID", fill.client_order_id || fill.order_id || "-"),
      summaryKeyRow("Symbol", fill.symbol || "-"),
      summaryKeyRow("Side", fill.side || "-"),
      summaryKeyRow("Quantity", formatNumber(fill.quantity)),
      summaryKeyRow("Price", formatNumber(fill.price)),
      summaryKeyRow("Fee", formatNumber(fill.fee)),
    ])),
    detailBlock("Timing & Venue", renderDetailGrid([
      summaryKeyRow("Venue", fill.venue || "-"),
      summaryKeyRow("Exchange Timestamp", formatMaybeTimestamp(fill.exchange_timestamp)),
      summaryKeyRow("Ingestion Timestamp", formatMaybeTimestamp(fill.ingestion_timestamp)),
      summaryKeyRow("Order Status After Fill", fill.order_status_after_fill || "-"),
    ])),
  ].join("");
}

function renderEmptyInspectors() {
  nodes.decisionInspector.innerHTML = `<div class="empty">Select a decision to inspect the full chain.</div>`;
  nodes.orderInspector.innerHTML = `<div class="empty">Select an order to inspect lifecycle and fills.</div>`;
  nodes.fillInspector.innerHTML = `<div class="empty">Select a fill to inspect execution detail.</div>`;
}

function setActiveTab(tab) {
  tabButtons.forEach((button) => button.classList.toggle("is-active", button.dataset.tab === tab));
  tabPanels.forEach((panel) => panel.classList.toggle("is-active", panel.id === `tab-${tab}`));
}

function renderKeyList(node, rows) {
  node.innerHTML = rows.length
    ? rows.map((row) => `<div class="key-row"><span>${escapeHtml(String(row.label))}</span><strong>${escapeHtml(String(row.value))}</strong></div>`).join("")
    : `<div class="empty">No data.</div>`;
}

function renderStackList(node, items, renderer, emptyText = "No data.") {
  node.innerHTML = items.length ? items.map(renderer).join("") : `<div class="empty">${escapeHtml(emptyText)}</div>`;
}

function renderTable(node, config) {
  if (!config.rows.length) {
    node.innerHTML = `<div class="empty">${escapeHtml(config.emptyText || "No data.")}</div>`;
    return;
  }
  const header = config.columns.map((column) => `<th>${escapeHtml(column.label)}</th>`).join("");
  const body = config.rows.map((row) => `<tr>${config.columns.map((column) => `<td data-label="${escapeHtml(column.label)}">${row[column.key] ?? "-"}</td>`).join("")}</tr>`).join("");
  node.innerHTML = `<table><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table>`;
}

function detailBlock(title, content) {
  return `<section class="detail-block"><h4>${escapeHtml(title)}</h4>${content}</section>`;
}

function renderDetailGrid(rows) {
  return `<div class="detail-grid">${rows.map((row) => `<div class="detail-grid-row"><span>${escapeHtml(String(row.label))}</span><strong>${escapeHtml(String(row.value))}</strong></div>`).join("")}</div>`;
}

function renderSimpleItem(item) {
  return `<div class="item"><div class="item-title">${escapeHtml(item.title)}</div><div class="item-meta">${escapeHtml(item.meta)}</div></div>`;
}

function summaryKeyRow(label, value) {
  return { label, value: value == null || value === "" ? "-" : value };
}

function subsystemSummary(value) {
  if (!value) {
    return "-";
  }
  return [
    value.ready === undefined ? null : (value.ready ? "ready" : "not ready"),
    value.fresh === undefined ? null : (value.fresh ? "fresh" : "stale"),
    value.detail || value.status || value.last_error,
  ].filter(Boolean).join(" | ") || "-";
}

function inspectButton(kind, id, label) {
  return id ? `<button class="table-row-button mono" data-inspect-kind="${escapeHtml(kind)}" data-inspect-id="${escapeHtml(id)}">${escapeHtml(label)}</button>` : "-";
}

function readableState(value) {
  return value ? String(value).replaceAll("_", " ") : "-";
}

function readableMode(value) {
  return value ? String(value).replaceAll("_", " ") : "-";
}

function listOrDash(value) {
  if (!value || (Array.isArray(value) && value.length === 0)) {
    return "-";
  }
  return Array.isArray(value) ? value.join(", ") : String(value);
}

function booleanText(value) {
  if (value === true) {
    return "yes";
  }
  if (value === false) {
    return "no";
  }
  return "-";
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const number = Number(value);
  if (Number.isNaN(number)) {
    return String(value);
  }
  if (Math.abs(number) >= 1000) {
    return number.toFixed(2);
  }
  if (Math.abs(number) >= 1) {
    return number.toFixed(4);
  }
  return number.toFixed(6);
}

function formatSigned(value) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const number = Number(value);
  if (Number.isNaN(number)) {
    return String(value);
  }
  return `${number > 0 ? "+" : ""}${formatNumber(number)}`;
}

function formatDuration(seconds) {
  const value = Number(seconds);
  if (Number.isNaN(value)) {
    return "-";
  }
  if (value < 60) {
    return `${Math.round(value)}s`;
  }
  if (value < 3600) {
    return `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
  }
  return `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`;
}

function formatMaybeTimestamp(value) {
  return value ? formatDateTime(value) : "-";
}

function formatDateTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("en-CA", { hour12: false });
}

function setStamp(node, timestamp) {
  node.textContent = timestamp ? `updated ${formatMaybeTimestamp(timestamp)}` : "waiting";
}

function chipClass(tone) {
  if (tone === "success") {
    return "chip-success";
  }
  if (tone === "warning") {
    return "chip-warning";
  }
  if (tone === "danger") {
    return "chip-danger";
  }
  if (tone === "neutral") {
    return "chip-neutral";
  }
  return "chip-outline";
}

function runtimeStateClass(runtimeState) {
  if (runtimeState === "healthy") {
    return "chip-success";
  }
  if (runtimeState === "degraded") {
    return "chip-warning";
  }
  if (runtimeState === "blocked" || runtimeState === "halted") {
    return "chip-danger";
  }
  return "chip-neutral";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

window.refreshDashboard = refreshDashboard;
