import { formatNumber } from "./formatters.js";
import { localizeError, readableState } from "./terms.js";

const CLASSIFICATION_COPY = {
  execution_activity_present: {
    label: "已有执行活动",
    copy: "这条记录已经关联订单或成交，不应按无交易样本解释。",
    tone: "info",
  },
  policy_execution_block: {
    label: "策略门禁阻断",
    copy: "本轮不是执行链丢单，而是策略或人工门禁明确阻止继续提交。",
    tone: "danger",
  },
  risk_execution_block: {
    label: "风控阻断",
    copy: "本轮不是执行链丢单，而是风控层明确阻止继续提交。",
    tone: "danger",
  },
  actionable_decision_missing_execution_activity: {
    label: "执行链缺口",
    copy: "本轮存在可执行目标，但没有看到订单或成交，需要按运行/执行链缺口排查。",
    tone: "danger",
  },
  no_executable_independent_legs_due_signal_and_net_edge_gates: {
    label: "双书信号与净边际不足",
    copy: "独立双书没有形成可执行腿，原因同时包含信号未达阈值和扣除成本后的净边际不足。",
    tone: "warning",
  },
  no_executable_independent_legs_due_net_edge_gate: {
    label: "双书净边际不足",
    copy: "独立双书有候选信号，但扣除执行成本后的净边际没有达到安全门槛。",
    tone: "warning",
  },
  no_executable_independent_legs_due_signal_threshold: {
    label: "双书信号未达阈值",
    copy: "独立双书的 long/short 账本分数没有达到开仓阈值，所以本轮继续观察。",
    tone: "warning",
  },
  no_executable_independent_candidate_inactive: {
    label: "独立双书候选未激活",
    copy: "独立双书路径没有激活可执行候选，所以本轮没有生成订单目标。",
    tone: "warning",
  },
  strategy_no_trade_reason_codes_present: {
    label: "策略原因阻断",
    copy: "策略层给出了无交易原因码，本轮没有形成可提交的执行目标。",
    tone: "warning",
  },
  unknown_no_trade_blocker: {
    label: "无交易原因待确认",
    copy: "这是一条无订单/无成交记录，但当前证据还不足以稳定归类。",
    tone: "warning",
  },
};

const SCOPE_LABELS = {
  execution_activity: "已有执行活动",
  policy_gate: "策略门禁",
  risk_gate: "风控门禁",
  runtime_execution_gap: "运行/执行链缺口",
  strategy_signal_or_net_edge_gate: "策略信号或净边际门禁",
  strategy_gate: "策略门禁",
  unknown: "待确认",
};

const FEASIBILITY_DIMENSION_LABELS = {
  signal_threshold: "信号阈值",
  net_edge: "净边际",
  cost: "成本证据",
  book_state: "盘口/账本状态",
  liquidity: "盘口/流动性",
  policy_gate: "策略门禁",
  risk_gate: "风控门禁",
};

const FEASIBILITY_STATUS_LABELS = {
  blocked: "阻断",
  mixed: "部分阻断",
  passed: "通过",
  observed: "已记录",
  unavailable: "证据缺失",
  execution_activity_present: "已有执行活动",
};

export function extractNoTradeClassification(source = {}) {
  const direct = source?.no_trade_classification;
  if (direct && typeof direct === "object") return direct;
  const outcome = source?.decision_outcome?.no_trade_classification;
  if (outcome && typeof outcome === "object") return outcome;
  return null;
}

export function hasNoTradeClassification(payload) {
  return Boolean(payload && typeof payload === "object" && payload.classification);
}

export function noTradeClassificationLabel(payload) {
  if (!hasNoTradeClassification(payload)) return "无交易原因待确认";
  return CLASSIFICATION_COPY[payload.classification]?.label || readableState(payload.classification, payload.classification);
}

export function noTradeClassificationTone(payload) {
  if (!hasNoTradeClassification(payload)) return "warning";
  return CLASSIFICATION_COPY[payload.classification]?.tone || "warning";
}

export function noTradeClassificationCopy(payload) {
  if (!hasNoTradeClassification(payload)) return "当前没有稳定的无交易分类。";
  return CLASSIFICATION_COPY[payload.classification]?.copy || "本轮没有形成可提交的执行目标。";
}

export function noTradeScopeLabel(payload) {
  const scope = String(payload?.scope || "unknown").trim();
  return SCOPE_LABELS[scope] || readableState(scope, scope || "待确认");
}

export function noTradeEvidenceSummary(payload) {
  if (!payload || typeof payload !== "object") return "当前没有额外证据。";
  const reasons = collectReasonCodes(payload);
  if (reasons.length) return reasons.slice(0, 4).map((item) => localizeError(item, item)).join(" / ");
  const books = Array.isArray(payload.book_runtime_states) ? payload.book_runtime_states : [];
  if (books.length) return summarizeBookStates(books);
  return payload.policy_risk_active_blocker ? "存在策略或风控 active blocker。" : "策略/风控没有 active blocker 证据。";
}

export function noTradeClassificationMeta(payload) {
  if (!payload || typeof payload !== "object") return "当前没有额外分类元数据。";
  const parts = [
    `范围 ${noTradeScopeLabel(payload)}`,
    payload.strategy_family ? `家族 ${readableState(payload.strategy_family, payload.strategy_family)}` : null,
    `订单 ${formatNumber(payload.order_count || 0, 0, "0")}`,
    `成交 ${formatNumber(payload.fill_count || 0, 0, "0")}`,
  ].filter(Boolean);
  return parts.join(" | ");
}

export function noTradeClassificationRows(payload) {
  if (!hasNoTradeClassification(payload)) return [];
  return [
    ["无交易分类", noTradeClassificationLabel(payload), noTradeClassificationMeta(payload)],
    ["为什么没有下单", noTradeClassificationCopy(payload), noTradeEvidenceSummary(payload)],
    [
      "策略/风控 active blocker",
      payload.policy_risk_active_blocker ? "有" : "无",
      payload.policy_risk_active_blocker
        ? policyRiskReasonSummary(payload)
        : "当前分类没有把这轮无交易归因到策略/风控 active blocker。",
    ],
    ...preOrderFeasibilityRows(payload.pre_order_feasibility),
    ["双书证据", summarizeBookStates(payload.book_runtime_states), "按 long / short 原生账本状态摘要。"],
  ];
}

export function preOrderFeasibilityRows(feasibility) {
  if (!feasibility || typeof feasibility !== "object") {
    return [["执行可行性", "证据缺失", "当前无交易分类没有携带 pre-order 可行性证据。"]];
  }
  const dimensions = feasibility.dimensions && typeof feasibility.dimensions === "object" ? feasibility.dimensions : {};
  const orderedKeys = ["signal_threshold", "net_edge", "cost", "book_state", "liquidity", "policy_gate", "risk_gate"];
  return [
    [
      "执行可行性",
      feasibilityStatusLabel(feasibility.status),
      feasibility.blocked_dimensions?.length
        ? `阻断维度：${feasibility.blocked_dimensions.map((key) => FEASIBILITY_DIMENSION_LABELS[key] || readableState(key, key)).join(" / ")}`
        : "当前没有记录到阻断维度；若证据缺失，不推断为可执行。",
    ],
    ...orderedKeys
      .filter((key) => dimensions[key])
      .map((key) => [
        FEASIBILITY_DIMENSION_LABELS[key] || readableState(key, key),
        feasibilityStatusLabel(dimensions[key]?.status),
        feasibilityDimensionSummary(key, dimensions[key]),
      ]),
  ];
}

function collectReasonCodes(payload) {
  const values = [
    ...(payload.reason_codes || []),
    ...(payload.policy_reasons || []),
    ...(payload.risk_rejection_reasons || []),
    ...(payload.risk_constraints_applied || []),
  ];
  return Array.from(new Set(values.map((item) => String(item || "").trim()).filter(Boolean)));
}

function policyRiskReasonSummary(payload) {
  const reasons = collectReasonCodes(payload);
  return reasons.length ? reasons.slice(0, 4).map((item) => localizeError(item, item)).join(" / ") : "当前没有额外策略/风控原因码。";
}

function feasibilityStatusLabel(status) {
  const normalized = String(status || "unavailable").trim();
  return FEASIBILITY_STATUS_LABELS[normalized] || readableState(normalized, normalized || "证据缺失");
}

function feasibilityDimensionSummary(key, dimension) {
  if (!dimension || typeof dimension !== "object") return "证据缺失。";
  const legs = Array.isArray(dimension.legs) ? dimension.legs : [];
  if (legs.length) {
    return summarizeFeasibilityLegs(key, legs);
  }
  const reasons = Array.isArray(dimension.reason_codes) ? dimension.reason_codes : [];
  if (reasons.length) {
    return reasons.slice(0, 4).map((item) => localizeError(item, item)).join(" / ");
  }
  return dimension.evidence_available ? "已有门禁状态证据。" : "当前没有记录该维度证据。";
}

function summarizeFeasibilityLegs(key, legs) {
  return legs
    .slice(0, 3)
    .map((leg) => {
      const base = `${readableState(leg?.leg, leg?.leg || "账本")} ${feasibilityStatusLabel(leg?.status)}`;
      if (key === "signal_threshold") {
        const score = leg?.score === null || leg?.score === undefined ? "分数缺失" : `分数 ${formatNumber(leg.score, 3)}`;
        const threshold = leg?.entry_threshold === null || leg?.entry_threshold === undefined ? "阈值缺失" : `阈值 ${formatNumber(leg.entry_threshold, 3)}`;
        return `${base}，${score} / ${threshold}`;
      }
      if (key === "net_edge") {
        const net = leg?.expected_net_edge_bps === null || leg?.expected_net_edge_bps === undefined
          ? "净边际缺失"
          : `净边际 ${formatNumber(leg.expected_net_edge_bps, 2)}bp`;
        const required = leg?.required_safe_net_edge_bps === null || leg?.required_safe_net_edge_bps === undefined
          ? "安全门槛缺失"
          : `安全门槛 ${formatNumber(leg.required_safe_net_edge_bps, 2)}bp`;
        const cost = leg?.expected_cost_bps === null || leg?.expected_cost_bps === undefined
          ? "成本缺失"
          : `成本 ${formatNumber(leg.expected_cost_bps, 2)}bp`;
        return `${base}，${net} / ${required} / ${cost}`;
      }
      if (key === "cost") {
        const cost = leg?.expected_cost_bps === null || leg?.expected_cost_bps === undefined
          ? "成本缺失"
          : `成本 ${formatNumber(leg.expected_cost_bps, 2)}bp`;
        const maxCost = leg?.max_acceptable_cost_bps === null || leg?.max_acceptable_cost_bps === undefined
          ? "最大可接受成本缺失"
          : `最大可接受 ${formatNumber(leg.max_acceptable_cost_bps, 2)}bp`;
        return `${base}，${cost} / ${maxCost}`;
      }
      if (key === "book_state") {
        return `${base}，状态 ${readableState(leg?.state || leg?.book_state, leg?.state || leg?.book_state || "缺失")}`;
      }
      if (key === "liquidity") {
        const score = leg?.liquidity_quality_score === null || leg?.liquidity_quality_score === undefined
          ? "流动性分缺失"
          : `流动性分 ${formatNumber(leg.liquidity_quality_score, 3)}`;
        const health = leg?.execution_health_state ? `执行健康 ${readableState(leg.execution_health_state, leg.execution_health_state)}` : "执行健康缺失";
        return `${base}，${score} / ${health}`;
      }
      return base;
    })
    .join(" | ");
}

function summarizeBookStates(value) {
  const books = Array.isArray(value) ? value : [];
  if (!books.length) return "当前没有双书状态证据。";
  return books
    .slice(0, 3)
    .map((item) => {
      const leg = readableState(item?.leg, item?.leg || "账本");
      const state = readableState(item?.state, item?.state || "待确认");
      const score = item?.score === null || item?.score === undefined ? null : `分数 ${formatNumber(item.score, 3)}`;
      const threshold = item?.entry_threshold === null || item?.entry_threshold === undefined ? null : `阈值 ${formatNumber(item.entry_threshold, 3)}`;
      const net = item?.expected_net_edge_bps === null || item?.expected_net_edge_bps === undefined ? null : `净边际 ${formatNumber(item.expected_net_edge_bps, 2)}bp`;
      const reasons = [...(item?.reason_codes || []), ...(item?.blocking_reasons || [])]
        .slice(0, 2)
        .map((reason) => localizeError(reason, reason))
        .join(" / ");
      return [leg, state, score, threshold, net, reasons || null].filter(Boolean).join(" ");
    })
    .join(" | ");
}
