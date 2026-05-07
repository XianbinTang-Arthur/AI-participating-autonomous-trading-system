import { actionButton, alertQueue, pill, primaryStatusPanel, renderPaginationFooter, responsiveTable, summaryStrip, surfaceCard } from "../components.js";
import { escapeHtml, formatDuration, formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, middleEllipsis } from "../formatters.js";
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
  const latestOrderIsCurrent = executionLatest.latest_order_is_current_runtime !== false;
  const latestFillIsCurrent = executionLatest.latest_fill_is_current_runtime !== false;
  const latestOrderLabel = latestOrder && !latestOrderIsCurrent ? "历史最新委托" : "最新委托";
  const latestFillLabel = latestFill && !latestFillIsCurrent ? "历史最新成交" : "最新成交";
  const latestReconciliation = executionLatest.latest_reconciliation || null;
  const ordersPayload = data.recentOrders || {};
  const fillsPayload = data.recentFills || {};
  const recentOrders = ordersPayload.orders || [];
  const recentFills = fillsPayload.fills || [];
  const errors = data.executionErrors?.errors || [];
  const metrics = data.metrics || {};
  const lifecycleAttribution = data.positionLifecycleAttribution || {};

  return {
    executionHero: primaryStatusPanel({
      eyebrow: "委托与成交",
      headline: executionHeadline({ latestOrder, latestFill, latestOrderIsCurrent, latestFillIsCurrent, errors }),
      summary: latestOrder || latestFill
        ? "先看当前运行时是否有新委托和成交，再把历史终局记录与当前异常分开判断。"
        : "当前暂无新的委托和成交记录。",
      tone: executionTone({ latestOrder, latestOrderIsCurrent, errors }),
      actions: latestOrder?.client_order_id ? actionButton(latestOrderIsCurrent ? "查看最新委托" : "查看历史委托", "inspect-order", latestOrder.client_order_id) : "",
      pills: [
        pill(`${latestOrderLabel} ${latestOrderStatusLabel(latestOrder)}`, executionTone({ latestOrder, latestOrderIsCurrent, errors })),
        pill(`活动委托 ${formatNumber(metrics.current_open_order_count, 0, "0")}`, metrics.current_open_order_count > 0 ? "warning" : "outline"),
        pill(`最近异常 ${formatNumber(errors.length, 0)}`, errors.length > 0 ? "danger" : "positive"),
      ],
      metrics: [
        { label: latestOrderLabel, value: latestOrderStatusLabel(latestOrder), meta: latestOrder ? `${middleEllipsis(latestOrder?.client_order_id, 10, 6, "暂未生成委托")} | ${latestOrderIsCurrent ? "当前运行时" : "历史记录"}` : "暂未生成委托", tone: latestOrderTone(latestOrder, latestOrderIsCurrent) },
        { label: "最近委托量", value: latestOrder?.requested_qty !== undefined ? formatSigned(latestOrder.requested_qty) : "暂无委托", meta: latestOrder ? `${readableState(latestOrder.order_type, "当前没有委托类型信息")} | ${latestOrder.symbol || "当前没有标的信息"}` : "当前没有最新委托" , tone: latestOrder && latestOrderIsCurrent ? "info" : "neutral" },
        { label: latestFillLabel, value: latestFill ? formatNumber(latestFill.fill_qty) : "暂未成交", meta: latestFill ? `价格 ${formatNumber(latestFill.fill_price)} | ${middleEllipsis(latestFill.fill_id)} | ${latestFillIsCurrent ? "当前运行时" : "历史记录"}` : "当前暂无成交编号", tone: latestFill && latestFillIsCurrent ? "positive" : "neutral" },
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
              copy: item.order_id ? `关联委托：${item.order_id}` : "系统级异常，无关联委托。",
              meta: `${readableState(item.subsystem || "execution")} | ${formatMaybeTimestamp(item.observed_at || item.timestamp)}`,
              tone: item.severity === "error" ? "danger" : "warning",
              pill: pill(item.severity === "error" ? "错误" : "告警", item.severity === "error" ? "danger" : "warning"),
            })),
            "当前暂无新的执行异常。"
          )
        : summaryStrip([
            { label: "异常数", value: "0", meta: "当前暂无新的执行异常", tone: "positive" },
            { label: `${latestOrderLabel}状态`, value: latestOrderStatusLabel(latestOrder), meta: latestOrder ? `${middleEllipsis(latestOrder?.client_order_id, 10, 6, "当前没有委托编号")} | ${latestOrderIsCurrent ? "当前运行时" : "历史记录"}` : "当前没有委托编号", tone: latestOrderTone(latestOrder, latestOrderIsCurrent) },
            { label: `${latestFillLabel}状态`, value: latestFill ? "已落库" : "暂无", meta: latestFill ? `${middleEllipsis(latestFill?.fill_id, 10, 6, "当前没有成交编号")} | ${latestFillIsCurrent ? "当前运行时" : "历史记录"}` : "当前没有成交编号", tone: latestFill && latestFillIsCurrent ? "positive" : "neutral" },
          ]),
    }),
    executionOrders: surfaceCard({
      title: "委托记录",
      kicker: "委托状态",
      copy: "按现货和合约分别查看，判断哪类委托在排队、卡住或反复失败。",
      content: `${renderOrderGroups(recentOrders)}${renderPaginationFooter({
        shown: Number(ordersPayload?.orders?.length || 0),
        total: ordersPayload?.total_available,
        hasMore: ordersPayload?.has_more,
        limit: ordersPayload?.limit,
        label: "委托记录",
        loadAction: "load-more-orders",
        collapseAction: "collapse-orders",
      })}`,
    }),
    executionFills: surfaceCard({
      title: "成交记录",
      kicker: "成交状态",
      copy: "确认最近成交是否已经稳定落库，并补充对盈亏和手续费的上下文判断。",
      content: `${renderFillGroups(recentFills)}${renderPaginationFooter({
        shown: Number(fillsPayload?.fills?.length || 0),
        total: fillsPayload?.total_available,
        hasMore: fillsPayload?.has_more,
        limit: fillsPayload?.limit,
        label: "成交记录",
        loadAction: "load-more-fills",
        collapseAction: "collapse-fills",
      })}`,
    }),
    executionLifecycleDiagnostics: surfaceCard({
      title: "仓位生命周期诊断",
      kicker: "整笔仓位复盘",
      panelKey: "positionLifecycleAttribution",
      copy: "这里按整笔仓位口径看综合净收益、费用拆分和退出链，不按单笔委托口径展示。",
      content: renderLifecycleDiagnostics(lifecycleAttribution),
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
      <div class="span-12">${sections.executionLifecycleDiagnostics}</div>
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
              `<div><strong>${escapeHtml(order.symbol || "标的待确认")}</strong><div class="table-meta">${group.scene === "derivatives" ? `${readableState(order.margin_mode, "保证金模式待确认")} | ${readableState(order.exposure_side, "方向待确认")}` : readableState(order.order_type, "订单类型未记录")}</div></div>`,
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
                { label: "交易场景", value: group.scene === "derivatives" ? "合约委托" : "现货委托", meta: group.scene === "derivatives" ? `${readableState(order.margin_mode, "保证金模式待确认")} | ${readableState(order.exposure_side, "方向待确认")}` : readableState(order.order_type, "订单类型未记录") },
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

// #11 修复：renderPaginationFooter 的本地定义已删除，统一到 components.js。

function executionHeadline({ latestOrder, latestFill, latestOrderIsCurrent, latestFillIsCurrent, errors }) {
  if (errors.length > 0) return errors.some((item) => item.order_id) ? "执行委托存在异常" : "执行子系统存在异常";
  if (!latestOrder || !latestOrderIsCurrent) return "当前暂无新的委托";
  if (latestFill && latestFillIsCurrent) return `最新${fillSceneSummary(latestFill)}已落库`;
  return `最近一笔${orderSceneSummary(latestOrder)}处于 ${readableState(latestOrder.status)} 阶段`;
}

function executionTone({ latestOrder, latestOrderIsCurrent, errors }) {
  if (errors.length > 0) return "danger";
  if (!latestOrder || !latestOrderIsCurrent) return "neutral";
  if (["created", "submitting", "partially_filled", "cancel_pending"].includes(String(latestOrder?.status || "").toLowerCase())) return "warning";
  return "positive";
}

function latestOrderTone(order = null, isCurrentRuntime = true) {
  if (!order) return "neutral";
  if (!isCurrentRuntime) return "neutral";
  return toneForOrderStatus(order.status);
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

function renderLifecycleDiagnostics(payload = {}) {
  const summary = payload.summary || {};
  const lifecycles = Array.isArray(payload.lifecycles) ? payload.lifecycles : [];
  return `
    ${summaryStrip([
      {
        label: "生命周期数",
        value: formatNumber(summary.lifecycle_count, 0, "0"),
        meta: `最近展示 ${formatNumber(lifecycles.length, 0, "0")} 笔`,
        tone: "info",
      },
      {
        label: "综合净收益",
        value: formatSigned(summary.combined_net_realized_pnl),
        meta: "整笔仓位口径",
        tone: Number(summary.combined_net_realized_pnl || 0) >= 0 ? "positive" : "warning",
      },
      {
        label: "总手续费",
        value: formatSigned(summary.total_fee_quote),
        meta: `资金费 ${formatSigned(summary.funding_fee_quote)}`,
        tone: Number(summary.total_fee_quote || 0) > 0 ? "warning" : "info",
      },
      {
        label: "未归属资金费",
        value: formatNumber(summary.unassigned_funding_fee_count, 0, "0"),
        meta: "用于暴露仍未归到整笔仓位的账单事件",
        tone: Number(summary.unassigned_funding_fee_count || 0) > 0 ? "warning" : "positive",
      },
    ])}
    ${responsiveTable(
      ["仓位", "综合净收益", "费用 / 资金费", "持有与退出", "操作"],
      lifecycles.map((item) => [
        `<div><strong>${escapeHtml(item.symbol || "标的待确认")}</strong><div class="table-meta">${escapeHtml(lifecycleHeadline(item))}</div></div>`,
        `<div><strong>${formatSigned(item.combined_net_realized_pnl)}</strong><div class="table-meta">毛收益 ${formatSigned(item.gross_realized_pnl)}</div></div>`,
        `<div><strong>${formatSigned(item.total_fee_quote)}</strong><div class="table-meta">开 ${formatSigned(item.entry_fee_quote)} / 平 ${formatSigned(item.exit_fee_quote)} / 资金费 ${formatSigned(item.funding_fee_quote)}</div></div>`,
        `<div><strong>${formatDuration(item.hold_seconds, "持有时长待确认")}</strong><div class="table-meta">${escapeHtml(lifecycleExitReasonSummary(item.exit_reason_breakdown))}</div></div>`,
        item.lifecycle_id ? actionButton("查看诊断", "inspect-lifecycle-attribution", item.lifecycle_id) : "",
      ]),
      "当前还没有仓位生命周期诊断样本。",
      lifecycles.map((item) => ({
        kicker: "仓位生命周期",
        title: `${item.symbol || "标的待确认"} | ${lifecycleHeadline(item)}`,
        meta: item.closed_at ? formatMaybeTimestamp(item.closed_at) : "当前仍未闭合",
        tone: Number(item.combined_net_realized_pnl || 0) >= 0 ? "positive" : "warning",
        badge: pill(
          Number(item.combined_net_realized_pnl || 0) >= 0 ? "综合净收益为正" : "综合净收益为负",
          Number(item.combined_net_realized_pnl || 0) >= 0 ? "positive" : "warning",
        ),
        fields: [
          { label: "综合净收益", value: formatSigned(item.combined_net_realized_pnl), meta: `毛收益 ${formatSigned(item.gross_realized_pnl)}` },
          { label: "总手续费", value: formatSigned(item.total_fee_quote), meta: `资金费 ${formatSigned(item.funding_fee_quote)}` },
          { label: "持有时长", value: formatDuration(item.hold_seconds, "持有时长待确认"), meta: lifecycleExitReasonSummary(item.exit_reason_breakdown) },
        ],
        details: [
          { label: "生命周期编号", value: item.lifecycle_id || "当前没有编号" },
          { label: "成交拆分", value: `${formatNumber(item.entry_fill_count, 0, "0")} 开 / ${formatNumber(item.exit_fill_count, 0, "0")} 平` },
          { label: "子委托", value: formatNumber(item.child_order_count, 0, "0") },
        ],
        detailLabel: "展开仓位生命周期摘要",
        action: item.lifecycle_id ? actionButton("查看诊断", "inspect-lifecycle-attribution", item.lifecycle_id) : "",
      })),
    )}
  `;
}

function lifecycleHeadline(item = {}) {
  return [
    readableState(item.direction || item.pos_side || "unknown", "方向待确认"),
    item.timeframe || "周期待确认",
    readableState(item.family || "unknown", item.family || "策略家族待确认"),
  ]
    .filter(Boolean)
    .join(" | ");
}

function lifecycleExitReasonSummary(rows = []) {
  if (!Array.isArray(rows) || !rows.length) return "当前还没有退出链诊断";
  return rows
    .slice(0, 2)
    .map((row) => `${localizeError(row.reason || "unknown")} / ${lifecycleTransitionLabel(row.transition_category)}`)
    .join(" | ");
}

function lifecycleTransitionLabel(value) {
  const key = String(value || "").trim().toLowerCase();
  if (key === "strategy_exit") return "策略退出";
  if (key === "protective_exit") return "保护退出";
  if (key === "execution_guard_exit") return "执行守护退出";
  return key ? readableState(key, "退出分类待确认") : "退出分类待确认";
}
