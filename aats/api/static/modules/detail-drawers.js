import { kvList, surfaceCard } from "./components.js";
import { meaningfulEntries, stateOrFallback } from "./copy.js";
import {
  booleanWord,
  emptyState,
  escapeHtml,
  formatMaybeTimestamp,
  formatNumber,
  formatRelativeAge,
  listOrDash,
  rawJson,
} from "./formatters.js";
import { localizeError, readableState } from "./terms.js";
import {
  decisionDrawerRows,
  fillDrawerRows,
  fillSceneSummary,
  orderDrawerRows,
  orderSceneSummary,
} from "./trade-display.js";
import { reconciliationActionCopy, renderReconciliationControls } from "./views/risk-view.js";

const EXECUTION_SUGGESTION_LABELS = {
  passive_bias: "被动倾向",
  maker_taker_bias: "主被动偏向",
  slice_count: "拆单数",
  max_participation_rate: "最大参与率",
  max_cross_spread_bps: "最大跨价差",
  cancel_replace_patience_ms: "撤改单等待",
};

export function buildDecisionDrawer(detail) {
  const aiEconomic = detail.ai_economic_actionability || null;
  const aiDecisionAudit = detail.ai_decision_audit || null;
  const aiExecutionSuggestion = detail.ai_execution_suggestion || null;
  const decisionOutcome = detail.decision_outcome || null;

  return {
    eyebrow: "决策链详情",
    title: detail.decision_id ? `当前记录：${detail.decision_id}` : "当前记录：决策链详情",
    summary: strategySummary(detail),
    body: [
      surfaceCard({
        title: "决策摘要",
        content: kvList(decisionDrawerRows(detail, describeDecisionIntent)),
      }),
      surfaceCard({
        title: "交易解释",
        content: `<div class="callout"><p>${escapeHtml(strategySummary(detail))}</p></div>`,
      }),
      aiEconomic
        ? surfaceCard({
            title: "AI 经济可行性",
            content: kvList(decisionEconomicRows(aiEconomic, decisionOutcome)),
          })
        : "",
      aiDecisionAudit
        ? surfaceCard({
            title: "AI 决策审计链",
            content: kvList(decisionAuditRows(aiDecisionAudit, decisionOutcome)),
          })
        : "",
      aiExecutionSuggestion
        ? surfaceCard({
            title: "AI 受限执行建议",
            content: kvList(decisionExecutionRows(aiExecutionSuggestion)),
          })
        : "",
      surfaceCard({
        title: "原始记录",
        content: rawJson(detail),
      }),
    ].join(""),
  };
}

export function buildOrderDrawer(detail) {
  const order = detail.order || {};
  const fills = detail.fills || [];
  return {
    eyebrow: `${orderSceneSummary(order)}详情`,
    title: order.client_order_id ? `当前记录：${order.client_order_id}` : `当前记录：${orderSceneSummary(order)}详情`,
    summary: `这笔${orderSceneSummary(order)}当前状态：${readableState(order.status)}。${fills.length ? ` 已关联 ${fills.length} 笔成交。` : " 目前还没有关联成交。"}`,
    body: [
      surfaceCard({
        title: `${orderSceneSummary(order)}摘要`,
        content: kvList([
          ...orderDrawerRows(order),
          ["最后更新时间", formatMaybeTimestamp(order.last_update_ts || order.created_at), formatRelativeAge(order.last_update_ts || order.created_at)],
        ]),
      }),
      surfaceCard({
        title: "关联成交",
        content: fills.length
          ? kvList(
              fills.map((fill) => [
                fill.fill_id || "成交编号待同步",
                `${formatNumber(fill.fill_qty)} @ ${formatNumber(fill.fill_price)}`,
                `${readableState(fill.side)} | 手续费 ${formatNumber(fill.fee_amount)} ${fill.fee_currency || ""}`,
              ]),
            )
          : emptyState("这笔委托暂时还没有对应成交。"),
      }),
      surfaceCard({
        title: "原始记录",
        content: rawJson(detail),
      }),
    ].join(""),
  };
}

export function buildFillDrawer(detail) {
  const fill = detail.fill || {};
  return {
    eyebrow: `${fillSceneSummary(fill)}详情`,
    title: fill.fill_id ? `当前记录：${fill.fill_id}` : `当前记录：${fillSceneSummary(fill)}详情`,
    summary: `这笔${fillSceneSummary(fill)}是 ${readableState(fill.side)} ${formatNumber(fill.fill_qty)}，成交价 ${formatNumber(fill.fill_price)}。`,
    body: [
      surfaceCard({
        title: `${fillSceneSummary(fill)}摘要`,
        content: kvList([
          ...fillDrawerRows(fill),
          ["落库时间", formatMaybeTimestamp(fill.ingestion_timestamp), formatRelativeAge(fill.ingestion_timestamp)],
        ]),
      }),
      surfaceCard({
        title: "原始记录",
        content: rawJson(detail),
      }),
    ].join(""),
  };
}

export function buildReconciliationDrawer(detail, { recovery = {}, latestReconciliationId = "", uiHints = {} } = {}) {
  const reconciliation = detail.reconciliation || {};
  const billsSummary = detail.exchange_bills_summary || {};
  const billsExplanations = Array.isArray(detail.exchange_bills_explanations) ? detail.exchange_bills_explanations : [];
  const isHistorical = Boolean(latestReconciliationId) && latestReconciliationId !== reconciliation.reconciliation_id;
  return {
    eyebrow: "对账详情",
    title: reconciliation.reconciliation_id ? `当前记录：${reconciliation.reconciliation_id}` : "当前记录：对账详情",
    summary: `这次对账结论是 ${readableState(reconciliation.severity)}。${reconciliation.halt_required ? " 系统已经要求先暂停自动交易。" : ""}`,
    body: [
      surfaceCard({
        title: "核对摘要",
        content: kvList([
          ["核对级别", readableState(reconciliation.severity), reconciliation.exchange_comparison_enabled ? "已对比交易所状态" : "只校验本地记录"],
          ["是否要求暂停", booleanWord(reconciliation.halt_required), booleanWord(reconciliation.review_required)],
          ["差异原因", drawerListText(detail.mismatch_summary?.mismatch_reasons, "当前没有额外差异原因"), drawerListText(detail.mismatch_summary?.mismatch_categories, "当前没有额外差异分类")],
          ["建议动作", detail.mismatch_summary?.recommended_operator_action ? localizeError(detail.mismatch_summary.recommended_operator_action) : "当前没有额外建议动作", drawerListText(detail.mismatch_summary?.safety_impacts, "当前没有额外安全影响说明")],
          ["核对时间", formatMaybeTimestamp(reconciliation.as_of_ts), formatRelativeAge(reconciliation.as_of_ts)],
        ]),
      }),
      surfaceCard({
        title: "账单解释链",
        content: kvList([
          ["最近账单数量", formatNumber(billsSummary.count || 0), drawerText(billsSummary.latest_bill_id, "当前暂无最新账单编号")],
          ["涉及币种", drawerListText(billsSummary.currencies, "当前没有账单币种摘要"), "最近交易所侧账务变动范围"],
          ["高频账务类别", renderReconciliationBillsCategories(billsSummary.top_categories), "已按类型、子类型和币种聚合"],
          ["可能解释当前差异", renderReconciliationBillExplanations(billsExplanations), billsExplanations.length ? "这些账务事件更可能解释余额、仓位或执行偏差" : "当前没有明确的账单解释链"],
          ["识别类型", renderReconciliationBillCases(billsExplanations), billsExplanations.length ? "系统按账单语义和对账差异归纳出的处理场景" : "当前没有可归类的账单处理场景"],
          ["建议处理", renderReconciliationBillActions(billsExplanations), billsExplanations.length ? "这是给操作员的下一步动作建议，不会直接改动交易所账单" : "当前没有额外账单处理建议"],
        ]),
      }),
      surfaceCard({
        title: "可执行操作",
        content: `
          <p class="meta-copy">${escapeHtml(reconciliationActionCopy({ reconciliation, recovery, isHistorical }))}</p>
          ${renderReconciliationControls({ reconciliation, recovery, uiHints, compact: true })}
        `,
      }),
      surfaceCard({
        title: "原始记录",
        content: rawJson(detail),
      }),
    ].join(""),
  };
}

function decisionEconomicRows(aiEconomic, decisionOutcome) {
  return [
    ["这轮值不值得让 AI 直接接管", booleanWord(aiEconomic.economically_actionable), `最低净优势要求 ${formatNumber(aiEconomic.min_required_net_edge_bps ?? 0, 2)} 个基点`],
    ["本轮预估边际", `${formatNumber(aiEconomic.estimated_edge_bps ?? 0, 2)} 个基点`, `成本 ${formatNumber(aiEconomic.estimated_cost_bps ?? 0, 2)} / 净优势 ${formatNumber(aiEconomic.estimated_net_edge_bps ?? 0, 2)} 个基点`],
    ["目标动作预估边际", `${formatNumber(aiEconomic.target_expected_signal_edge_bps ?? 0, 2)} 个基点`, `成本 ${formatNumber(aiEconomic.target_expected_cost_bps ?? 0, 2)} / 净优势 ${formatNumber(aiEconomic.target_expected_net_edge_bps ?? 0, 2)} 个基点`],
    ["最终门槛", `${formatNumber(aiEconomic.required_total_edge_bps ?? 0, 2)} 个基点`, `噪声缓冲 ${formatNumber(aiEconomic.noise_buffer_bps ?? 0, 2)} / 最低净优势 ${formatNumber(aiEconomic.min_required_net_edge_bps ?? 0, 2)} 个基点`],
    ["这轮最终采用谁的结论", decisionSourceLabel(decisionOutcome?.decision_source), decisionSourceNarrative(decisionOutcome)],
    ["行情和账户状态", `行情快照 ${booleanWord(aiEconomic.market_snapshot_fresh)} / 账户快照 ${booleanWord(aiEconomic.account_snapshot_fresh)}`, `允许交易 ${booleanWord(aiEconomic.safe_to_trade)} / ${drawerText(aiEconomic.execution_condition, "当前没有额外执行条件")}`],
    ["最近执行健康度", `手续费拖累 ${formatNumber(aiEconomic.recent_fee_drag_ratio ?? 0, 3)} / 来回交易 ${formatNumber(aiEconomic.recent_churn_ratio ?? 0, 3)}`, `低边际连续次数 ${formatNumber(aiEconomic.recent_low_edge_trade_streak ?? 0, 0)} / 活动委托 ${formatNumber(aiEconomic.current_open_order_count ?? 0, 0)}`],
    ["校验与拦截标记", drawerListText(aiEconomic.validation_flags, "当前没有额外校验标记"), drawerListText(aiEconomic.rejection_flags, "当前没有额外拦截说明")],
  ];
}

function decisionAuditRows(aiDecisionAudit, decisionOutcome) {
  return [
    ["当前运行模式与评估方式", `${readableState(aiDecisionAudit.configured_mode || "unknown")} / ${readableState(aiDecisionAudit.assessment_operating_mode || "unknown")}`, drawerText(aiDecisionAudit.provider_name, "当前没有模型服务说明")],
    ["方向判断对比", `基础策略 ${readableState(aiDecisionAudit.baseline_direction || "unknown")} / AI ${readableState(aiDecisionAudit.ai_direction || "unknown")}`, `最终结论 ${readableState(aiDecisionAudit.final_direction || "unknown")}`],
    ["这轮最终采用谁的结论", decisionSourceLabel(aiDecisionAudit.decision_source), `${decisionSourceNarrative(decisionOutcome)} / ${decisionAuthorityLabel(aiDecisionAudit.decision_authority)}`],
    ["为什么没有直接采用 AI", drawerListText((decisionOutcome?.decision_blocked_reasons || []).map(localizeError), "当前没有额外决策链路阻断项"), drawerListText(aiDecisionAudit.guardrail_flags, "当前没有额外保护规则")],
    ["行情和账户状态", `行情快照 ${booleanWord(aiDecisionAudit.market_snapshot_fresh)} / 账户快照 ${booleanWord(aiDecisionAudit.account_snapshot_fresh)}`, `允许交易 ${booleanWord(aiDecisionAudit.safe_to_trade)} / ${drawerText(aiDecisionAudit.execution_condition, "当前没有额外执行条件")}`],
    ["最近执行健康度", `手续费拖累 ${formatNumber(aiDecisionAudit.recent_fee_drag_ratio ?? 0, 3)} / 来回交易 ${formatNumber(aiDecisionAudit.recent_churn_ratio ?? 0, 3)}`, `低边际连续次数 ${formatNumber(aiDecisionAudit.recent_low_edge_trade_streak ?? 0, 0)} / 活动委托 ${formatNumber(aiDecisionAudit.current_open_order_count ?? 0, 0)}`],
  ];
}

function decisionExecutionRows(aiExecutionSuggestion) {
  return [
    ["建议模式", readableState(aiExecutionSuggestion.configured_mode || "disabled"), aiExecutionSuggestion.translation_present ? "已有翻译结果" : aiExecutionSuggestion.suggestion_present ? "已有建议结果" : "最近没有生成新建议"],
    ["翻译器状态", readableState(aiExecutionSuggestion.status || "absent"), aiExecutionSuggestion.latest_translation?.applied_to_live_execution ? "已进入真实执行" : "当前不会改写真实执行"],
    ["实盘应用", booleanWord(aiExecutionSuggestion.latest_translation?.applied_to_live_execution), aiExecutionSuggestion.latest_translation?.applied_to_live_execution ? drawerListText(aiExecutionSuggestion.latest_translation?.applied_live_fields, "当前没有额外实盘字段") : drawerListText([aiExecutionSuggestion.latest_translation?.live_translation_fallback_reason], "当前没有进入实盘应用")],
    ["建议执行姿态", suggestionSummaryText(aiExecutionSuggestion.assessment_suggestion?.suggestion), drawerListText(aiExecutionSuggestion.latest_translation?.clipped_fields?.map(localizeError), "当前没有裁剪字段")],
    ["翻译预览", translationPreviewSummaryText(aiExecutionSuggestion.latest_translation?.translation_preview), drawerListText((aiExecutionSuggestion.latest_translation?.rejection_reasons || []).map(localizeError), "当前没有拒绝原因")],
    ["实盘字段", liveExecutionSummaryText(aiExecutionSuggestion), stateOrFallback(aiExecutionSuggestion.live_execution_style, "当前没有执行风格说明")],
  ];
}

function decisionSourceLabel(value) {
  const source = String(value || "baseline").toLowerCase();
  if (source === "ai") return "本轮最终采用 AI 结论";
  if (source === "baseline_fallback") return "本轮最终回退到基础策略";
  if (source === "admin_override") return "本轮由管理员手动覆盖";
  return "本轮继续沿用基础策略";
}

function decisionAuthorityLabel(value) {
  const authority = String(value || "reference_only").toLowerCase();
  if (authority === "final_decision_with_profile_control") return "当前模式允许 AI 最终决策并联动档位控制";
  if (authority === "final_decision") return "当前模式允许 AI 直接给最终结论";
  if (authority === "advisory") return "当前模式只把 AI 当辅助建议";
  return "当前模式只把 AI 当参考";
}

function decisionSourceNarrative(decisionOutcome = null) {
  const source = String(decisionOutcome?.decision_source || "baseline").toLowerCase();
  if (source === "ai") {
    return "虽然系统会继续检查风控和执行条件，但这轮最终结论已经直接采用了 AI。";
  }
  if (source === "baseline_fallback") {
    return `当前运行模式允许 AI 参与，但这轮 AI 没有通过最终门槛，所以系统改为采用基础策略。${drawerListText((decisionOutcome?.decision_blocked_reasons || []).map(localizeError), "当前没有额外阻断说明")}`;
  }
  if (source === "admin_override") {
    return "这轮最终结论来自管理员手动覆盖，而不是系统自动采用 AI 或基础策略。";
  }
  return "这轮没有采用 AI 的最终结论，系统继续沿用基础策略。";
}

function renderReconciliationBillsCategories(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return "当前没有账单分类";
  return rows
    .slice(0, 4)
    .map((item) => `${item.human_label || `${item.type}/${item.sub_type}`}${item.count ? ` x${formatNumber(item.count)}` : ""}`)
    .join(" | ");
}

function renderReconciliationBillExplanations(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return "当前没有账单解释";
  return rows
    .slice(0, 3)
    .map((item) => `${item.title}: ${drawerListText(item.likely_explains, "当前没有额外解释")}`)
    .join(" | ");
}

function renderReconciliationBillCases(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return "当前没有账单处理分类";
  return rows
    .slice(0, 3)
    .map((item) => `${item.title}: ${localizeError(item.operator_case || "manual_activity")}`)
    .join(" | ");
}

function renderReconciliationBillActions(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return "当前没有账单处理建议";
  return rows
    .slice(0, 3)
    .map((item) => `${item.title}: ${localizeError(item.operator_action || "observe_only")}`)
    .join(" | ");
}

function suggestionSummaryText(value) {
  const entries = meaningfulEntries(value).filter(([, item]) => {
    if (typeof item === "number") return item !== 0;
    return true;
  });
  if (!entries.length) return "当前没有新的执行姿态建议";
  return entries
    .slice(0, 4)
    .map(([key, item]) => `${executionSuggestionLabel(key)} ${formatNumber(item ?? 0, 2)}`)
    .join(" / ");
}

function executionSuggestionLabel(key) {
  const normalized = String(key || "").trim();
  return EXECUTION_SUGGESTION_LABELS[normalized] || normalized;
}

function translationPreviewSummaryText(value) {
  const orderType = value?.order_type;
  const executionStyle = value?.execution_style;
  if (!orderType && !executionStyle) return "当前没有新的翻译预览";
  const styleText = stateOrFallback(executionStyle, "执行风格待确认");
  const typeText = stateOrFallback(orderType, "订单类型待确认");
  const offsetText = value?.limit_offset_bps === null || value?.limit_offset_bps === undefined
    ? "当前没有价格偏移说明"
    : `价格偏移 ${formatNumber(value.limit_offset_bps, 2)} 个基点`;
  return `${styleText} / ${typeText} / ${offsetText}`;
}

function liveExecutionSummaryText(value) {
  const hasLiveField = value?.live_order_type || value?.live_time_in_force || value?.live_limit_price !== null && value?.live_limit_price !== undefined;
  if (!hasLiveField) return "当前没有新的实盘下探字段";
  return `订单类型 ${drawerText(value.live_order_type, "待确认")} / 时效 ${drawerText(value.live_time_in_force, "待确认")} / 限价 ${formatNumber(value.live_limit_price ?? 0, 2)}`;
}

function drawerText(value, fallback = "待确认") {
  if (value === null || value === undefined) return fallback;
  const text = String(value).trim();
  return text ? text : fallback;
}

function drawerListText(value, fallback = "当前没有额外说明") {
  if (Array.isArray(value)) {
    const filtered = value.map((item) => String(item ?? "").trim()).filter(Boolean);
    return filtered.length ? filtered.join(" / ") : fallback;
  }
  return drawerText(value, fallback);
}

function strategySummary(detail) {
  const policy = detail.policy_decision || {};
  const risk = detail.risk_decision || {};
  if (!detail.decision_id) return "当前暂无新的策略详情。";
  return `系统当前对 ${detail.decision_context?.symbol || "当前标的"} 的交易结论是 ${describeDecisionIntent(detail)}。`
    + `${policy.execution_allowed ? "策略门禁已通过，" : "策略门禁未通过，"}`
    + `${risk.approved ? "风控当前没有继续阻断。" : `风控仍在拦截：${listOrDash(risk.rejection_reasons)}。`}`;
}

function describeDecisionIntent(detail) {
  const target = detail.position_target || {};
  const rawIntent = String(target.position_intent || "hold").toLowerCase();
  const currentQty = Number(target.current_position_qty ?? detail.decision_context?.current_position_qty ?? 0);
  const targetQty = Number(target.target_position_qty ?? 0);
  const openOrders = Array.isArray(detail.decision_context?.current_open_orders) ? detail.decision_context.current_open_orders : [];
  if (rawIntent === "hold" && currentQty === 0 && targetQty === 0 && openOrders.length === 0) {
    return "继续观望";
  }
  return readableState(rawIntent);
}
