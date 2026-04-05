import { actionButton, alertQueue, pill, primaryStatusPanel, responsiveTable, summaryStrip, surfaceCard } from "../components.js";
import { escapeHtml, formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, middleEllipsis } from "../formatters.js";
import { localizeError, readableState, toneForOrderStatus } from "../terms.js";
import {
  fillFeeText,
  fillImpactMeta,
  fillRowMeta,
  fillRowTitle,
  fillSceneSummary,
  orderRowMeta,
  orderRowTitle,
  orderSceneSummary,
  orderTableHeaders,
  fillTableHeaders,
  splitByTradeScene,
} from "../trade-display.js";

export function renderExecutionSections(data) {
  const executionLatest = data.executionLatest || {};
  const latestOrder = executionLatest.latest_order || null;
  const latestFill = executionLatest.latest_fill || null;
  const latestReconciliation = executionLatest.latest_reconciliation || null;
  const ordersPayload = data.recentOrders || {};
  const fillsPayload = data.recentFills || {};
  const recentOrders = ordersPayload.orders || [];
  const recentFills = fillsPayload.fills || [];
  const errors = data.executionErrors?.errors || [];
  const metrics = data.metrics || {};

  return {
    executionHero: primaryStatusPanel({
      eyebrow: "委托与成交",
      headline: executionHeadline({ latestOrder, latestFill, errors }),
      summary: latestOrder || latestFill ? "先看最近委托和成交有没有真正落地，再看是否仍有异常在收敛。" : "当前暂无新的委托和成交记录。",
      tone: executionTone({ latestOrder, errors }),
      actions: latestOrder?.client_order_id ? actionButton("查看最新委托", "inspect-order", latestOrder.client_order_id) : "",
      pills: [
        pill(`最新委托 ${latestOrderStatusLabel(latestOrder)}`, executionTone({ latestOrder, errors })),
        pill(`活动委托 ${formatNumber(metrics.current_open_order_count, 0)}`, metrics.current_open_order_count > 0 ? "warning" : "outline"),
        pill(`最近异常 ${formatNumber(errors.length, 0)}`, errors.length > 0 ? "danger" : "positive"),
      ],
      metrics: [
        { label: "最新委托", value: latestOrderStatusLabel(latestOrder), meta: middleEllipsis(latestOrder?.client_order_id, 10, 6, "暂未生成委托"), tone: toneForOrderStatus(latestOrder?.status) },
        { label: "最近委托量", value: latestOrder?.requested_qty !== undefined ? formatSigned(latestOrder.requested_qty) : "暂无委托", meta: latestOrder ? `${readableState(latestOrder.order_type, "当前没有委托类型信息")} | ${latestOrder.symbol || "当前没有标的信息"}` : "当前没有最新委托" , tone: latestOrder ? "info" : "neutral" },
        { label: "最新成交", value: latestFill ? formatNumber(latestFill.fill_qty) : "暂未成交", meta: latestFill ? `价格 ${formatNumber(latestFill.fill_price)} | ${middleEllipsis(latestFill.fill_id)}` : "当前暂无成交编号", tone: latestFill ? "positive" : "neutral" },
        { label: "最新对账", value: latestReconciliation ? readableState(latestReconciliation.severity || "unknown") : "暂无对账", meta: middleEllipsis(latestReconciliation?.reconciliation_id, 10, 6, "暂时没有最新对账"), tone: latestReconciliation?.halt_required ? "danger" : latestReconciliation?.severity ? "warning" : "neutral" },
      ],
    }),
    executionExceptions: surfaceCard({
      title: "执行异常",
      kicker: "异常处理",
      copy: "先判断执行链路有没有卡住，再去看历史委托和成交明细。",
      classes: errors.length ? "" : "is-muted",
      content: errors.length
        ? alertQueue(
            errors.slice(0, 6).map((item) => ({
              title: localizeError(item.message || item.status || "execution_issue"),
              copy: item.order_id ? `关联委托：${item.order_id}` : "当前没有关联的委托编号。",
              meta: `${readableState(item.subsystem || "execution")} | ${formatMaybeTimestamp(item.observed_at || item.timestamp)}`,
              tone: item.severity === "error" ? "danger" : "warning",
              pill: pill(item.severity === "error" ? "错误" : "告警", item.severity === "error" ? "danger" : "warning"),
            })),
            "当前暂无新的执行异常。"
          )
        : summaryStrip([
            { label: "异常数", value: "0", meta: "当前暂无新的执行异常", tone: "positive" },
            { label: "最新委托状态", value: latestOrderStatusLabel(latestOrder), meta: middleEllipsis(latestOrder?.client_order_id, 10, 6, "当前没有委托编号"), tone: toneForOrderStatus(latestOrder?.status) },
            { label: "最新成交状态", value: latestFill ? "已落库" : "暂无", meta: middleEllipsis(latestFill?.fill_id, 10, 6, "当前没有成交编号"), tone: latestFill ? "positive" : "neutral" },
          ]),
    }),
    executionOrders: surfaceCard({
      title: "委托记录",
      kicker: "委托状态",
      copy: "按现货和合约分别查看，判断哪类委托在排队、卡住或反复失败。",
      content: `${renderOrderGroups(recentOrders)}${renderPaginationFooter({
        payload: ordersPayload,
        key: "orders",
        singular: "委托",
        loadAction: "load-more-orders",
        collapseAction: "collapse-orders",
      })}`,
    }),
    executionFills: surfaceCard({
      title: "成交记录",
      kicker: "成交状态",
      copy: "确认最近成交是否已经稳定落库，并补充对盈亏和手续费的上下文判断。",
      content: `${renderFillGroups(recentFills)}${renderPaginationFooter({
        payload: fillsPayload,
        key: "fills",
        singular: "成交",
        loadAction: "load-more-fills",
        collapseAction: "collapse-fills",
      })}`,
    }),
  };
}

export function renderExecutionView(data) {
  const sections = renderExecutionSections(data);
  return `
    <div class="panel-grid">
      <div class="span-12">${sections.executionHero}</div>
      <div class="span-12">${sections.executionExceptions}</div>
      <div class="span-12">${sections.executionOrders}</div>
      <div class="span-12">${sections.executionFills}</div>
    </div>
  `;
}

function renderOrderGroups(recentOrders) {
  const groups = splitByTradeScene(recentOrders);
  if (!groups.length) {
    return '<div class="empty-state">当前暂无委托记录。</div>';
  }
  return groups
    .map((group) => {
      const sceneLabel = group.scene === "derivatives" ? "合约委托" : "现货委托";
      return `
        <section class="subsection-block">
          <div class="panel-head">
            <div>
              <p class="panel-kicker">${sceneLabel}</p>
              <p class="meta-copy">${group.scene === "derivatives" ? "重点看开仓、减仓、平仓和卡单。" : "重点看买入、卖出和未成交数量。"}</p>
            </div>
          </div>
          ${responsiveTable(
            orderTableHeaders(group.scene),
            group.records.map((order) => [
              `<div><strong>${escapeHtml(order.symbol || "标的待确认")}</strong><div class="table-meta">${group.scene === "derivatives" ? `${readableState(order.margin_mode, "保证金模式待确认")} | ${readableState(order.exposure_side, "方向待确认")}` : readableState(order.order_type, "委托类型待确认")}</div></div>`,
              `<div><strong>${orderRowTitle(order)}</strong><div class="table-meta">${orderRowMeta(order)}</div></div>`,
              `<div><strong>${readableState(order.status)}</strong><div class="table-meta">${escapeHtml(order.exchange_order_id || "等待交易所回执")}</div></div>`,
              `<div><strong>${formatRelativeAge(order.last_update_ts || order.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(order.last_update_ts || order.created_at)}</div></div>`,
              `<div class="stack-actions">${actionButton("查看详情", "inspect-order", order.client_order_id)}${stuckButton(order)}</div>`,
            ]),
            "当前暂无委托记录。",
            group.records.map((order) => ({
              kicker: group.scene === "derivatives" ? "合约委托" : "现货委托",
              title: `${order.symbol || "标的待确认"} | ${orderRowTitle(order)}`,
              meta: `${formatRelativeAge(order.last_update_ts || order.created_at)} | ${formatMaybeTimestamp(order.last_update_ts || order.created_at)}`,
              tone: ["failed", "rejected"].includes(String(order.status || "").toLowerCase()) ? "danger" : ["created", "submitting", "partially_filled", "cancel_pending"].includes(String(order.status || "").toLowerCase()) ? "warning" : "positive",
              badge: pill(readableState(order.status), ["failed", "rejected"].includes(String(order.status || "").toLowerCase()) ? "danger" : ["created", "submitting", "partially_filled", "cancel_pending"].includes(String(order.status || "").toLowerCase()) ? "warning" : "positive"),
              fields: [
                { label: "交易场景", value: group.scene === "derivatives" ? "合约委托" : "现货委托", meta: group.scene === "derivatives" ? `${readableState(order.margin_mode, "保证金模式待确认")} | ${readableState(order.exposure_side, "方向待确认")}` : readableState(order.order_type, "委托类型待确认") },
                { label: "委托摘要", value: orderRowTitle(order), meta: orderRowMeta(order) },
                { label: "交易所回执", value: order.exchange_order_id || "等待回执" },
              ],
              details: [
                { label: "委托状态", value: readableState(order.status), meta: order.exchange_order_id || "等待交易所回执" },
                { label: "创建时间", value: formatMaybeTimestamp(order.created_at), meta: formatRelativeAge(order.created_at) },
                { label: "最后更新时间", value: formatMaybeTimestamp(order.last_update_ts || order.created_at), meta: formatRelativeAge(order.last_update_ts || order.created_at) },
              ],
              detailLabel: "展开委托详情",
              action: `<div class="stack-actions">${actionButton("查看详情", "inspect-order", order.client_order_id)}${stuckButton(order)}</div>`,
            }))
          )}
        </section>
      `;
    })
    .join("");
}

function renderFillGroups(recentFills) {
  const groups = splitByTradeScene(recentFills);
  if (!groups.length) {
    return '<div class="empty-state">当前暂无成交记录。</div>';
  }
  return groups
    .map((group) => {
      const sceneLabel = group.scene === "derivatives" ? "合约成交" : "现货成交";
      return `
        <section class="subsection-block">
          <div class="panel-head">
            <div>
              <p class="panel-kicker">${sceneLabel}</p>
              <p class="meta-copy">${group.scene === "derivatives" ? "重点看仓位变化、已实现盈亏和手续费。" : "重点看买卖数量、成交金额和手续费。"}</p>
            </div>
          </div>
          ${responsiveTable(
            fillTableHeaders(group.scene),
            group.records.map((fill) => {
              const impact = fillImpactDisplay(fill, group.scene);
              return [
                `<div><strong>${escapeHtml(fill.symbol || "标的待确认")}</strong><div class="table-meta">${group.scene === "derivatives" ? `${readableState(fill.margin_mode, "保证金模式待确认")} | ${readableState(fill.exposure_side, "方向待确认")}` : readableState(fill.side, "方向待确认")}</div></div>`,
                `<div><strong>${fillRowTitle(fill)}</strong><div class="table-meta">${fillRowMeta(fill)}</div></div>`,
                `<div><strong>${impact.value}</strong><div class="table-meta">${impact.meta}</div></div>`,
                `<div><strong>${formatRelativeAge(fill.ingestion_timestamp)}</strong><div class="table-meta">${formatMaybeTimestamp(fill.ingestion_timestamp)}</div></div>`,
                actionButton("查看详情", "inspect-fill", fill.fill_id),
              ];
            }),
            "当前暂无成交记录。",
            group.records.map((fill) => {
              const impact = fillImpactDisplay(fill, group.scene);
              return {
                kicker: group.scene === "derivatives" ? "合约成交" : "现货成交",
                title: `${fill.symbol || "标的待确认"} | ${fillRowTitle(fill)}`,
                meta: `${formatRelativeAge(fill.ingestion_timestamp)} | ${formatMaybeTimestamp(fill.ingestion_timestamp)}`,
                tone: group.scene === "derivatives" ? "info" : "positive",
                badge: pill(group.scene === "derivatives" ? "合约成交" : "现货成交", "info"),
                fields: [
                  { label: "成交方向", value: group.scene === "derivatives" ? readableState(fill.exposure_side, "方向待确认") : readableState(fill.side, "方向待确认"), meta: group.scene === "derivatives" ? readableState(fill.margin_mode, "保证金模式待确认") : fillRowMeta(fill) },
                  { label: "成交摘要", value: fillRowTitle(fill), meta: fillRowMeta(fill) },
                  { label: "盈亏 / 影响", value: impact.value, meta: impact.meta },
                ],
                details: [
                  { label: "成交时间", value: formatMaybeTimestamp(fill.ingestion_timestamp), meta: formatRelativeAge(fill.ingestion_timestamp) },
                  { label: "成交编号", value: fill.fill_id || "当前没有编号" },
                  { label: "补充说明", value: impact.meta },
                ],
                detailLabel: "展开成交详情",
                action: actionButton("查看详情", "inspect-fill", fill.fill_id),
              };
            })
          )}
        </section>
      `;
    })
    .join("");
}

function renderPaginationFooter({ payload, key, singular, loadAction, collapseAction }) {
  const shown = Number(payload?.[key]?.length || 0);
  const total = Number(payload?.total_available || shown);
  const hasMore = Boolean(payload?.has_more);
  const limit = Number(payload?.limit || shown);
  if (!shown) return "";
  return `
    <div class="history-footer">
      <p class="meta-copy">当前显示 ${shown} / ${total} 条${singular}记录。</p>
      <div class="stack-actions">
        ${hasMore ? actionButton(`加载更多${singular}`, loadAction, "", "secondary") : ""}
        ${limit > 8 ? actionButton("收起到最新 8 条", collapseAction, "", "ghost") : ""}
      </div>
    </div>
  `;
}

function executionHeadline({ latestOrder, latestFill, errors }) {
  if (errors.length > 0) return "执行链路存在异常";
  if (!latestOrder) return "当前暂无新的委托";
  if (latestFill) return `最新${fillSceneSummary(latestFill)}已落库`;
  return `最近一笔${orderSceneSummary(latestOrder)}处于 ${readableState(latestOrder.status)} 阶段`;
}

function executionTone({ latestOrder, errors }) {
  if (errors.length > 0) return "danger";
  if (["created", "submitting", "partially_filled", "cancel_pending"].includes(String(latestOrder?.status || "").toLowerCase())) return "warning";
  return "positive";
}

function latestOrderStatusLabel(order = null) {
  if (!order) return "暂无委托";
  return readableState(order.status || "unknown");
}

function stuckButton(order) {
  const status = String(order?.status || "").toLowerCase();
  if (!["created", "submitting"].includes(status)) return "";
  return actionButton("处理卡单", "resolve-stuck-order", order.client_order_id, "warning");
}

function fillImpactDisplay(fill, scene) {
  if (scene !== "derivatives") {
    const meta = fillImpactMeta(fill);
    const [value] = String(meta || "").split(" | ");
    return {
      value: value && value !== "当前没有额外说明" ? value : "影响待确认",
      meta: meta && meta !== "当前没有额外说明" ? meta : "当前暂无足够的成交影响上下文",
    };
  }

  const realizedPnl = Number(fill?.realized_pnl);
  const feeText = fillFeeText(fill, { includeCurrency: false });
  if (Number.isFinite(realizedPnl)) {
    return {
      value: formatSigned(realizedPnl),
      meta: feeText,
    };
  }
  return {
    value: "逐笔盈亏未单独落库",
    meta: `${feeText} | 当前成交已落库，但这条成交记录没有单独保存已实现盈亏`,
  };
}
