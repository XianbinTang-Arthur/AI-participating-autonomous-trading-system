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
import { AUTO_REFRESH_MS, CORE_SPECS, createState, viewSpecs } from "./modules/store.js";
import { localizeError, readableState, toneForOrderStatus, toneForRuntimeState } from "./modules/terms.js";
import { renderAdminView } from "./modules/views/admin-view.js";
import { renderExecutionView } from "./modules/views/execution-view.js";
import { renderOverviewView } from "./modules/views/overview-view.js";
import { renderRiskView } from "./modules/views/risk-view.js";
import { renderStrategyView } from "./modules/views/strategy-view.js";

const state = createState();

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
    const specs = dedupeSpecs([...CORE_SPECS, ...viewSpecs(state.activeView)]);
    const results = await fetchPanels(specs);
    for (const [key, result] of Object.entries(results)) {
      state.data[key] = result.data;
      state.errors[key] = result.error;
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
  nodes.sessionIdentityValue.textContent = session.identity || "未登录";
  nodes.sessionRoleValue.textContent = `角色：${readableState(session.role || "anonymous")}`;
  nodes.authStateChip.className = `status-pill tone-${session.authenticated ? "positive" : "neutral"}`;
  nodes.authStateChip.textContent = session.authenticated ? "已认证" : "未认证";
}

function renderStatusRibbon() {
  const health = state.data.health || {};
  const mode = state.data.mode || {};
  const runtime = state.data.runtime || {};
  const recovery = state.data.systemRecovery?.recovery || {};
  const account = state.data.accountState || {};
  const reconciliation = state.data.reconciliationLatest?.reconciliation || null;
  const portfolio = state.data.portfolio?.portfolio || {};

  nodes.statusRibbon.innerHTML = [
    statusCard({
      title: "系统状态",
      value: readableState(health.runtime_state || health.overall_status),
      meta: `总体状态 ${readableState(health.overall_status)}`,
      pills: [pill(`运行姿态 ${readableState(mode.operating_state || "unknown")}`, toneForRuntimeState(health.runtime_state || health.overall_status))],
    }),
    statusCard({
      title: "交易资格",
      value: recovery.safe_to_trade ? "允许继续交易" : "当前不允许交易",
      meta: recovery.safe_to_trade ? "没有发现明确阻断" : listOrDash(recovery.resume_blocked_reasons),
      pills: [
        pill(`已暂停 ${booleanWord(health.halted)}`, health.halted ? "danger" : "outline"),
        pill(`需人工确认 ${booleanWord(recovery.review_required)}`, recovery.review_required ? "warning" : "outline"),
      ],
    }),
    statusCard({
      title: "提交模式",
      value: readableState(runtime.environment_capabilities?.exchange_submission_target || mode.execution_route || "unknown"),
      meta: `执行通道 ${runtime.environment_capabilities?.execution_route || mode.execution_route || "-"}`,
      pills: [
        pill(`允许提交 ${booleanWord(mode.exchange_submit_allowed)}`, mode.exchange_submit_allowed ? "positive" : "outline"),
      ],
    }),
    statusCard({
      title: "账户状态",
      value: booleanWord(account.ready),
      meta: account.ready ? "账户快照可用" : listOrDash(account.blockers),
      pills: [
        pill(`已连接 ${booleanWord(account.connected)}`, account.connected ? "positive" : "warning"),
        pill(`数据新鲜 ${booleanWord(account.fresh)}`, account.fresh ? "positive" : "warning"),
      ],
    }),
    statusCard({
      title: "对账状态",
      value: readableState(reconciliation?.severity || "unknown"),
      meta: reconciliation?.reconciliation_id || "还没有对账记录",
      pills: [
        pill(`要求暂停 ${booleanWord(reconciliation?.halt_required)}`, reconciliation?.halt_required ? "danger" : "outline"),
      ],
    }),
    statusCard({
      title: "当前权益",
      value: formatNumber(portfolio.total_equity),
      meta: `已实现 ${formatSigned(portfolio.realized_pnl)} / 未实现 ${formatSigned(portfolio.unrealized_pnl)}`,
      pills: [
        pill(`总敞口 ${formatNumber(portfolio.gross_exposure)}`, "info"),
      ],
    }),
  ].join("");
}

function renderBanners() {
  const banners = [];
  const recovery = state.data.systemRecovery?.recovery || {};
  const blockers = state.data.blockers?.blockers || [];

  if (!recovery.safe_to_trade) {
    banners.push(notice(`当前不允许继续交易：${listOrDash(recovery.resume_blocked_reasons)}`, "warning"));
  }
  if (blockers.length > 0) {
    banners.push(notice(`当前 blocker：${localizeError(blockers[0].blocker)}`, blockers[0].affects_execution ? "danger" : "warning"));
  }
  if (state.flash) {
    banners.push(notice(state.flash.message, state.flash.tone));
    state.flash = null;
  }
  nodes.bannerContainer.innerHTML = banners.join("");
}

function renderActiveView() {
  const viewData = { ...state.data, errors: state.errors };
  nodes.overviewContent.innerHTML = state.activeView === "overview" ? renderOverviewView(viewData) : "";
  nodes.strategyContent.innerHTML = state.activeView === "strategy" ? renderStrategyView(viewData) : "";
  nodes.executionContent.innerHTML = state.activeView === "execution" ? renderExecutionView(viewData) : "";
  nodes.riskContent.innerHTML = state.activeView === "risk" ? renderRiskView(viewData) : "";
  nodes.adminContent.innerHTML = state.activeView === "admin" ? renderAdminView(viewData) : "";
}

function updateActionAccess() {
  const canWrite = operatorCanWrite();
  const buttons = [nodes.reconcileButton, nodes.rebaselineButton, nodes.resumeButton, nodes.haltButton, nodes.logoutButton];
  buttons.forEach((node) => {
    if (!node) return;
    node.disabled = !canWrite && node !== nodes.logoutButton;
  });
  nodes.actionPermissionHint.textContent = canWrite
    ? "当前会话可以执行人工控制操作。"
    : "当前会话没有写入权限，只允许查看数据。";
}

function updateRefreshLabel() {
  if (state.refreshing) {
    nodes.lastRefreshLabel.textContent = "正在刷新页面数据…";
    return;
  }
  if (!state.lastRefreshAt) {
    nodes.lastRefreshLabel.textContent = "尚未刷新";
    return;
  }
  nodes.lastRefreshLabel.textContent = `最近刷新：${formatMaybeTimestamp(state.lastRefreshAt)}（${formatRelativeAge(state.lastRefreshAt)}）`;
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
          title: "关键信息",
          content: kvList([
            ["标的", detail.decision_context?.symbol || "-", detail.decision_context?.timeframe || "-"],
            ["仓位意图", readableState(detail.position_target?.position_intent || "hold"), readableState(detail.position_target?.target_exposure_side || "-")],
            ["目标仓位变化", formatSigned(detail.position_target?.delta_position_qty), `目标仓位 ${formatSigned(detail.position_target?.target_position_qty)}`],
            ["策略放行", booleanWord(detail.policy_decision?.execution_allowed), listOrDash(detail.policy_decision?.blocker_reasons)],
            ["风控通过", booleanWord(detail.risk_decision?.approved), listOrDash(detail.risk_decision?.rejection_reasons)],
          ]),
        }),
        surfaceCard({
          title: "决策解释",
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
      eyebrow: "订单详情",
      title: order.client_order_id || "订单详情",
      summary: `订单当前状态：${readableState(order.status)}。${fills.length ? ` 已关联 ${fills.length} 笔成交。` : " 当前还没有关联成交。"} `,
      body: [
        surfaceCard({
          title: "订单摘要",
          content: kvList([
            ["标的", order.symbol || "-", order.order_type || "-"],
            ["订单状态", readableState(order.status), order.exchange_order_id || "等待同步交易所订单号"],
            ["请求数量", formatSigned(order.requested_qty), `剩余 ${formatNumber(order.remaining_qty)}`],
            ["已成交数量", formatNumber(order.filled_qty), `平均成交价 ${formatNumber(order.average_fill_price)}`],
            ["更新时间", formatMaybeTimestamp(order.last_update_ts || order.created_at), formatRelativeAge(order.last_update_ts || order.created_at)],
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
            : emptyState("这笔订单目前还没有关联成交。"),
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
      eyebrow: "成交详情",
      title: fill.fill_id || "成交详情",
      summary: `这笔成交记录了 ${readableState(fill.side)} ${formatNumber(fill.fill_qty)}，成交价 ${formatNumber(fill.fill_price)}。`,
      body: [
        surfaceCard({
          title: "成交摘要",
          content: kvList([
            ["标的", fill.symbol || "-", readableState(fill.position_intent || "-")],
            ["成交方向", readableState(fill.side), readableState(fill.exposure_side || "-")],
            ["成交数量", formatNumber(fill.fill_qty), `成交价格 ${formatNumber(fill.fill_price)}`],
            ["收益影响", formatSigned(fill.realized_pnl), `手续费 ${formatNumber(fill.fee_amount)} ${fill.fee_currency || ""}`],
            ["写入时间", formatMaybeTimestamp(fill.ingestion_timestamp), formatRelativeAge(fill.ingestion_timestamp)],
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
      summary: `最新对账级别是 ${readableState(reconciliation.severity)}。${reconciliation.halt_required ? " 这份结果要求暂停交易。" : ""}`,
      body: [
        surfaceCard({
          title: "对账摘要",
          content: kvList([
            ["对账级别", readableState(reconciliation.severity), reconciliation.exchange_comparison_enabled ? "已对比交易所" : "仅本地校验"],
            ["是否要求暂停", booleanWord(reconciliation.halt_required), booleanWord(reconciliation.review_required)],
            ["差异原因", listOrDash(detail.mismatch_summary?.mismatch_reasons), listOrDash(detail.mismatch_summary?.mismatch_categories)],
            ["建议动作", localizeError(detail.mismatch_summary?.recommended_operator_action || "-"), listOrDash(detail.mismatch_summary?.safety_impacts)],
            ["对账时间", formatMaybeTimestamp(reconciliation.as_of_ts), formatRelativeAge(reconciliation.as_of_ts)],
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
    successMessage: "已发起卡单处理请求。",
    confirmMessage: "确认对这笔卡住的订单执行人工恢复处理吗？",
  });
}

async function createOperatorUser() {
  const username = document.getElementById("operatorCreateUsername")?.value.trim();
  const password = document.getElementById("operatorCreatePassword")?.value;
  const role = document.getElementById("operatorCreateRole")?.value;
  const enabled = document.getElementById("operatorCreateEnabled")?.value === "true";
  if (!username || !password || !role) {
    state.flash = { tone: "warning", message: "请先完整填写新账户的用户名、密码和角色。" };
    renderBanners();
    return;
  }
  try {
    await requestJson("/auth/users", { method: "POST", body: { username, password, role, enabled } });
    state.flash = { tone: "info", message: `已创建账户 ${username}。` };
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

function strategySummary(detail) {
  const target = detail.position_target || {};
  const policy = detail.policy_decision || {};
  const risk = detail.risk_decision || {};
  if (!detail.decision_id) return "最近没有新的策略详情。";
  return `系统当前对 ${detail.decision_context?.symbol || "当前标的"} 的判断是 ${readableState(target.position_intent || "hold")}。` +
    `${policy.execution_allowed ? "策略层已放行，" : "策略层未放行，"}` +
    `${risk.approved ? "风控层已通过。" : `风控层仍在阻断：${listOrDash(risk.rejection_reasons)}。`}`;
}

window.refreshDashboard = refreshDashboard;
