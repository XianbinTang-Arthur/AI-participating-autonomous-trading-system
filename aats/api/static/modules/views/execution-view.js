import { actionButton, callout, kvList, pill, statGrid, surfaceCard, table } from "../components.js";
import { formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned } from "../formatters.js";
import { localizeError, readableState, toneForOrderStatus } from "../terms.js";

export function renderExecutionView(data) {
  const executionLatest = data.executionLatest || {};
  const latestOrder = executionLatest.latest_order || null;
  const latestFill = executionLatest.latest_fill || null;
  const latestReconciliation = executionLatest.latest_reconciliation || null;
  const recentOrders = data.recentOrders?.orders || [];
  const recentFills = data.recentFills?.fills || [];
  const errors = data.executionErrors?.errors || [];
  const metrics = data.metrics || {};
  const latestDecision = data.latestDecision || {};

  return `
    <div class="panel-grid">
      <div class="span-12">
        ${surfaceCard({
          title: "执行主控区",
          kicker: "最新执行状态",
          copy: "这里回答三件事：最近订单是否顺利、最近成交是否落地、当前是否有执行错误在影响交易。",
          classes: "hero-card",
          actions: latestOrder?.client_order_id ? actionButton("查看最新订单", "inspect-order", latestOrder.client_order_id) : "",
          content: `
            ${callout({
              title: latestOrder ? `最新订单：${readableState(latestOrder.status)}` : "当前没有新的订单记录",
              copy: executionNarrative({ latestDecision, latestOrder, latestFill, latestReconciliation }),
              pills: [
                pill(`最新成交：${latestFill ? "已写入" : "暂无"}`, latestFill ? "positive" : "outline"),
                pill(`对账：${readableState(latestReconciliation?.severity || "unknown")}`, latestReconciliation?.halt_required ? "danger" : "info"),
                pill(`活动订单数：${formatNumber(metrics.current_open_order_count)}`, metrics.current_open_order_count > 0 ? "warning" : "outline"),
              ],
            })}
            ${statGrid([
              { label: "最新订单", value: readableState(latestOrder?.status || "unknown"), meta: latestOrder?.client_order_id || "暂无订单" },
              { label: "最新下单量", value: formatSigned(latestOrder?.requested_qty), meta: latestOrder ? `${latestOrder.order_type || "-"} | ${latestOrder.symbol || "-"}` : "-" },
              { label: "最新成交量", value: formatNumber(latestFill?.fill_qty), meta: latestFill ? `成交价 ${formatNumber(latestFill.fill_price)}` : "暂无成交" },
              { label: "最近错误数", value: formatNumber(errors.length), meta: errors[0] ? localizeError(errors[0].message || errors[0].status) : "近期没有执行错误" },
            ])}
          `,
        })}
      </div>

      <div class="span-7">
        ${surfaceCard({
          title: "最近订单",
          kicker: "订单生命周期",
          copy: "重点观察订单是否长时间停留在中间状态、是否重复失败、是否存在卡单。",
          content: table(
            ["订单", "含义", "状态", "更新时间", "操作"],
            recentOrders.map((order) => [
              `<div><strong>${order.symbol || "-"}</strong><div class="table-meta mono">${order.client_order_id || "-"}</div></div>`,
              `<div><strong>${readableState(order.position_intent || order.submission_payload?.positionIntent || "-")}</strong><div class="table-meta">${order.order_type || "-"} | 请求量 ${formatSigned(order.requested_qty)}</div></div>`,
              `<div><strong>${readableState(order.status)}</strong><div class="table-meta">${order.exchange_order_id || "本地订单号等待同步"}</div></div>`,
              `<div><strong>${formatRelativeAge(order.last_update_ts || order.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(order.last_update_ts || order.created_at)}</div></div>`,
              `<div class="stack-actions">${actionButton("详情", "inspect-order", order.client_order_id)}${stuckButton(order)}</div>`,
            ]),
            "最近还没有订单。"
          ),
        })}
      </div>

      <div class="span-5">
        ${surfaceCard({
          title: "最近成交",
          kicker: "成交确认",
          copy: "成交落地以后，收益、仓位和对账才能继续收敛。",
          content: table(
            ["成交", "成交内容", "费用影响", "写入时间", "查看"],
            recentFills.map((fill) => [
              `<div><strong>${fill.symbol || "-"}</strong><div class="table-meta mono">${fill.fill_id || "-"}</div></div>`,
              `<div><strong>${readableState(fill.side)}</strong><div class="table-meta">${formatNumber(fill.fill_qty)} @ ${formatNumber(fill.fill_price)}</div></div>`,
              `<div><strong>${formatSigned(fill.realized_pnl)}</strong><div class="table-meta">手续费 ${formatNumber(fill.fee_amount)} ${fill.fee_currency || ""}</div></div>`,
              `<div><strong>${formatRelativeAge(fill.ingestion_timestamp)}</strong><div class="table-meta">${formatMaybeTimestamp(fill.ingestion_timestamp)}</div></div>`,
              actionButton("详情", "inspect-fill", fill.fill_id),
            ]),
            "最近还没有成交记录。"
          ),
        })}
      </div>

      <div class="span-12">
        ${surfaceCard({
          title: "执行错误与人工处理",
          kicker: "异常队列",
          copy: "错误不是噪音，而是判断系统是否还能继续自动执行的重要信号。",
          content: errors.length
            ? `
                <div class="alert-list">
                  ${errors
                    .slice(0, 6)
                    .map(
                      (item) => `
                        <article class="alert-item">
                          <div class="panel-head">
                            <strong>${localizeError(item.message || item.status || "execution_issue")}</strong>
                            ${pill(item.severity === "error" ? "错误" : "告警", item.severity === "error" ? "danger" : "warning")}
                          </div>
                          <p class="meta-copy">${readableState(item.subsystem || "execution")} | ${formatMaybeTimestamp(item.observed_at || item.timestamp)}</p>
                          <p>${item.order_id ? `关联订单：${item.order_id}` : "当前没有关联订单号。"}</p>
                        </article>
                      `
                    )
                    .join("")}
                </div>
              `
            : kvList([
                ["最近执行错误数", "0", "当前没有新的执行错误"],
                ["最新订单状态", readableState(latestOrder?.status || "unknown"), latestOrder?.client_order_id || "-"],
                ["最新成交状态", latestFill ? "已写入" : "暂无", latestFill?.fill_id || "-"],
              ]),
        })}
      </div>
    </div>
  `;
}

function executionNarrative({ latestDecision, latestOrder, latestFill, latestReconciliation }) {
  if (!latestOrder) {
    return "最近没有新的订单，说明策略当前更倾向于继续持有、等待，或被策略 / 风控明确阻断。";
  }
  return `最近一笔订单来自决策 ${latestDecision?.decision_id || "-"}，当前状态为 ${readableState(latestOrder.status)}。` +
    `${latestFill ? ` 已有成交写入，最新成交量为 ${formatNumber(latestFill.fill_qty)}。` : " 当前还没有新的成交写入。"} ` +
    `${latestReconciliation ? `最新对账状态为 ${readableState(latestReconciliation.severity)}。` : "当前还没有新的对账结果。"} `;
}

function stuckButton(order) {
  const status = String(order?.status || "").toLowerCase();
  if (!["created", "submitting"].includes(status)) return "";
  return actionButton("处理卡单", "resolve-stuck-order", order.client_order_id, "warning");
}
