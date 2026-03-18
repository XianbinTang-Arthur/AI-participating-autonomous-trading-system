import { actionButton, callout, kvList, pill, statGrid, surfaceCard, table } from "../components.js";
import { formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned } from "../formatters.js";
import { localizeError, readableState } from "../terms.js";
import {
  fillImpactMeta,
  fillRowMeta,
  fillRowTitle,
  fillSceneSummary,
  fillTableHeaders,
  orderRowMeta,
  orderRowTitle,
  orderSceneSummary,
  orderTableHeaders,
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
  const latestDecision = data.latestDecision || {};

  return {
    executionHero: surfaceCard({
      title: "委托执行总览",
      kicker: "最新执行状态",
      copy: "这里重点回答三件事：最近一笔委托走到了哪一步、最近一笔成交有没有落库、当前是否存在会影响自动交易的执行异常。",
      classes: "hero-card",
      actions: latestOrder?.client_order_id ? actionButton("查看最新委托", "inspect-order", latestOrder.client_order_id) : "",
      content: `
        ${callout({
          title: latestOrder ? `最新${orderSceneSummary(latestOrder)}：${readableState(latestOrder.status)}` : "当前还没有新的委托记录",
          copy: executionNarrative({ latestDecision, latestOrder, latestFill, latestReconciliation }),
          pills: [
            pill(`最新成交：${latestFill ? fillSceneSummary(latestFill) : "暂无"}`, latestFill ? "positive" : "outline"),
            pill(`对账：${readableState(latestReconciliation?.severity || "unknown")}`, latestReconciliation?.halt_required ? "danger" : "info"),
            pill(`活动委托数：${formatNumber(metrics.current_open_order_count)}`, metrics.current_open_order_count > 0 ? "warning" : "outline"),
          ],
        })}
        ${statGrid([
          { label: "最新委托", value: readableState(latestOrder?.status || "unknown"), meta: latestOrder?.client_order_id || "暂无委托" },
          { label: "最近委托量", value: latestOrder ? latestOrder.requested_qty !== undefined ? formatSigned(latestOrder.requested_qty) : "-" : "-", meta: latestOrder ? `${readableState(latestOrder.order_type || "-")} | ${latestOrder.symbol || "-"}` : "-" },
          { label: "最新成交量", value: formatNumber(latestFill?.fill_qty), meta: latestFill ? `成交价 ${formatNumber(latestFill.fill_price)}` : "暂无成交" },
          { label: "最近异常数", value: formatNumber(errors.length), meta: errors[0] ? localizeError(errors[0].message || errors[0].status) : "近期没有执行异常" },
        ])}
      `,
    }),
    executionOrders: surfaceCard({
      title: "最近委托",
      kicker: "委托状态变化",
      copy: "按现货和合约分别查看委托，更容易判断到底是现货买卖、合约开平，还是某一类委托在反复失败或卡住。",
      content: `${renderOrderGroups(recentOrders)}${renderPaginationFooter({
        payload: ordersPayload,
        key: "orders",
        singular: "委托",
        loadAction: "load-more-orders",
        collapseAction: "collapse-orders",
      })}`,
    }),
    executionFills: surfaceCard({
      title: "最近成交",
      kicker: "成交落库确认",
      copy: "按现货和合约分别核对成交，便于分清现金买卖、仓位变化、手续费影响和已实现盈亏。",
      content: `${renderFillGroups(recentFills)}${renderPaginationFooter({
        payload: fillsPayload,
        key: "fills",
        singular: "成交",
        loadAction: "load-more-fills",
        collapseAction: "collapse-fills",
      })}`,
    }),
    executionExceptions: surfaceCard({
      title: "执行异常与人工处理",
      kicker: "异常队列",
      copy: "执行报错不是噪音，而是判断系统还能不能继续自动交易的重要信号。",
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
                      <p>${item.order_id ? `关联委托：${item.order_id}` : "当前没有关联委托号。"}</p>
                    </article>
                  `
                )
                .join("")}
            </div>
          `
        : kvList([
            ["最近执行异常数", "0", "当前没有新的执行异常"],
            ["最新委托状态", readableState(latestOrder?.status || "unknown"), latestOrder?.client_order_id || "-"],
            ["最新成交状态", latestFill ? "已落库" : "暂无", latestFill?.fill_id || "-"],
          ]),
    }),
  };
}

export function renderExecutionView(data) {
  const sections = renderExecutionSections(data);
  return `
    <div class="panel-grid">
      <div class="span-12">${sections.executionHero}</div>
      <div class="span-7">${sections.executionOrders}</div>
      <div class="span-5">${sections.executionFills}</div>
      <div class="span-12">${sections.executionExceptions}</div>
    </div>
  `;
}

function renderOrderGroups(recentOrders) {
  const groups = splitByTradeScene(recentOrders);
  if (!groups.length) {
    return '<div class="empty-state">最近还没有委托。</div>';
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
          ${table(
            orderTableHeaders(group.scene),
            group.records.map((order) => [
              `<div><strong>${order.symbol || "-"}</strong><div class="table-meta">${group.scene === "derivatives" ? `${readableState(order.margin_mode || "-")} | ${readableState(order.exposure_side || "-")}` : readableState(order.order_type || "-")}</div></div>`,
              `<div><strong>${orderRowTitle(order)}</strong><div class="table-meta">${orderRowMeta(order)}</div></div>`,
              `<div><strong>${readableState(order.status)}</strong><div class="table-meta">${order.exchange_order_id || "等待交易所回执"}</div></div>`,
              `<div><strong>${formatRelativeAge(order.last_update_ts || order.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(order.last_update_ts || order.created_at)}</div></div>`,
              `<div class="stack-actions">${actionButton("详情", "inspect-order", order.client_order_id)}${stuckButton(order)}</div>`,
            ]),
            "最近还没有委托。"
          )}
        </section>
      `;
    })
    .join("");
}

function renderFillGroups(recentFills) {
  const groups = splitByTradeScene(recentFills);
  if (!groups.length) {
    return '<div class="empty-state">最近还没有成交记录。</div>';
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
          ${table(
            fillTableHeaders(group.scene),
            group.records.map((fill) => [
              `<div><strong>${fill.symbol || "-"}</strong><div class="table-meta">${group.scene === "derivatives" ? `${readableState(fill.margin_mode || "-")} | ${readableState(fill.exposure_side || "-")}` : readableState(fill.side || "-")}</div></div>`,
              `<div><strong>${fillRowTitle(fill)}</strong><div class="table-meta">${fillRowMeta(fill)}</div></div>`,
              `<div><strong>${group.scene === "derivatives" ? formatSigned(fill.realized_pnl) : fillImpactMeta(fill).split(" | ")[0]}</strong><div class="table-meta">${fillImpactMeta(fill)}</div></div>`,
              `<div><strong>${formatRelativeAge(fill.ingestion_timestamp)}</strong><div class="table-meta">${formatMaybeTimestamp(fill.ingestion_timestamp)}</div></div>`,
              actionButton("详情", "inspect-fill", fill.fill_id),
            ]),
            "最近还没有成交记录。"
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
      <p class="meta-copy">当前展示 ${shown} / ${total} 条${singular}记录。</p>
      <div class="stack-actions">
        ${hasMore ? actionButton(`加载更多${singular}`, loadAction, "", "secondary") : ""}
        ${limit > 8 ? actionButton("收起到最新 8 条", collapseAction, "", "ghost") : ""}
      </div>
    </div>
  `;
}

function executionNarrative({ latestDecision, latestOrder, latestFill, latestReconciliation }) {
  if (!latestOrder) {
    return "最近没有新的委托，通常表示策略当前仍在继续观望，或者虽然允许交易，但这一轮并没有形成真正需要下单的信号。";
  }
  return `最近一笔${orderSceneSummary(latestOrder)}来自决策 ${latestDecision?.decision_id || "-"}，当前状态为 ${readableState(latestOrder.status)}。` +
    `${latestFill ? ` 最新一笔${fillSceneSummary(latestFill)}已经落库，数量为 ${formatNumber(latestFill.fill_qty)}。` : " 当前还没有新的成交落库。"} ` +
    `${latestReconciliation ? `最新对账结论为 ${readableState(latestReconciliation.severity)}。` : "当前还没有新的对账结论。"}`;
}

function stuckButton(order) {
  const status = String(order?.status || "").toLowerCase();
  if (!["created", "submitting"].includes(status)) return "";
  return actionButton("处理卡单", "resolve-stuck-order", order.client_order_id, "warning");
}
