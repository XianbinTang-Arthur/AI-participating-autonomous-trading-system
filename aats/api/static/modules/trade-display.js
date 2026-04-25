import { formatNumber, formatSigned, listOrDash } from "./formatters.js";
import { readableState } from "./terms.js";

// #32 修复：原本 detail-drawers.js 和 ai-view.js 各定义了一份同名 EXECUTION_SUGGESTION_LABELS
// 和 executionSuggestionLabel，一改要改两处（strategy-view 之前也有一份，已在更早的重构里
// 删掉）。统一搬到 trade-display.js（执行相关展示 helper 的自然归属），两边改为 import。
const EXECUTION_SUGGESTION_LABELS = {
  passive_bias: "被动倾向",
  maker_taker_bias: "主被动偏向",
  slice_count: "拆单数",
  max_participation_rate: "最大参与率",
  max_cross_spread_bps: "最大跨价差",
  cancel_replace_patience_ms: "撤改单等待",
};

export function executionSuggestionLabel(key) {
  const normalized = String(key || "").trim();
  return EXECUTION_SUGGESTION_LABELS[normalized] || normalized;
}

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

// #23 修复：原本 orderTableHeaders / fillTableHeaders / decisionTableHeaders
// 都根据 scene === "derivatives" 走两个分支，但两个分支返回的列表完全一样，
// 等于 scene 是 dead parameter。
//
// 之所以一直留着这个分支结构，是因为最早的产品计划里"现货 vs 合约"会展示
// 不同的列（例如合约要多一列开仓方向 long/short）。后来我们改成把方向信息
// 塞进"动作摘要"那一列里，于是分支就退化了。
//
// 这里直接合成单一返回值，但保留 scene 形参（execution-view 调用点已经传
// 进来，删形参会让 view 那边的 group 循环代码看起来"丢了 scene 信息"）。
// 形参标注为下划线前缀以提示静态扫描工具它故意未使用，但仍是 API 的一部分：
// 如果未来再次出现"现货/合约要分列"的需求，把分支还原回来即可。
export function orderTableHeaders(_scene) {
  return ["标的 / 场景", "动作摘要", "委托状态", "记录时间", "操作"];
}

export function fillTableHeaders(_scene) {
  return ["标的 / 场景", "成交摘要", "影响摘要", "记录时间", "操作"];
}

export function decisionTableHeaders(_scene) {
  return ["记录时间", "标的 / 周期", "结论摘要", "门禁状态", "操作"];
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
  const fee = fillFeeText(fill);
  if (inferTradeScene(fill) === "derivatives") {
    return fee || "手续费待同步";
  }
  return `成交额 ${formatQuoteNotional(fill.symbol, fill.fill_qty, fill.fill_price)} | ${fee || "手续费待同步"}`;
}

export function normalizedFillFeeImpact(fill = {}) {
  const feeAmount = Number(fill?.fee ?? fill?.fee_amount);
  if (Number.isFinite(feeAmount)) return feeAmount === 0 ? 0 : -feeAmount;

  const feeQuoteAmount = Number(fill?.fee_quote_amount);
  if (Number.isFinite(feeQuoteAmount)) return feeQuoteAmount === 0 ? 0 : -feeQuoteAmount;

  const feeDelta = Number(fill?.fee_delta);
  if (Number.isFinite(feeDelta)) return feeDelta === 0 ? 0 : -feeDelta;

  return null;
}

export function fillFeeText(fill = {}, { includeCurrency = true, fallback = "手续费待同步" } = {}) {
  const feeImpact = normalizedFillFeeImpact(fill);
  if (!Number.isFinite(feeImpact)) return fallback;
  const amountText = formatSigned(feeImpact, 4, "待同步");
  const feeCurrency = includeCurrency ? String(fill?.fee_currency || "").trim() : "";
  return `手续费 ${amountText}${feeCurrency ? ` ${feeCurrency}` : ""}`;
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
      ["已实现盈亏", formatSigned(fill.realized_pnl), fillFeeText(fill)],
    ];
  }

  return [
    ["现货标的", fill.symbol || "标的待确认", `${readableState(fill.side, "买卖方向待确认")} | ${readableState(fill.execution_action || fill.position_intent, "成交意图待确认")}`],
    ["成交数量", formatAssetAmount(fill.symbol, fill.fill_qty), `成交单价 ${formatQuotePrice(fill.symbol, fill.fill_price)}`],
    ["成交金额", formatQuoteNotional(fill.symbol, fill.fill_qty, fill.fill_price), fillFeeText(fill)],
    ["盈亏影响", formatSigned(fill.realized_pnl), readableState(fill.liquidity_role, "流动性角色待确认")],
  ];
}

export function decisionDrawerRows(detail = {}, describeDecisionIntent) {
  const target = detail.position_target || {};
  const productType = inferTradeScene(target.product_type ? target : detail.decision_context || {});
  const intent = typeof describeDecisionIntent === "function" ? describeDecisionIntent(detail) : readableState(target.position_intent || "hold");
  const sizingBreakdown = target.sizing_breakdown || target.sizingBreakdown || null;
  const noOrderTarget = isNoOrderTarget(detail);
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
      ? [
        ["保证金模式", readableState(target.margin_mode || detail.decision_context?.margin_mode, "保证金模式待确认"), `目标杠杆 ${formatNumber(target.target_leverage)} 倍`],
        ...(noOrderTarget
          ? [[
            "本轮下单规模",
            "无新增订单",
            `目标仓位 ${formatSigned(target.target_position_qty, 6, "0")} / 仓位变化 ${formatSigned(target.delta_position_qty, 6, "0")}`,
          ]]
          : sizingBreakdown
          ? [[
            "下单规模分解",
            sizingBreakdownSummary(sizingBreakdown),
            sizingBreakdownMeta(sizingBreakdown),
          ]]
          : []),
      ]
      : [["交易场景", "现货", "现金买卖，不使用杠杆"]];
  return [
    ...commonRows,
    ...sceneRows,
    ["策略门禁", gateStateText(detail.policy_decision?.execution_allowed), gateMetaText(detail.policy_decision?.blocker_reasons, noOrderTarget, "当前没有额外门禁说明", "策略门禁未阻断，但本轮没有下单意图")],
    ["风控结论", gateStateText(detail.risk_decision?.approved), gateMetaText(detail.risk_decision?.rejection_reasons, noOrderTarget, "当前没有额外风控说明", "风控未阻断，但本轮没有下单意图")],
  ];
}

function isNoOrderTarget(detail = {}) {
  const target = detail.position_target || {};
  const rawIntent = String(target.position_intent || "hold").toLowerCase();
  const targetQty = Number(target.target_position_qty ?? 0);
  const deltaQty = Number(target.delta_position_qty ?? 0);
  const orderCount = Array.isArray(detail.order_intents) ? detail.order_intents.length : 0;
  return rawIntent === "hold" && targetQty === 0 && deltaQty === 0 && orderCount === 0;
}

function gateStateText(value) {
  return value ? "未阻断" : readableState("blocked");
}

function gateMetaText(reasons, noOrderTarget, fallback, noOrderCopy) {
  const reasonText = listOrDash(reasons, fallback);
  if (!noOrderTarget || reasonText !== fallback) return reasonText;
  return noOrderCopy;
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

function sizingBreakdownSummary(sizingBreakdown = {}) {
  const mode = String(sizingBreakdown.sizing_mode || sizingBreakdown.sizingMode || "").trim().toLowerCase();
  const modeText = mode === "balance_aware" ? "余额感知" : "固定下单量";
  return [
    modeText,
    `可用权益 ${formatNumber(sizingBreakdown.available_equity ?? sizingBreakdown.availableEquity, 4, "待确认")}`,
    `价格 ${formatNumber(sizingBreakdown.last_price ?? sizingBreakdown.lastPrice, 4, "待确认")}`,
    `目标 ${formatSigned(sizingBreakdown.resolved_target_qty ?? sizingBreakdown.resolvedTargetQty, 6, "待确认")}`,
  ].join(" | ");
}

function sizingBreakdownMeta(sizingBreakdown = {}) {
  return [
    `保证金占用比例 ${formatNumber(sizingBreakdown.margin_usage_fraction ?? sizingBreakdown.marginUsageFraction, 4, "待确认")}`,
    `杠杆 ${formatNumber(sizingBreakdown.target_leverage ?? sizingBreakdown.targetLeverage, 2, "待确认")} 倍`,
    `基准 ${formatSigned(sizingBreakdown.legacy_reference_qty ?? sizingBreakdown.legacyReferenceQty, 6, "待确认")} -> ${formatSigned(sizingBreakdown.resolved_reference_qty ?? sizingBreakdown.resolvedReferenceQty, 6, "待确认")}`,
  ].join(" | ");
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
