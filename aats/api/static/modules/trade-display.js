import { formatNumber, formatSigned, listOrDash } from "./formatters.js";
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
    ? ["标的 / 场景", "动作摘要", "委托状态", "记录时间", "操作"]
    : ["标的 / 场景", "动作摘要", "委托状态", "记录时间", "操作"];
}

export function fillTableHeaders(scene) {
  return scene === "derivatives"
    ? ["标的 / 场景", "成交摘要", "影响摘要", "记录时间", "操作"]
    : ["标的 / 场景", "成交摘要", "影响摘要", "记录时间", "操作"];
}

export function decisionTableHeaders(scene) {
  return scene === "derivatives"
    ? ["记录时间", "标的 / 周期", "结论摘要", "门禁状态", "操作"]
    : ["记录时间", "标的 / 周期", "结论摘要", "门禁状态", "操作"];
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
    return `${readableState(order.order_type, "委托类型待确认")} | 仓位调整 ${formatSigned(order.requested_qty)}`;
  }
  return `${readableState(order.order_type, "委托类型待确认")} | 买卖数量 ${formatAssetAmount(order.symbol, order.requested_qty, true)}`;
}

export function fillRowTitle(fill = {}) {
  return inferTradeScene(fill) === "derivatives"
    ? readableState(preferredDerivativesPositionAction(fill), "成交方向待确认")
    : readableState(fill.side, "成交方向待确认");
}

export function fillRowMeta(fill = {}) {
  if (inferTradeScene(fill) === "derivatives") {
    return `${formatNumber(fill.fill_qty, 4, "数量待确认")} @ ${formatQuotePrice(fill.symbol, fill.fill_price)}`;
  }
  return `${formatAssetAmount(fill.symbol, fill.fill_qty)} @ ${formatQuotePrice(fill.symbol, fill.fill_price)}`;
}

export function fillImpactMeta(fill = {}) {
  const fee = `手续费 ${formatNumber(fill.fee_amount, 4, "待同步")} ${fill.fee_currency || ""}`.trim();
  if (inferTradeScene(fill) === "derivatives") {
    return fee || "手续费待同步";
  }
  return `成交额 ${formatQuoteNotional(fill.symbol, fill.fill_qty, fill.fill_price)} | ${fee || "手续费待同步"}`;
}

export function orderDrawerRows(order = {}) {
  return inferTradeScene(order) === "derivatives"
    ? [
        ["合约标的", order.symbol || "标的待确认", `${readableState(order.margin_mode, "保证金模式待确认")} | ${readableState(order.exposure_side, "方向待确认")}`],
        ["仓位动作", derivativesOrderAction(order), `目标杠杆 ${formatNumber(order.target_leverage)} 倍`],
        ["委托状态", readableState(order.status), order.exchange_order_id || "等待交易所订单号"],
        ["计划调整仓位", formatSigned(order.requested_qty), `剩余未成 ${formatNumber(order.remaining_qty)}`],
        ["已成交仓位", formatNumber(order.filled_qty), `成交均价 ${formatQuotePrice(order.symbol, order.average_fill_price)}`],
      ]
    : [
        ["现货标的", order.symbol || "标的待确认", `${readableState(order.order_type, "委托类型待确认")} | ${spotOrderAction(order)}`],
        ["委托状态", readableState(order.status), order.exchange_order_id || "等待交易所订单号"],
        ["计划买卖数量", formatAssetAmount(order.symbol, order.requested_qty, true), `剩余未成 ${formatAssetAmount(order.symbol, order.remaining_qty)}`],
        ["已成交数量", formatAssetAmount(order.symbol, order.filled_qty), `成交均价 ${formatQuotePrice(order.symbol, order.average_fill_price)}`],
        ["交易模式", readableState(order.submission_mode, "模式待确认"), readableState(order.execution_action || order.position_intent, "意图待确认")],
      ];
}

export function fillDrawerRows(fill = {}) {
  if (inferTradeScene(fill) === "derivatives") {
    return [
      ["合约标的", fill.symbol || "标的待确认", `${readableState(fill.margin_mode, "保证金模式待确认")} | ${readableState(fill.exposure_side, "方向待确认")}`],
      ["仓位动作", readableState(preferredDerivativesPositionAction(fill), "仓位动作待确认"), `${readableState(fill.side, "买卖方向待确认")} | ${readableState(fill.liquidity_role, "流动性角色待确认")}`],
      ["成交仓位", formatNumber(fill.fill_qty), `成交均价 ${formatQuotePrice(fill.symbol, fill.fill_price)}`],
      ["成交名义价值", formatQuoteNotional(fill.symbol, fill.fill_qty, fill.fill_price), `交易所时间 ${fill.exchange_timestamp || "待同步"}`],
      ["仓位前后", `${formatSigned(fill.starting_position_qty)} -> ${formatSigned(fill.ending_position_qty)}`, `均价 ${formatNumber(fill.starting_avg_entry_price, 4, "待同步")} -> ${formatNumber(fill.ending_avg_entry_price, 4, "待同步")}`],
      ["已实现盈亏", formatSigned(fill.realized_pnl), `手续费 ${formatNumber(fill.fee_amount, 4, "待同步")} ${fill.fee_currency || ""}`.trim()],
    ];
  }

  return [
    ["现货标的", fill.symbol || "标的待确认", `${readableState(fill.side, "买卖方向待确认")} | ${readableState(fill.execution_action || fill.position_intent, "成交意图待确认")}`],
    ["成交数量", formatAssetAmount(fill.symbol, fill.fill_qty), `成交单价 ${formatQuotePrice(fill.symbol, fill.fill_price)}`],
    ["成交金额", formatQuoteNotional(fill.symbol, fill.fill_qty, fill.fill_price), `手续费 ${formatNumber(fill.fee_amount, 4, "待同步")} ${fill.fee_currency || ""}`.trim()],
    ["盈亏影响", formatSigned(fill.realized_pnl), readableState(fill.liquidity_role, "流动性角色待确认")],
  ];
}

export function decisionDrawerRows(detail = {}, describeDecisionIntent) {
  const target = detail.position_target || {};
  const productType = inferTradeScene(target.product_type ? target : detail.decision_context || {});
  const intent = typeof describeDecisionIntent === "function" ? describeDecisionIntent(detail) : readableState(target.position_intent || "hold");
  const commonRows = [
    ["交易标的", detail.decision_context?.symbol || "标的待确认", detail.decision_context?.timeframe || "周期待确认"],
    [productType === "derivatives" ? "仓位动作" : "买卖动作", intent, readableState(target.target_exposure_side, "目标方向待确认")],
    [
      productType === "derivatives" ? "目标净仓位变化" : "目标持仓变化",
      formatSigned(target.delta_position_qty),
      `${productType === "derivatives" ? "目标净仓位" : "目标持仓"} ${formatSigned(target.target_position_qty)}`,
    ],
  ];
  const sceneRows =
    productType === "derivatives"
      ? [["保证金模式", readableState(target.margin_mode || detail.decision_context?.margin_mode, "保证金模式待确认"), `目标杠杆 ${formatNumber(target.target_leverage)} 倍`]]
      : [["交易场景", "现货", "现金买卖，不使用杠杆"]];
  return [
    ...commonRows,
    ...sceneRows,
    ["策略门禁", readableState(detail.policy_decision?.execution_allowed ? "ready" : "blocked"), listOrDash(detail.policy_decision?.blocker_reasons, "当前没有额外门禁说明")],
    ["风控结论", readableState(detail.risk_decision?.approved ? "ready" : "blocked"), listOrDash(detail.risk_decision?.rejection_reasons, "当前没有额外风控说明")],
  ];
}

function spotOrderAction(order = {}) {
  const highLevelAction = String(order.execution_action || "").toLowerCase();
  if (highLevelAction === "exit" || highLevelAction === "reduce") return "卖出现货";
  if (highLevelAction === "enter" || highLevelAction === "scale_in") return "买入现货";
  const submissionSide = String(order.submission_payload?.side || "").toLowerCase();
  if (submissionSide === "buy") return "买入现货";
  if (submissionSide === "sell") return "卖出现货";
  const intent = String(order.position_intent || "").toLowerCase();
  if (intent.includes("close") || intent.includes("reduce")) return "卖出现货";
  if (intent.includes("open")) return "买入现货";
  return readableState(order.position_intent, "买卖动作待确认");
}

function derivativesOrderAction(order = {}) {
  return readableState(preferredDerivativesPositionAction(order) || order.exposure_side, "仓位动作待确认");
}

function preferredDerivativesPositionAction(record = {}) {
  const positionIntent = String(record.position_intent || "").toLowerCase();
  if (
    positionIntent === "open_long" ||
    positionIntent === "scale_in_long" ||
    positionIntent === "open_short" ||
    positionIntent === "scale_in_short" ||
    positionIntent === "reduce_long" ||
    positionIntent === "reduce_short" ||
    positionIntent === "close_long" ||
    positionIntent === "close_short" ||
    positionIntent === "reverse_to_long" ||
    positionIntent === "reverse_to_short"
  ) {
    return positionIntent;
  }
  return record.execution_action || record.position_intent;
}

function formatAssetAmount(symbol, value, signed = false) {
  const formatted = signed ? formatSigned(value, 4, "数量待确认") : formatNumber(value, 4, "数量待确认");
  if (formatted === "数量待确认") return formatted;
  const base = baseAsset(symbol);
  return base ? `${formatted} ${base}` : formatted;
}

function formatQuotePrice(symbol, value) {
  const formatted = formatNumber(value, 4, "价格待确认");
  if (formatted === "价格待确认") return formatted;
  const quote = quoteAsset(symbol);
  return quote ? `${formatted} ${quote}` : formatted;
}

function formatQuoteNotional(symbol, qty, price) {
  const qtyNumber = Number(qty);
  const priceNumber = Number(price);
  if (!Number.isFinite(qtyNumber) || !Number.isFinite(priceNumber)) return "成交金额待确认";
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
