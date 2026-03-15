const AUTO_REFRESH_MS = 5000;
const API_KEY_STORAGE_KEY = "aats.operator.apiKey";

const state = {
  activeView: "overview",
  apiKey: window.localStorage.getItem(API_KEY_STORAGE_KEY) || "",
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
  apiKeyInput: "apiKeyInput",
  saveApiKeyButton: "saveApiKeyButton",
  clearApiKeyButton: "clearApiKeyButton",
  refreshButton: "refreshButton",
  haltButton: "haltButton",
  resumeButton: "resumeButton",
  reconcileButton: "reconcileButton",
  autoRefreshToggle: "autoRefreshToggle",
  lastRefreshLabel: "lastRefreshLabel",
  bannerContainer: "bannerContainer",
  systemCardValue: "systemCardValue",
  systemCardMeta: "systemCardMeta",
  portfolioCardValue: "portfolioCardValue",
  portfolioCardMeta: "portfolioCardMeta",
  decisionCardValue: "decisionCardValue",
  decisionCardMeta: "decisionCardMeta",
  executionCardValue: "executionCardValue",
  executionCardMeta: "executionCardMeta",
  overviewStatus: "overviewStatus",
  overviewMode: "overviewMode",
  overviewPortfolio: "overviewPortfolio",
  overviewBlockers: "overviewBlockers",
  overviewBlockerStamp: "overviewBlockerStamp",
  decisionSpotlight: "decisionSpotlight",
  decisionFeed: "decisionFeed",
  decisionLookupInput: "decisionLookupInput",
  loadDecisionButton: "loadDecisionButton",
  executionSpotlight: "executionSpotlight",
  orderFeed: "orderFeed",
  fillFeed: "fillFeed",
  orderLookupInput: "orderLookupInput",
  loadOrderButton: "loadOrderButton",
  fillLookupInput: "fillLookupInput",
  loadFillButton: "loadFillButton",
  diagnosticReconciliation: "diagnosticReconciliation",
  diagnosticMetrics: "diagnosticMetrics",
  diagnosticErrors: "diagnosticErrors",
  diagnosticReplay: "diagnosticReplay",
  inspectSystemButton: "inspectSystemButton",
  inspectRuntimeButton: "inspectRuntimeButton",
  inspectPortfolioButton: "inspectPortfolioButton",
  inspectLatestDecisionButton: "inspectLatestDecisionButton",
  inspectLatestOrderButton: "inspectLatestOrderButton",
  inspectLatestFillButton: "inspectLatestFillButton",
  inspectReconciliationButton: "inspectReconciliationButton",
  detailDrawer: "detailDrawer",
  drawerBackdrop: "drawerBackdrop",
  closeDrawerButton: "closeDrawerButton",
  drawerTitle: "drawerTitle",
  drawerBody: "drawerBody",
});

const viewTabs = Array.from(document.querySelectorAll(".view-tab"));
const views = Array.from(document.querySelectorAll(".view"));

init();

function init() {
  nodes.apiKeyInput.value = state.apiKey;
  bindEvents();
  renderEmptyState();
  updateAuthStateChip();
  void refreshDashboard();
}

function bindEvents() {
  nodes.saveApiKeyButton.addEventListener("click", saveApiKey);
  nodes.clearApiKeyButton.addEventListener("click", clearApiKey);
  nodes.refreshButton.addEventListener("click", () => void refreshDashboard({ manual: true }));
  nodes.haltButton.addEventListener("click", () => void runAction("/system/halt", { reason: "ui_manual_halt" }, "System halted."));
  nodes.resumeButton.addEventListener("click", () => void runAction("/system/resume", { reason: "ui_manual_resume" }, "Resume requested. Readiness was re-evaluated."));
  nodes.reconcileButton.addEventListener("click", () => void runAction("/reconciliation/validate", { reason: "ui_manual_validate" }, "Reconciliation validation requested."));
  nodes.autoRefreshToggle.addEventListener("change", () => nodes.autoRefreshToggle.checked ? scheduleRefresh() : cancelScheduledRefresh());
  nodes.inspectSystemButton.addEventListener("click", inspectSystemDetail);
  nodes.inspectRuntimeButton.addEventListener("click", inspectRuntimeDetail);
  nodes.inspectPortfolioButton.addEventListener("click", inspectPortfolioDetail);
  nodes.inspectLatestDecisionButton.addEventListener("click", () => void inspectLatestDecision());
  nodes.inspectLatestOrderButton.addEventListener("click", () => void inspectLatestOrder());
  nodes.inspectLatestFillButton.addEventListener("click", () => void inspectLatestFill());
  nodes.inspectReconciliationButton.addEventListener("click", () => void inspectLatestReconciliation());
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
    ["health", "/system/health"],
    ["mode", "/system/mode"],
    ["runtime", "/system/runtime"],
    ["blockers", "/system/blockers"],
    ["metrics", "/system/metrics"],
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
  state.flashMessage = { message, tone };
}

function setActionButtonsBusy(busy) {
  [nodes.refreshButton, nodes.haltButton, nodes.resumeButton, nodes.reconcileButton].forEach((node) => {
    node.disabled = busy;
  });
  nodes.refreshButton.textContent = busy ? "Refreshing..." : "Refresh";
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
  renderBanners();
  renderTopChips();
  renderStatusRibbon();
  renderOverview();
  renderDecisions();
  renderExecution();
  renderDiagnostics();
  nodes.lastRefreshLabel.textContent = state.lastRefreshAt
    ? `Last refresh ${formatDateTime(state.lastRefreshAt)}${manual ? " | manual" : ""}`
    : "Not refreshed yet";
}

function renderEmptyState() {
  nodes.systemCardValue.textContent = "-";
  nodes.portfolioCardValue.textContent = "-";
  nodes.decisionCardValue.textContent = "-";
  nodes.executionCardValue.textContent = "-";
  nodes.overviewStatus.innerHTML = emptyCard("Waiting for runtime data.");
  nodes.overviewMode.innerHTML = emptyCard("Waiting for mode data.");
  nodes.overviewPortfolio.innerHTML = emptyCard("Waiting for portfolio data.");
  nodes.overviewBlockers.innerHTML = emptyCard("No blocker information yet.");
  nodes.decisionSpotlight.innerHTML = emptyCard("Waiting for the first decision.");
  nodes.decisionFeed.innerHTML = emptyCard("No recent decisions yet.");
  nodes.executionSpotlight.innerHTML = emptyCard("Waiting for execution posture.");
  nodes.orderFeed.innerHTML = emptyCard("No recent orders yet.");
  nodes.fillFeed.innerHTML = emptyCard("No recent fills yet.");
  nodes.diagnosticReconciliation.innerHTML = emptyCard("No reconciliation report yet.");
  nodes.diagnosticMetrics.innerHTML = emptyCard("No runtime metrics yet.");
  nodes.diagnosticErrors.innerHTML = emptyCard("No execution errors.");
  nodes.diagnosticReplay.innerHTML = emptyCard("No replay validation yet.");
  setDrawerContent("Detail", `<div class="empty">Use a tab action or a direct lookup to inspect detail.</div>`);
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
  nodes.authStateChip.className = `pill ${pillClass(tone)}`;
}

function renderTopChips() {
  const health = state.data.health || {};
  const mode = state.data.mode || {};
  nodes.runtimeStateChip.textContent = readableState(health.runtime_state || health.overall_status);
  nodes.runtimeStateChip.className = `pill ${runtimeStateClass(health.runtime_state)}`;
  nodes.operatingStateChip.textContent = readableMode(mode.operating_state || "unknown");
  nodes.operatingStateChip.className = `pill ${pillClass(mode.execution_blocked ? "warning" : "outline")}`;
  nodes.executionRouteChip.textContent = mode.execution_route || mode.execution_backend || "unknown";
  nodes.executionRouteChip.className = `pill ${pillClass(mode.exchange_submit_allowed ? "success" : "outline")}`;
  nodes.submitPostureChip.textContent = mode.exchange_submit_allowed ? "submit enabled" : "submit blocked";
  nodes.submitPostureChip.className = `pill ${pillClass(mode.exchange_submit_allowed ? "success" : "danger")}`;
}

function renderBanners() {
  const health = state.data.health || {};
  const blockers = state.data.blockers?.blockers || [];
  const banners = [];
  if (state.flashMessage) {
    banners.push(state.flashMessage);
    state.flashMessage = null;
  }
  if (health.runtime_state === "halted") {
    banners.push({ tone: "danger", message: "The runtime is halted. Execution is stopped until resume succeeds." });
  } else if (health.runtime_state === "blocked") {
    const lead = blockers[0];
    banners.push({
      tone: "warning",
      message: lead ? `Execution is blocked by ${lead.blocker}. ${lead.recommended_action}` : "Execution is blocked by an active safety condition.",
    });
  } else if (health.runtime_state === "degraded") {
    banners.push({ tone: "info", message: "The runtime is degraded. Review freshness and reconciliation before trusting the next action." });
  }
  Object.entries(state.panelErrors).slice(0, 4).forEach(([panel, error]) => {
    banners.push({
      tone: error.status === 401 || error.status === 403 ? "danger" : "warning",
      message: `${panel} failed: ${error.message}`,
    });
  });
  nodes.bannerContainer.innerHTML = banners.length
    ? banners.map((item) => `<div class="banner banner-${item.tone}">${escapeHtml(item.message)}</div>`).join("")
    : "";
}

function renderStatusRibbon() {
  const health = state.data.health || {};
  const portfolio = state.data.portfolio?.portfolio || null;
  const latestDecision = state.data.latestDecision || {};
  const execution = state.data.executionLatest || {};
  const latestFill = execution.latest_fill || null;

  nodes.systemCardValue.textContent = readableState(health.runtime_state || health.overall_status);
  nodes.systemCardMeta.textContent = health.submit_blocked ? listOrDash(health.submit_blocked_reasons) : "No submit blockers";
  nodes.portfolioCardValue.textContent = portfolio ? formatNumber(portfolio.total_equity) : "-";
  nodes.portfolioCardMeta.textContent = portfolio
    ? `gross ${formatNumber(portfolio.gross_exposure)} | net ${formatSigned(portfolio.net_exposure)}`
    : "No portfolio snapshot";
  nodes.decisionCardValue.textContent = latestDecision.decision_id || "-";
  nodes.decisionCardMeta.textContent = latestDecision.position_target
    ? `delta ${formatSigned(latestDecision.position_target.delta_position_qty)}`
    : "No decision detail";
  nodes.executionCardValue.textContent = execution.mode?.exchange_submit_allowed ? "armed" : "guarded";
  nodes.executionCardMeta.textContent = latestFill
    ? `${formatNumber(latestFill.fill_qty)} @ ${formatNumber(latestFill.fill_price)}`
    : "No recent fills";
}

function renderOverview() {
  const health = state.data.health || {};
  const mode = state.data.mode || {};
  const portfolio = state.data.portfolio?.portfolio || null;
  const blockers = state.data.blockers?.blockers || [];
  const account = state.data.accountState || {};

  nodes.overviewStatus.innerHTML = renderFactStack([
    factRow("Overall", readableState(health.overall_status)),
    factRow("Runtime", readableState(health.runtime_state)),
    factRow("Halted", booleanWord(health.halted)),
    factRow("Market Fresh", booleanWord(health.freshness?.market_fresh)),
    factRow("Account Fresh", booleanWord(health.freshness?.account_fresh)),
    factRow("Reconciliation Fresh", booleanWord(health.freshness?.reconciliation_fresh)),
    factRow("Account Ready", booleanWord(account.ready)),
    factRow("Warnings", String((health.warnings || []).length)),
  ]);

  nodes.overviewMode.innerHTML = renderFactStack([
    factRow("Profile", mode.config_profile || "-"),
    factRow("Mode", readableMode(mode.mode)),
    factRow("Operating State", readableMode(mode.operating_state)),
    factRow("Market Backend", mode.market_data_backend || "-"),
    factRow("Account Backend", mode.account_backend || "-"),
    factRow("Execution Backend", mode.execution_backend || "-"),
    factRow("AI Mode", mode.ai_operating_mode || "-"),
    factRow("Submit Allowed", booleanWord(mode.exchange_submit_allowed)),
  ]);

  nodes.overviewPortfolio.innerHTML = renderFactStack([
    factRow("Total Equity", portfolio ? formatNumber(portfolio.total_equity) : "-"),
    factRow("Realized PnL", portfolio ? formatSigned(portfolio.realized_pnl) : "-"),
    factRow("Unrealized PnL", portfolio ? formatSigned(portfolio.unrealized_pnl) : "-"),
    factRow("Gross Exposure", portfolio ? formatNumber(portfolio.gross_exposure) : "-"),
    factRow("Net Exposure", portfolio ? formatSigned(portfolio.net_exposure) : "-"),
    factRow("Positions", portfolio ? String((portfolio.positions || []).length) : "-"),
    factRow("USDT Balance", portfolio ? formatNumber(portfolio.balances?.USDT) : "-"),
    factRow("Updated", state.data.portfolio?.latest_update_timestamp ? formatDateTime(state.data.portfolio.latest_update_timestamp) : "-"),
  ]);

  nodes.overviewBlockerStamp.textContent = blockers.length ? `${blockers.length} blocker${blockers.length === 1 ? "" : "s"}` : "no blockers";
  nodes.overviewBlockers.innerHTML = renderSignalList(
    blockers.slice(0, 4).map((item) => ({
      title: item.blocker,
      meta: `${item.subsystem} | ${item.affects_execution ? "affects execution" : "submit-only"}`,
      tone: item.affects_execution ? "danger" : "warning",
      detail: item.recommended_action,
    })),
    "No active blockers."
  );
}

function renderDecisions() {
  const latestDecision = state.data.latestDecision || {};
  const summary = latestDecision.summary || {
    decision_id: latestDecision.decision_id,
    symbol: latestDecision.decision_context?.symbol,
    timeframe: latestDecision.decision_context?.timeframe,
    decision_time: latestDecision.decision_context?.as_of_ts,
    target_delta_qty: latestDecision.position_target?.delta_position_qty,
    policy_result: latestDecision.policy_decision?.execution_allowed,
    risk_result: latestDecision.risk_decision?.approved,
    execution_result: {
      order_count: (latestDecision.order_intents || []).length,
      fill_count: (latestDecision.fills || []).length,
      reconciled: Boolean(latestDecision.reconciliations?.length),
    },
  };
  const baseline = latestDecision.baseline_assessment || null;
  const risk = latestDecision.risk_decision || null;
  const policy = latestDecision.policy_decision || null;
  const target = latestDecision.position_target || null;

  nodes.decisionSpotlight.innerHTML = latestDecision.decision_id ? `
    <article class="spotlight-card">
      <div class="spotlight-hero">
        <div>
          <p class="spotlight-id">${escapeHtml(latestDecision.decision_id)}</p>
          <h3>${escapeHtml(summary.symbol || "-")} · ${escapeHtml(summary.timeframe || "-")}</h3>
        </div>
        <div class="meta-row">
          ${miniPill(readableMode(baseline?.regime || "unknown"), baseline?.regime === "trend" || baseline?.regime === "breakout" ? "success" : "neutral")}
          ${miniPill(`policy ${booleanShort(policy?.execution_allowed)}`, policy?.execution_allowed ? "success" : "danger")}
          ${miniPill(`risk ${booleanShort(risk?.approved)}`, risk?.approved ? "success" : "danger")}
        </div>
      </div>
      <div class="spotlight-main">
        ${factGrid([
          factRow("Target Delta", formatSigned(target?.delta_position_qty)),
          factRow("Target Qty", formatSigned(target?.target_position_qty)),
          factRow("Bias", baseline?.direction_bias || "-"),
          factRow("Alpha", formatSigned(baseline?.composite_alpha_score)),
          factRow("Position Scale", formatNumber(baseline?.suggested_position_scale)),
          factRow("Vol Target", formatNumber(baseline?.volatility_target_scale)),
        ])}
        <div class="signal-item">
          <div class="signal-head">
            <span class="signal-title">Decision Outcome</span>
            <button class="button button-ghost" data-inspect-decision="${escapeHtml(latestDecision.decision_id)}">Open audit chain</button>
          </div>
          <div class="signal-meta">${escapeHtml(decisionOutcomeLabel(summary))}</div>
          <div class="fact-note">${escapeHtml(listOrDash(risk?.rejection_reasons || baseline?.reason_codes || []))}</div>
        </div>
      </div>
    </article>
  ` : emptyCard("No latest decision is available yet.");

  const recentDecisions = state.data.recentDecisions?.decisions || [];
  nodes.decisionFeed.innerHTML = renderFeedList(
    recentDecisions,
    "No recent decisions yet.",
    (item) => ({
      title: `${item.symbol || "-"} · ${item.timeframe || "-"}`,
      subtitle: item.decision_id || "-",
      meta: [
        miniPill(`delta ${formatSigned(item.target_delta_qty)}`, "outline"),
        miniPill(`policy ${booleanShort(item.policy_result)}`, item.policy_result ? "success" : "danger"),
        miniPill(`risk ${booleanShort(item.risk_result)}`, item.risk_result ? "success" : "danger"),
        miniPill(`fills ${item.execution_result?.fill_count ?? 0}`, "neutral"),
      ],
      detail: item.decision_time ? `decision ${formatDateTime(item.decision_time)}` : "decision time unknown",
      action: item.decision_id ? `<button class="button button-ghost" data-inspect-decision="${escapeHtml(item.decision_id)}">Inspect</button>` : "",
    })
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

  nodes.executionSpotlight.innerHTML = `
    <article class="spotlight-card">
      <div class="spotlight-hero">
        <div>
          <p class="spotlight-id">${escapeHtml(mode.execution_route || mode.execution_backend || "unknown")}</p>
          <h3>${escapeHtml(readableMode(mode.operating_state || "unknown"))}</h3>
        </div>
        <div class="meta-row">
          ${miniPill(mode.exchange_submit_allowed ? "submit enabled" : "submit blocked", mode.exchange_submit_allowed ? "success" : "danger")}
          ${miniPill(booleanWord(readiness.ready), readiness.ready ? "success" : "warning")}
        </div>
      </div>
      ${factGrid([
        factRow("Submit Blockers", listOrDash(mode.submit_blocked_reasons)),
        factRow("Latest Order", latestOrder ? `${latestOrder.status} · ${latestOrder.client_order_id}` : "-"),
        factRow("Latest Fill", latestFill ? `${formatNumber(latestFill.fill_qty)} @ ${formatNumber(latestFill.fill_price)}` : "-"),
        factRow("Reconciliation", latestReconciliation ? latestReconciliation.severity : "-"),
        factRow("Open Orders", formatNumber(state.data.metrics?.current_open_order_count)),
        factRow("Recovery", execution.recovery?.safe_startup ? "safe" : "review required"),
      ])}
    </article>
  `;

  nodes.orderFeed.innerHTML = renderFeedList(
    recentOrders,
    "No recent orders yet.",
    (order) => ({
      title: `${order.symbol || "-"} · ${order.status || "-"}`,
      subtitle: order.client_order_id || "-",
      meta: [
        miniPill(order.side || "-", "outline"),
        miniPill(order.venue || order.submission_mode || "-", "neutral"),
        miniPill(`qty ${formatNumber(order.requested_qty)}`, "outline"),
      ],
      detail: order.last_update_ts ? `updated ${formatDateTime(order.last_update_ts)}` : "no update timestamp",
      action: order.client_order_id ? `<button class="button button-ghost" data-inspect-order="${escapeHtml(order.client_order_id)}">Inspect</button>` : "",
    })
  );

  nodes.fillFeed.innerHTML = renderFeedList(
    recentFills,
    "No recent fills yet.",
    (fill) => ({
      title: `${fill.symbol || "-"} · ${fill.side || "-"}`,
      subtitle: fill.fill_id || "-",
      meta: [
        miniPill(`qty ${formatNumber(fill.fill_qty)}`, "outline"),
        miniPill(`px ${formatNumber(fill.fill_price)}`, "outline"),
        miniPill(fill.venue || "-", "neutral"),
      ],
      detail: fill.ingestion_timestamp ? `ingested ${formatDateTime(fill.ingestion_timestamp)}` : "no ingestion timestamp",
      action: fill.fill_id ? `<button class="button button-ghost" data-inspect-fill="${escapeHtml(fill.fill_id)}">Inspect</button>` : "",
    })
  );
}

function renderDiagnostics() {
  const reconciliation = state.data.reconciliationLatest?.reconciliation || null;
  const mismatchSummary = state.data.reconciliationLatest?.mismatch_summary || null;
  const metrics = state.data.metrics || {};
  const errors = state.data.executionErrors?.errors || [];
  const replay = state.data.replayStatus || {};

  nodes.diagnosticReconciliation.innerHTML = reconciliation ? `
    <article class="spotlight-card">
      <div class="spotlight-hero">
        <div>
          <p class="spotlight-id">${escapeHtml(reconciliation.reconciliation_id)}</p>
          <h3>${escapeHtml(reconciliation.severity)}</h3>
        </div>
        <div class="meta-row">
          ${miniPill(reconciliation.halt_required ? "halt required" : "safe", reconciliation.halt_required ? "danger" : "success")}
          ${miniPill(reconciliation.exchange_comparison_enabled ? "exchange aware" : "local only", "outline")}
        </div>
      </div>
      ${factGrid([
        factRow("Mismatch Reasons", listOrDash(mismatchSummary?.mismatch_reasons)),
        factRow("Safety Impacts", listOrDash(mismatchSummary?.safety_impacts)),
        factRow("Validated", state.data.reconciliationLatest?.latest_validation?.validated_at ? formatDateTime(state.data.reconciliationLatest.latest_validation.validated_at) : "-"),
      ])}
    </article>
  ` : emptyCard("No reconciliation report yet.");

  nodes.diagnosticMetrics.innerHTML = renderFactStack([
    factRow("Decision Cycles", formatNumber(metrics.decision_cycle_count)),
    factRow("Order Intents", formatNumber(metrics.order_intent_count)),
    factRow("Fills", formatNumber(metrics.fill_count)),
    factRow("Rejections", formatNumber(metrics.rejection_count)),
    factRow("Reconciliation Mismatches", formatNumber(metrics.reconciliation_mismatch_count)),
    factRow("Open Orders", formatNumber(metrics.current_open_order_count)),
    factRow("Gross Exposure", formatNumber(metrics.exposure_summary?.gross_exposure)),
    factRow("Net Exposure", formatSigned(metrics.exposure_summary?.net_exposure)),
  ]);

  nodes.diagnosticErrors.innerHTML = renderSignalList(
    errors.slice(0, 5).map((item) => ({
      title: item.message || item.status || "execution error",
      meta: `${item.subsystem || "execution"} | ${formatMaybeTimestamp(item.timestamp)}`,
      tone: item.severity === "error" ? "danger" : "warning",
      detail: [item.decision_id, item.order_id].filter(Boolean).join(" | ") || "no linked IDs",
    })),
    "No recent execution errors."
  );

  nodes.diagnosticReplay.innerHTML = renderFactStack([
    factRow("Replay Supported", booleanWord(replay.supported)),
    factRow("Replay Healthy", booleanWord(replay.healthy)),
    factRow("Last Decision", replay.last_validation?.decision_id || "-"),
    factRow("Replayed Events", formatNumber(replay.last_validation?.replayed_event_count)),
    factRow("Divergences", formatNumber(replay.last_validation?.divergence_count)),
    factRow("Validated", replay.last_validation?.validated_at ? formatDateTime(replay.last_validation.validated_at) : "-"),
  ]);
}

async function inspectLatestDecision() {
  const decisionId = state.data.latestDecision?.decision_id;
  if (!decisionId) {
    flash("No latest decision is available yet.", "warning");
    renderBanners();
    return;
  }
  await inspectDecision(decisionId, { manual: true });
}

async function inspectLatestOrder() {
  const orderId = state.data.executionLatest?.latest_order?.client_order_id;
  if (!orderId) {
    flash("No latest order is available yet.", "warning");
    renderBanners();
    return;
  }
  await inspectOrder(orderId, { manual: true });
}

async function inspectLatestFill() {
  const fillId = state.data.executionLatest?.latest_fill?.fill_id;
  if (!fillId) {
    flash("No latest fill is available yet.", "warning");
    renderBanners();
    return;
  }
  await inspectFill(fillId, { manual: true });
}

async function inspectLatestReconciliation() {
  const reconciliationId = state.data.reconciliationLatest?.reconciliation?.reconciliation_id;
  if (!reconciliationId) {
    flash("No reconciliation report is available yet.", "warning");
    renderBanners();
    return;
  }
  try {
    const detail = await requestJson(`/reconciliation/${encodeURIComponent(reconciliationId)}`);
    showDetail("Reconciliation Detail", [
      detailSection("Summary", detail.mismatch_summary || {}),
      detailSection("Reconciliation", detail.reconciliation || {}),
    ]);
  } catch (error) {
    flash(`Reconciliation lookup failed: ${normalizeError(error, "Reconciliation lookup failed").message}`, "danger");
    renderBanners();
  }
}

function inspectSystemDetail() {
  showDetail("System Detail", [
    detailSection("Health", state.data.health || {}),
    detailSection("Mode", state.data.mode || {}),
    detailSection("Blockers", state.data.blockers || {}),
    detailSection("Account", state.data.accountState || {}),
  ]);
}

function inspectRuntimeDetail() {
  showDetail("Runtime Detail", [
    detailSection("Runtime", state.data.runtime || {}),
    detailSection("Metrics", state.data.metrics || {}),
    detailSection("Replay", state.data.replayStatus || {}),
  ]);
}

function inspectPortfolioDetail() {
  showDetail("Portfolio Snapshot", [
    detailSection("Portfolio", state.data.portfolio?.portfolio || {}),
  ]);
}

async function inspectDecision(decisionId, { manual = false } = {}) {
  if (!decisionId) {
    if (manual) {
      flash("Enter a valid decision ID.", "warning");
      renderBanners();
    }
    return;
  }
  try {
    const detail = await requestJson(`/decision/${encodeURIComponent(decisionId)}`);
    nodes.decisionLookupInput.value = decisionId;
    showDetail("Decision Detail", [
      detailSection("Decision Context", detail.decision_context || {}),
      detailSection("Assessments", {
        baseline_assessment: detail.baseline_assessment || null,
        ai_assessment: detail.ai_assessment || null,
      }),
      detailSection("Policy / Risk / Target", {
        position_target: detail.position_target || null,
        policy_decision: detail.policy_decision || null,
        risk_decision: detail.risk_decision || null,
      }),
      detailSection("Execution Chain", {
        execution_plan: detail.execution_plan || null,
        order_intents: detail.order_intents || [],
        order_updates: detail.order_updates || [],
        fills: detail.fills || [],
        portfolio_snapshot: detail.portfolio_snapshot || null,
        reconciliations: detail.reconciliations || [],
      }),
      detailSection("Audit", detail.audit || {}),
    ]);
  } catch (error) {
    flash(`Decision lookup failed: ${normalizeError(error, "Decision lookup failed").message}`, "danger");
    renderBanners();
  }
}

async function inspectOrder(orderId, { manual = false } = {}) {
  if (!orderId) {
    if (manual) {
      flash("Enter a valid order ID.", "warning");
      renderBanners();
    }
    return;
  }
  try {
    const detail = await requestJson(`/orders/${encodeURIComponent(orderId)}`);
    nodes.orderLookupInput.value = orderId;
    showDetail("Order Detail", [
      detailSection("Order", detail.order || {}),
      detailSection("Fills", detail.fills || []),
    ]);
  } catch (error) {
    flash(`Order lookup failed: ${normalizeError(error, "Order lookup failed").message}`, "danger");
    renderBanners();
  }
}

async function inspectFill(fillId, { manual = false } = {}) {
  if (!fillId) {
    if (manual) {
      flash("Enter a valid fill ID.", "warning");
      renderBanners();
    }
    return;
  }
  try {
    const detail = await requestJson(`/fills/${encodeURIComponent(fillId)}`);
    nodes.fillLookupInput.value = fillId;
    showDetail("Fill Detail", [
      detailSection("Fill", detail.fill || {}),
    ]);
  } catch (error) {
    flash(`Fill lookup failed: ${normalizeError(error, "Fill lookup failed").message}`, "danger");
    renderBanners();
  }
}

function showDetail(title, sections) {
  setDrawerContent(title, sections.map((section) => `
    <section class="detail-section">
      <h3>${escapeHtml(section.title)}</h3>
      <pre class="detail-json">${escapeHtml(JSON.stringify(section.value, null, 2))}</pre>
    </section>
  `).join(""));
  openDrawer();
}

function detailSection(title, value) {
  return { title, value };
}

function setDrawerContent(title, html) {
  nodes.drawerTitle.textContent = title;
  nodes.drawerBody.innerHTML = html;
}

function openDrawer() {
  nodes.detailDrawer.classList.add("is-open");
  nodes.detailDrawer.setAttribute("aria-hidden", "false");
}

function closeDrawer() {
  nodes.detailDrawer.classList.remove("is-open");
  nodes.detailDrawer.setAttribute("aria-hidden", "true");
}

function renderFactStack(rows) {
  return rows.length ? rows.map((row) => `
    <div class="fact-item">
      <span class="fact-key">${escapeHtml(row.label)}</span>
      <strong class="fact-value">${escapeHtml(row.value)}</strong>
    </div>
  `).join("") : emptyCard("No data.");
}

function renderSignalList(items, emptyText) {
  return items.length ? items.map((item) => `
    <article class="signal-item">
      <div class="signal-head">
        <span class="signal-title">${escapeHtml(item.title)}</span>
        ${miniPill(item.tone || "neutral", item.tone || "neutral")}
      </div>
      <div class="signal-meta">${escapeHtml(item.meta || "-")}</div>
      ${item.detail ? `<div class="fact-note">${escapeHtml(item.detail)}</div>` : ""}
    </article>
  `).join("") : emptyCard(emptyText);
}

function renderFeedList(items, emptyText, formatter) {
  return items.length ? items.map((item) => {
    const row = formatter(item);
    return `
      <article class="feed-item">
        <div class="feed-head">
          <div>
            <div class="feed-title">${escapeHtml(row.title)}</div>
            <div class="spotlight-id">${escapeHtml(row.subtitle || "-")}</div>
          </div>
          <div class="feed-actions">${row.action || ""}</div>
        </div>
        <div class="feed-metrics">${(row.meta || []).join("")}</div>
        <div class="signal-meta">${escapeHtml(row.detail || "-")}</div>
      </article>
    `;
  }).join("") : emptyCard(emptyText);
}

function factGrid(rows) {
  return `<div class="fact-stack">${renderFactStack(rows)}</div>`;
}

function factRow(label, value) {
  return { label, value: value == null || value === "" ? "-" : String(value) };
}

function decisionOutcomeLabel(summary) {
  if (!summary) {
    return "No decision outcome.";
  }
  const orderCount = summary.execution_result?.order_count ?? 0;
  const fillCount = summary.execution_result?.fill_count ?? 0;
  if (summary.risk_result === false) {
    return "Risk denied execution for this decision.";
  }
  if (summary.policy_result === false) {
    return "Policy blocked execution for this decision.";
  }
  if (orderCount === 0) {
    return "Decision completed without creating an execution intent.";
  }
  if (fillCount === 0) {
    return "Execution intent exists but no fills have been ingested yet.";
  }
  return "Decision flowed through execution and produced fills.";
}

function miniPill(label, tone = "neutral") {
  return `<span class="mini-pill ${miniPillClass(tone)}">${escapeHtml(label)}</span>`;
}

function pillClass(tone) {
  if (tone === "success") {
    return "pill-success";
  }
  if (tone === "warning") {
    return "pill-warning";
  }
  if (tone === "danger") {
    return "pill-danger";
  }
  if (tone === "neutral") {
    return "pill-neutral";
  }
  return "pill-outline";
}

function miniPillClass(tone) {
  if (tone === "success") {
    return "mini-pill-success";
  }
  if (tone === "warning") {
    return "mini-pill-warning";
  }
  if (tone === "danger") {
    return "mini-pill-danger";
  }
  if (tone === "neutral") {
    return "mini-pill-neutral";
  }
  return "mini-pill-outline";
}

function runtimeStateClass(runtimeState) {
  if (runtimeState === "healthy") {
    return "pill-success";
  }
  if (runtimeState === "degraded") {
    return "pill-warning";
  }
  if (runtimeState === "blocked" || runtimeState === "halted") {
    return "pill-danger";
  }
  return "pill-neutral";
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

function booleanWord(value) {
  if (value === true) {
    return "yes";
  }
  if (value === false) {
    return "no";
  }
  return "-";
}

function booleanShort(value) {
  if (value === true) {
    return "ok";
  }
  if (value === false) {
    return "blocked";
  }
  return "n/a";
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

function formatDateTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("en-CA", { hour12: false });
}

function formatMaybeTimestamp(value) {
  return value ? formatDateTime(value) : "-";
}

function emptyCard(message) {
  return `<div class="empty">${escapeHtml(message)}</div>`;
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
window.showDetail = showDetail;
