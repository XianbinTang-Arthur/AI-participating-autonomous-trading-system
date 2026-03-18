import { formatNumber, formatSigned } from "./formatters.js";
import { readableState } from "./terms.js";

export function inferTradeScene(record = {}) {
  const productType = String(record.product_type || record.productType || "").toLowerCase();
  if (productType === "derivatives") return "derivatives";
  if (productType === "spot") return "spot";
  const marginMode = String(record.margin_mode || record.marginMode || "").toLowerCase();
  if (marginMode === "cross" || marginMode === "isolated") return "derivatives";
  return "spot";
}

export function splitByTradeScene(records = []) {
  const groups = {
    spot: [],
    derivatives: [],
  };
  for (const item of records) {
    groups[inferTradeScene(item)].push(item);
  }
  return [
    { scene: "spot", title: "现货", records: groups.spot },
    { scene: "derivatives", title: "合约", records: groups.derivatives },
  ].filter((group) => group.records.length > 0);
}

export function orderTableHeaders(scene) {
  return scene === "derivatives"
    ? ["合约标的", "仓位动作 / 数量", "委托状态", "最后更新时间", "操作"]
    : ["现货标的", "买卖方向 / 数量", "委托状态", "最后更新时间", "操作"];
}

export function fillTableHeaders(scene) {
  return scene === "derivatives"
    ? ["合约成交", "仓位变化 / 价格", "盈亏与手续费", "落库时间", "查看"]
    : ["现货成交", "买卖数量 / 价格", "成交金额与手续费", "落库时间", "查看"];
}

export function decisionTableHeaders(scene) {
  return scene === "derivatives"
    ? ["时间", "合约标的 / 周期", "仓位结论", "策略与风控", "查看"]
    : ["时间", "现货标的 / 周期", "买卖结论", "策略与风控", "查看"];
}

export function orderSceneSummary(order = {}) {
  return inferTradeScene(order) === "derivatives" ? "合约委托" : "现货委托";
}

export function fillSceneSummary(fill = {}) {
  return inferTradeScene(fill) === "derivatives" ? "合约成交" : "现货成交";
}

export function orderRowTitle(order = {}) {
  return inferTradeScene(order) === "derivatives" ? derivativesOrderAction(order) : spotOrderAction(order);
}

export function orderRowMeta(order = {}) {
  if (inferTradeScene(order) === "derivatives") {
    return `${readableState(order.order_type || "-")} | 仓位调整 ${formatSigned(order.requested_qty)}`;
  }
  return `${readableState(order.order_type || "-")} | 买卖数量 ${formatAssetAmount(order.symbol, order.requested_qty, true)}`;
}

export function fillRowTitle(fill = {}) {
  return inferTradeScene(fill) === "derivatives" ? readableState(fill.position_intent || "-") : readableState(fill.side || "-");
}

export function fillRowMeta(fill = {}) {
  if (inferTradeScene(fill) === "derivatives") {
    return `${formatNumber(fill.fill_qty)} @ ${formatQuotePrice(fill.symbol, fill.fill_price)}`;
  }
  return `${formatAssetAmount(fill.symbol, fill.fill_qty)} @ ${formatQuotePrice(fill.symbol, fill.fill_price)}`;
}

export function fillImpactMeta(fill = {}) {
  const fee = `手续费 ${formatNumber(fill.fee_amount)} ${fill.fee_currency || ""}`.trim();
  if (inferTradeScene(fill) === "derivatives") {
    return fee;
  }
  return `成交额 ${formatQuoteNotional(fill.symbol, fill.fill_qty, fill.fill_price)} | ${fee}`;
}

export function orderDrawerRows(order = {}) {
  return inferTradeScene(order) === "derivatives"
    ? [
        ["合约标的", order.symbol || "-", `${readableState(order.margin_mode || "-")} | ${readableState(order.exposure_side || "-")}`],
        ["仓位动作", derivativesOrderAction(order), `目标杠杆 ${formatNumber(order.target_leverage)} 倍`],
        ["委托状态", readableState(order.status), order.exchange_order_id || "等待交易所订单号"],
        ["计划调整仓位", formatSigned(order.requested_qty), `剩余未成 ${formatNumber(order.remaining_qty)}`],
        ["已成交仓位", formatNumber(order.filled_qty), `成交均价 ${formatQuotePrice(order.symbol, order.average_fill_price)}`],
      ]
    : [
        ["现货标的", order.symbol || "-", `${readableState(order.order_type || "-")} | ${spotOrderAction(order)}`],
        ["委托状态", readableState(order.status), order.exchange_order_id || "等待交易所订单号"],
        ["计划买卖数量", formatAssetAmount(order.symbol, order.requested_qty, true), `剩余未成 ${formatAssetAmount(order.symbol, order.remaining_qty)}`],
        ["已成交数量", formatAssetAmount(order.symbol, order.filled_qty), `成交均价 ${formatQuotePrice(order.symbol, order.average_fill_price)}`],
        ["交易模式", readableState(order.submission_mode || "-"), readableState(order.position_intent || "-")],
      ];
}

export function fillDrawerRows(fill = {}) {
  const baseRows =
    inferTradeScene(fill) === "derivatives"
      ? [
          ["合约标的", fill.symbol || "-", `${readableState(fill.margin_mode || "-")} | ${readableState(fill.exposure_side || "-")}`],
          ["仓位动作", readableState(fill.position_intent || "-"), `${readableState(fill.side || "-")} | ${readableState(fill.liquidity_role || "-")}`],
          ["成交仓位", formatNumber(fill.fill_qty), `成交均价 ${formatQuotePrice(fill.symbol, fill.fill_price)}`],
          ["已实现盈亏", formatSigned(fill.realized_pnl), `手续费 ${formatNumber(fill.fee_amount)} ${fill.fee_currency || ""}`.trim()],
        ]
      : [
          ["现货标的", fill.symbol || "-", `${readableState(fill.side || "-")} | ${readableState(fill.position_intent || "-")}`],
          ["成交数量", formatAssetAmount(fill.symbol, fill.fill_qty), `成交单价 ${formatQuotePrice(fill.symbol, fill.fill_price)}`],
          ["成交金额", formatQuoteNotional(fill.symbol, fill.fill_qty, fill.fill_price), `手续费 ${formatNumber(fill.fee_amount)} ${fill.fee_currency || ""}`.trim()],
          ["盈亏影响", formatSigned(fill.realized_pnl), readableState(fill.liquidity_role || "-")],
        ];
  return baseRows;
}

export function decisionDrawerRows(detail = {}, describeDecisionIntent) {
  const target = detail.position_target || {};
  const productType = inferTradeScene(target.product_type ? target : detail.decision_context || {});
  const intent = typeof describeDecisionIntent === "function" ? describeDecisionIntent(detail) : readableState(target.position_intent || "hold");
  const commonRows = [
    ["交易标的", detail.decision_context?.symbol || "-", detail.decision_context?.timeframe || "-"],
    [productType === "derivatives" ? "仓位动作" : "买卖动作", intent, readableState(target.target_exposure_side || "-")],
    [
      productType === "derivatives" ? "目标净仓位变化" : "目标持仓变化",
      formatSigned(target.delta_position_qty),
      `${productType === "derivatives" ? "目标净仓位" : "目标持仓"} ${formatSigned(target.target_position_qty)}`,
    ],
  ];
  const sceneRows =
    productType === "derivatives"
      ? [["保证金模式", readableState(target.margin_mode || detail.decision_context?.margin_mode || "-"), `目标杠杆 ${formatNumber(target.target_leverage)} 倍`]]
      : [["交易场景", "现货", "现金买卖，不使用杠杆"]];
  return [
    ...commonRows,
    ...sceneRows,
    ["策略门禁", readableState(detail.policy_decision?.execution_allowed ? "ready" : "blocked"), listOrDash(detail.policy_decision?.blocker_reasons)],
    ["风控结论", readableState(detail.risk_decision?.approved ? "ready" : "blocked"), listOrDash(detail.risk_decision?.rejection_reasons)],
  ];
}

function listOrDash(value) {
  if (!value) return "-";
  if (Array.isArray(value)) return value.length ? value.join("、") : "-";
  return String(value);
}

function spotOrderAction(order = {}) {
  const submissionSide = String(order.submission_payload?.side || "").toLowerCase();
  if (submissionSide === "buy") return "买入现货";
  if (submissionSide === "sell") return "卖出现货";
  const intent = String(order.position_intent || "").toLowerCase();
  if (intent.includes("close") || intent.includes("reduce")) return "卖出现货";
  if (intent.includes("open")) return "买入现货";
  return readableState(order.position_intent || "-");
}

function derivativesOrderAction(order = {}) {
  return readableState(order.position_intent || order.exposure_side || "-");
}

function formatAssetAmount(symbol, value, signed = false) {
  const formatted = signed ? formatSigned(value) : formatNumber(value);
  if (formatted === "-") return "-";
  const base = baseAsset(symbol);
  return base ? `${formatted} ${base}` : formatted;
}

function formatQuotePrice(symbol, value) {
  const formatted = formatNumber(value);
  if (formatted === "-") return "-";
  const quote = quoteAsset(symbol);
  return quote ? `${formatted} ${quote}` : formatted;
}

function formatQuoteNotional(symbol, qty, price) {
  const qtyNumber = Number(qty);
  const priceNumber = Number(price);
  if (!Number.isFinite(qtyNumber) || !Number.isFinite(priceNumber)) return "-";
  const quote = quoteAsset(symbol);
  const formatted = formatNumber(qtyNumber * priceNumber);
  return quote ? `${formatted} ${quote}` : formatted;
}

function baseAsset(symbol) {
  return splitSymbol(symbol)[0];
}

function quoteAsset(symbol) {
  return splitSymbol(symbol)[1];
}

function splitSymbol(symbol) {
  const parts = String(symbol || "").split("-").filter(Boolean);
  if (parts.length >= 2) return [parts[0], parts[1]];
  return ["", ""];
}
