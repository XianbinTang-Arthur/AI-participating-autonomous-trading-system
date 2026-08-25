import { buildFillDrawer, buildOrderDrawer } from "../detail-drawers.js";
import { buildLifecycleAttributionDrawer } from "../lifecycle-drawer.js";
import { setFlash } from "../flash.js";

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
  async function inspectOrder(orderId, triggerElement = null) {
    if (!orderId) return;
    try {
      const detail = await requestJson(`/orders/${encodeURIComponent(orderId)}`);
      openDrawer(buildOrderDrawer(detail), triggerElement);
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    }
  }

  async function inspectFill(fillId, triggerElement = null) {
    if (!fillId) return;
    try {
      const detail = await requestJson(`/fills/${encodeURIComponent(fillId)}`);
      openDrawer(buildFillDrawer(detail), triggerElement);
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    }
  }

  async function inspectLifecycleAttribution(lifecycleId, triggerElement = null) {
    if (!lifecycleId) return;
    try {
      const detail = await requestJson(`/reports/position-lifecycle-attribution/${encodeURIComponent(lifecycleId)}`);
      openDrawer(buildLifecycleAttributionDrawer(detail), triggerElement);
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    }
  }

  async function resolveStuckOrder(orderId) {
    if (!orderId) return;
    const result = await runDangerousAction({
      path: `/orders/${encodeURIComponent(orderId)}/resolve-stuck-submission`,
      body: { reason: "ui_resolve_stuck_submission" },
      successMessage: "已提交卡单处理请求。",
      confirmMessage: "确认对这笔长时间卡住的委托执行人工恢复处理吗？",
    });
    const errorMessage = String(result?.error?.message || result?.error || "");
    if (!result?.ok && errorMessage.includes("claimed_submit_requires_operator_confirmation")) {
      await runDangerousAction({
        path: `/orders/${encodeURIComponent(orderId)}/resolve-stuck-submission`,
        body: {
          reason: "ui_resolve_claimed_submit_after_exchange_absent",
          operator_confirmation: `resolve_claimed_submit_as_failed:${orderId}`,
        },
        successMessage: "已提交 CLAIMED 提交卡单处理请求。",
        confirmMessage: `检测到 ${orderId} 存在已领取的提交命令。仅在已人工确认 OKX 无此订单时继续。`,
      });
    }
  }

  return {
    "inspect-order": (value, target) => inspectOrder(value, target),
    "inspect-fill": (value, target) => inspectFill(value, target),
    "inspect-lifecycle-attribution": (value, target) => inspectLifecycleAttribution(value, target),
    "resolve-stuck-order": (value) => resolveStuckOrder(value),
    "load-more-orders": () => adjustPageLimit("recentOrders", pageLoadStep),
    "collapse-orders": () => resetPageLimit("recentOrders"),
    "load-more-fills": () => adjustPageLimit("recentFills", pageLoadStep),
    "collapse-fills": () => resetPageLimit("recentFills"),
  };
}
