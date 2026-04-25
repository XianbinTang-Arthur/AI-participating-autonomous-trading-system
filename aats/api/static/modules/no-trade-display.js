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
    ["双书证据", summarizeBookStates(payload.book_runtime_states), "按 long / short 原生账本状态摘要。"],
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
