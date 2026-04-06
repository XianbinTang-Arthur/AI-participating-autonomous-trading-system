import { buildFillDrawer, buildOrderDrawer } from "../detail-drawers.js";

export function createExecutionActionHandlers({
  pageLoadStep = 12,
  requestJson,
  renderBanners,
  openDrawer,
  runDangerousAction,
  state,
  adjustPageLimit,
  resetPageLimit,
}) {
  async function inspectOrder(orderId) {
    if (!orderId) return;
    try {
      const detail = await requestJson(`/orders/${encodeURIComponent(orderId)}`);
      openDrawer(buildOrderDrawer(detail));
    } catch (error) {
      state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
      renderBanners();
    }
  }

  async function inspectFill(fillId) {
    if (!fillId) return;
    try {
      const detail = await requestJson(`/fills/${encodeURIComponent(fillId)}`);
      openDrawer(buildFillDrawer(detail));
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

  return {
    "inspect-order": (value) => inspectOrder(value),
    "inspect-fill": (value) => inspectFill(value),
    "resolve-stuck-order": (value) => resolveStuckOrder(value),
    "load-more-orders": () => adjustPageLimit("recentOrders", pageLoadStep),
    "collapse-orders": () => resetPageLimit("recentOrders"),
    "load-more-fills": () => adjustPageLimit("recentFills", pageLoadStep),
    "collapse-fills": () => resetPageLimit("recentFills"),
  };
}
