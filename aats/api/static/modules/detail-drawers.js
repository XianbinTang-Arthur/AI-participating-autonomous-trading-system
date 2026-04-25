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
import {
  localizeError,
  readableBookExpectancySummary,
  readableBookRuntimeStateSummary,
  readableIndependentAdaptiveMeta,
  readableIndependentAdaptiveSummary,
  readableExpectedVsRealizedMeta,
  readableExpectedVsRealizedSummary,
  readableFamilyExecutionDirection,
  readableFamilyExecutionSummary,
  readableOverlayParentLegQuantitySummary,
  readableOverlayParentPostmortemMeta,
  readableOverlayParentSignalSummary,
  readableIndependentTransitionExceptionMeta,
  readableIndependentTransitionExceptionSummary,
  readableState,
} from "./terms.js";
import {
  extractNoTradeClassification,
  hasNoTradeClassification,
  noTradeClassificationCopy,
  noTradeClassificationRows,
} from "./no-trade-display.js";
import {
  decisionDrawerRows,
  executionSuggestionLabel,
  fillFeeText,
  fillDrawerRows,
  fillSceneSummary,
  orderDrawerRows,
  orderSceneSummary,
} from "./trade-display.js";
// #5 修复：reconciliationActionCopy / renderReconciliationControls 原本定义在
// modules/views/risk-view.js 里，detail-drawers 反向 import 过来导致依赖方向
// 倒挂（drawer 是底层 helper，反而依赖 view 模块）。已抽到
// modules/reconciliation-controls.js，risk-view 和这里都从该模块 import。
import { reconciliationActionCopy, renderReconciliationControls } from "./reconciliation-controls.js";

// #32 修复：本地 EXECUTION_SUGGESTION_LABELS 已删除，改为从 trade-display.js import
// 统一的 executionSuggestionLabel()。

export function buildDecisionDrawer(detail) {
  const aiEconomic = detail.ai_economic_actionability || null;
  const aiDecisionAudit = detail.ai_decision_audit || null;
  const aiExecutionSuggestion = detail.ai_execution_suggestion || null;
  const decisionOutcome = detail.decision_outcome || null;
  const noTradeClassification = extractNoTradeClassification(detail);
  const hedgeModeAudit = detail.hedge_mode_audit || null;
  const overlayParentPostmortem =
    aiDecisionAudit?.overlay_parent_exposure_summary
    || hedgeModeAudit?.overlay?.overlay_parent_exposure_summary
    || aiDecisionAudit?.overlay_parent_exposure
    || hedgeModeAudit?.overlay?.overlay_parent_exposure
    || null;

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
      hasNoTradeClassification(noTradeClassification)
        ? surfaceCard({
            title: "无交易原因",
            content: kvList(noTradeClassificationRows(noTradeClassification)),
          })
        : "",
      aiEconomic
        ? surfaceCard({
            title: "AI 经济可行性",
            content: kvList(decisionEconomicRows(aiEconomic, decisionOutcome, detail.ai_assessment)),
          })
        : "",
      aiDecisionAudit
        ? surfaceCard({
            title: "AI 决策审计链",
            content: kvList(decisionAuditRows(aiDecisionAudit, decisionOutcome)),
          })
        : "",
      hasMeaningfulAiExecutionSuggestion(aiExecutionSuggestion)
        ? surfaceCard({
            title: "AI 受限执行建议",
            content: kvList(decisionExecutionRows(aiExecutionSuggestion)),
          })
        : "",
      hedgeModeAudit && shouldRenderHedgeModeAudit(hedgeModeAudit)
        ? surfaceCard({
            title: "对冲模式审计",
            content: kvList(decisionHedgeModeRows(hedgeModeAudit)),
          })
        : "",
      hedgeModeAudit?.overlay && Object.keys(hedgeModeAudit.overlay).length
        ? surfaceCard({
            title: "Overlay 审计",
            content: kvList(decisionOverlayAuditRows(hedgeModeAudit.overlay)),
          })
        : "",
      overlayParentPostmortem && Object.keys(overlayParentPostmortem).length
        ? surfaceCard({
            title: "父腿暴露复盘",
            content: kvList(decisionOverlayParentPostmortemRows(overlayParentPostmortem)),
          })
        : "",
      hedgeModeAudit?.leg_orders?.total_count
        ? surfaceCard({
            title: "腿级订单审计",
            content: kvList(decisionLegOrderRows(hedgeModeAudit.leg_orders)),
          })
        : "",
      hedgeModeAudit?.leg_trial_guard?.total_count
        ? surfaceCard({
            title: "腿级试盘守护",
            content: kvList(decisionLegTrialGuardRows(hedgeModeAudit.leg_trial_guard)),
          })
        : "",
      hedgeModeAudit?.leg_reconciliation?.total_count
        ? surfaceCard({
            title: "腿级对账审计",
            content: kvList(decisionLegReconciliationRows(hedgeModeAudit.leg_reconciliation)),
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
                `${readableState(fill.side)} | ${fillFeeText(fill)}`,
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

function decisionEconomicRows(aiEconomic, decisionOutcome, aiAssessment = null) {
  const rows = [
    ["AI 采纳门槛", aiEconomic.economically_actionable ? "通过" : "未通过", `最低净优势要求 ${formatNumber(aiEconomic.min_required_net_edge_bps ?? 0, 2)} 个基点`],
    ["AI 评估净边际", `${formatNumber(aiEconomic.estimated_net_edge_bps ?? 0, 2)} 个基点`, `信号 ${formatNumber(aiEconomic.estimated_edge_bps ?? 0, 2)} / 成本 ${formatNumber(aiEconomic.estimated_cost_bps ?? 0, 2)} 个基点`],
    ["策略候选净边际", `${formatNumber(aiEconomic.target_expected_net_edge_bps ?? 0, 2)} 个基点`, `信号 ${formatNumber(aiEconomic.target_expected_signal_edge_bps ?? 0, 2)} / ${targetExpectancyDisciplineSummary(aiEconomic)}`],
    ["最终门槛", `${formatNumber(aiEconomic.required_total_edge_bps ?? 0, 2)} 个基点`, `噪声缓冲 ${formatNumber(aiEconomic.noise_buffer_bps ?? 0, 2)} / 最低净优势 ${formatNumber(aiEconomic.min_required_net_edge_bps ?? 0, 2)} 个基点`],
    ["最终采纳路径", decisionSourceLabel(decisionOutcome?.decision_source), decisionSourceNarrative(decisionOutcome)],
    ["行情和账户状态", `行情快照 ${freshnessWord(aiEconomic.market_snapshot_fresh)} / 账户快照 ${freshnessWord(aiEconomic.account_snapshot_fresh)}`, `交易前置 ${aiEconomic.safe_to_trade ? "可交易" : "不可交易"} / ${readableState(aiEconomic.execution_condition, "当前没有额外执行条件")}`],
    ["最近执行健康度", `手续费拖累 ${formatNumber(aiEconomic.recent_fee_drag_ratio ?? 0, 3)} / 来回交易 ${formatNumber(aiEconomic.recent_churn_ratio ?? 0, 3)}`, `低边际连续次数 ${formatNumber(aiEconomic.recent_low_edge_trade_streak ?? 0, 0)} / 活动委托 ${formatNumber(aiEconomic.current_open_order_count ?? 0, 0)}`],
    ["校验与拦截标记", drawerListText(aiEconomic.validation_flags, "当前没有额外校验标记"), drawerListText(aiEconomic.rejection_flags, "当前没有额外拦截说明")],
  ];
  const modelRow = aiModelAuditRow(aiAssessment);
  return modelRow ? [modelRow, ...rows] : rows;
}

function aiModelAuditRow(aiAssessment = null) {
  if (!aiAssessment || typeof aiAssessment !== "object") return null;
  const model = aiAssessment.model_name || aiAssessment.model_version
    ? [aiAssessment.model_name, aiAssessment.model_version ? `v${aiAssessment.model_version}` : null].filter(Boolean).join(" ")
    : drawerText(aiAssessment.provider_name, "");
  const status = aiAssessment.output_valid === true
    ? "输出有效"
    : aiAssessment.output_valid === false
      ? "输出无效"
      : "输出状态待确认";
  const meta = [
    aiAssessment.provider_name ? `服务 ${aiAssessment.provider_name}` : null,
    aiAssessment.provider_latency_ms !== undefined && aiAssessment.provider_latency_ms !== null
      ? `耗时 ${formatLatencyMs(aiAssessment.provider_latency_ms)}`
      : null,
    aiAssessment.prompt_version ? `prompt ${aiAssessment.prompt_version}` : null,
    aiAssessment.fallback_used ? `回退 ${drawerText(aiAssessment.fallback_reason, "原因待确认")}` : null,
  ].filter(Boolean);
  if (!model && !meta.length && status === "输出状态待确认") return null;
  return ["模型响应", model ? `${model} / ${status}` : status, meta.length ? meta.join(" | ") : "当前没有额外模型响应信息"];
}

function formatLatencyMs(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "待确认";
  if (number >= 1000) return `${formatNumber(number / 1000, 2)} 秒`;
  return `${formatNumber(number, 0)} 毫秒`;
}

function freshnessWord(value) {
  if (value === true) return "可用";
  if (value === false) return "不可用";
  return "待确认";
}

function targetExpectancyDisciplineSummary(aiEconomic = {}) {
  const parts = [
    `成本 ${formatNumber(aiEconomic.target_expected_cost_bps ?? 0, 2)} / 净优势 ${formatNumber(aiEconomic.target_expected_net_edge_bps ?? 0, 2)} 个基点`,
  ];
  if (aiEconomic.target_required_safe_net_edge_bps !== undefined && aiEconomic.target_required_safe_net_edge_bps !== null) {
    parts.push(`安全净边际 ${formatNumber(aiEconomic.target_required_safe_net_edge_bps, 2)} 个基点`);
  }
  if (
    aiEconomic.target_max_acceptable_cost_bps !== undefined
    && aiEconomic.target_max_acceptable_cost_bps !== null
    && Number(aiEconomic.target_max_acceptable_cost_bps) > 0
  ) {
    parts.push(`成本上限 ${formatNumber(aiEconomic.target_max_acceptable_cost_bps, 2)} 个基点`);
  }
  if (aiEconomic.target_weak_edge_execution_mode) {
    parts.push(`弱边际 ${readableState(aiEconomic.target_weak_edge_execution_mode, aiEconomic.target_weak_edge_execution_mode)}`);
  }
  if (aiEconomic.target_weak_edge_report_only === true) {
    parts.push("本轮只保留报告");
  }
  if (aiEconomic.target_passive_first_required === true) {
    parts.push("要求被动优先");
  }
  if (aiEconomic.target_book_action) {
    parts.push(`腿动作 ${readableState(aiEconomic.target_book_action, aiEconomic.target_book_action)}`);
  }
  if (aiEconomic.target_close_reason) {
    parts.push(`退出原因 ${localizeError(aiEconomic.target_close_reason, aiEconomic.target_close_reason)}`);
  }
  if (aiEconomic.target_policy_reason) {
    parts.push(`执行策略 ${readableState(aiEconomic.target_policy_reason, aiEconomic.target_policy_reason)}`);
  }
  if (aiEconomic.target_execution_policy_urgency) {
    parts.push(`优先级 ${readableState(aiEconomic.target_execution_policy_urgency, aiEconomic.target_execution_policy_urgency)}`);
  }
  if (
    aiEconomic.target_execution_style_preference
    || aiEconomic.target_order_type_preference
    || aiEconomic.target_time_in_force_preference
  ) {
    const executionParts = [
      aiEconomic.target_execution_style_preference ? readableState(aiEconomic.target_execution_style_preference, aiEconomic.target_execution_style_preference) : null,
      aiEconomic.target_order_type_preference ? readableState(aiEconomic.target_order_type_preference, aiEconomic.target_order_type_preference) : null,
      aiEconomic.target_time_in_force_preference ? readableState(aiEconomic.target_time_in_force_preference, aiEconomic.target_time_in_force_preference) : null,
    ].filter(Boolean);
    if (executionParts.length) {
      parts.push(`执行偏好 ${executionParts.join(" / ")}`);
    }
  }
  return parts.join(" | ");
}

function decisionAuditRows(aiDecisionAudit, decisionOutcome) {
  const executionSummary = aiDecisionAudit?.family_execution_summary || decisionOutcome?.family_execution_summary || {};
  return [
    ["当前运行模式与评估方式", `${readableState(aiDecisionAudit.configured_mode || "unknown")} / ${readableState(aiDecisionAudit.assessment_operating_mode || "unknown")}`, drawerText(aiDecisionAudit.provider_name, "当前没有模型服务说明")],
    ["方向判断对比", `基础策略 ${readableState(aiDecisionAudit.baseline_direction || "unknown")} / AI ${readableState(aiDecisionAudit.ai_direction || "unknown")}`, `最终结论 ${readableFamilyExecutionDirection(executionSummary, readableState(aiDecisionAudit.final_direction || "unknown"))} / ${readableFamilyExecutionSummary(executionSummary, "当前没有额外执行摘要")}`],
    ["每条书预期边际", readableBookExpectancySummary(executionSummary, "当前没有每条书的边际拆解"), drawerText(executionSummary?.book_expectancy_summary?.source || executionSummary?.bookExpectancySummary?.source, "当前没有额外来源说明")],
    ["每条书当前状态", readableBookRuntimeStateSummary(aiDecisionAudit, "当前没有每条书的原生状态"), "按 long / short 账本原生状态对象记录"],
    ["自适应阈值与仓位因子", readableIndependentAdaptiveSummary(aiDecisionAudit, "当前还没有独立双书自适应摘要"), readableIndependentAdaptiveMeta(aiDecisionAudit, "当前没有额外自适应说明")],
    ["迁移异常摘要", readableIndependentTransitionExceptionSummary(aiDecisionAudit, "当前没有独立双书迁移异常摘要"), readableIndependentTransitionExceptionMeta(aiDecisionAudit, "当前没有额外迁移异常说明")],
    ["预期 vs 已实现", readableExpectedVsRealizedSummary(aiDecisionAudit, "当前还没有预期与已实现诊断"), readableExpectedVsRealizedMeta(aiDecisionAudit, "当前没有额外诊断说明")],
    ["最终采纳路径", decisionSourceLabel(aiDecisionAudit.decision_source), `${decisionSourceNarrative(decisionOutcome)} / ${decisionAuthorityLabel(aiDecisionAudit.decision_authority)}`],
    ["AI 未采纳原因", drawerListText((decisionOutcome?.decision_blocked_reasons || []).map(localizeError), "当前没有额外决策链路阻断项"), drawerListText(aiDecisionAudit.guardrail_flags, "当前没有额外保护规则")],
    ["行情和账户状态", `行情快照 ${freshnessWord(aiDecisionAudit.market_snapshot_fresh)} / 账户快照 ${freshnessWord(aiDecisionAudit.account_snapshot_fresh)}`, `交易前置 ${aiDecisionAudit.safe_to_trade ? "可交易" : "不可交易"} / ${readableState(aiDecisionAudit.execution_condition, "当前没有额外执行条件")}`],
    ["最近执行健康度", `手续费拖累 ${formatNumber(aiDecisionAudit.recent_fee_drag_ratio ?? 0, 3)} / 来回交易 ${formatNumber(aiDecisionAudit.recent_churn_ratio ?? 0, 3)}`, `低边际连续次数 ${formatNumber(aiDecisionAudit.recent_low_edge_trade_streak ?? 0, 0)} / 活动委托 ${formatNumber(aiDecisionAudit.current_open_order_count ?? 0, 0)}`],
  ];
}

function hasMeaningfulAiExecutionSuggestion(aiExecutionSuggestion = null) {
  if (!aiExecutionSuggestion || typeof aiExecutionSuggestion !== "object") return false;
  return Boolean(
    aiExecutionSuggestion.suggestion_present
    || aiExecutionSuggestion.translation_present
    || aiExecutionSuggestion.latest_translation
    || aiExecutionSuggestion.assessment_suggestion
    || aiExecutionSuggestion.execution_plan_translation
    || aiExecutionSuggestion.latest_order_intent_translation
    || aiExecutionSuggestion.live_order_type
    || aiExecutionSuggestion.live_time_in_force
    || (aiExecutionSuggestion.live_limit_price !== null && aiExecutionSuggestion.live_limit_price !== undefined)
    || aiExecutionSuggestion.live_execution_style
  );
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

function shouldRenderHedgeModeAudit(hedgeModeAudit) {
  if (!hedgeModeAudit) return false;
  const positionMode = hedgeModeAudit.position_mode || {};
  return Boolean(
    positionMode.configured_derivatives_position_mode
    || positionMode.exchange_position_mode
    || (positionMode.observed_position_modes || []).length
  );
}

function decisionHedgeModeRows(hedgeModeAudit) {
  const positionMode = hedgeModeAudit.position_mode || {};
  return [
    [
      "本地运行模式",
      hedgeModeLabel(positionMode.configured_derivatives_position_mode),
      `要求交易所模式 ${exchangePositionModeLabel(positionMode.required_exchange_position_mode)}`,
    ],
    [
      "交易所当前模式",
      exchangePositionModeLabel(positionMode.exchange_position_mode),
      positionMode.position_mode_match_required
        ? `强匹配 ${booleanWord(positionMode.exchange_position_mode_matches_configured)}`
        : "当前没有强制匹配要求",
    ],
    [
      "链路里实际看到的模式",
      drawerListText(positionMode.observed_position_modes, "当前没有腿级执行记录"),
      drawerListText(positionMode.observed_pos_sides, "当前没有 long / short 方向记录"),
    ],
    [
      "模式风险",
      positionMode.mode_change_detected ? "当前存在模式变更或不一致信号" : "当前没有模式切换或不一致信号",
      positionMode.contract_mismatch_detected ? "交易所模式和本地配置不一致" : "当前没有 contract mismatch",
    ],
  ];
}

// #39 修复：drawer 一格 kv 横向预算大约能塞 2 条"长行"或 3 条"短行"。
// 短行 = "侧别+模式+动作"，长行 = "目标 X / Δ Y / 原因 Z"。
const OVERLAY_AUDIT_PREVIEW_SHORT = 3;
const OVERLAY_AUDIT_PREVIEW_LONG = 2;

function decisionOverlayAuditRows(overlay) {
  const items = Array.isArray(overlay?.items) ? overlay.items : [];
  const parentSignalSummary = readableOverlayParentSignalSummary(overlay, "");
  return [
    [
      "当前 overlay 模式",
      drawerText(readableState(overlay?.effective_mode || overlay?.configured_mode), "待确认"),
      drawerText(localizeError(overlay?.overlay_source), "当前没有来源归因"),
    ],
    [
      "当前状态",
      `${booleanWord(overlay?.active)} / ${drawerText(readableState(overlay?.state), "待确认")}`,
      drawerListText((overlay?.reason_codes || []).map(localizeError), "当前没有额外状态说明"),
    ],
    [
      "阻断与双书分",
      drawerListText((overlay?.blocked_reasons || []).map(localizeError), "当前没有额外阻断原因"),
      `long ${formatNumber(overlay?.long_leg_score ?? 0, 2)} / short ${formatNumber(overlay?.short_leg_score ?? 0, 2)}`,
    ],
    [
      "父腿暴露信号",
      parentSignalSummary || "当前没有额外父腿信号说明",
      drawerText(localizeError(overlay?.signal_source), "当前没有额外来源说明"),
    ],
    // #39 修复：原本两行 slice 一行写 (0, 3)、一行写 (0, 2)，没有解释。两个
    // 字段渲染的是同一组 overlay 腿，截断到不同长度只有一个原因——第二行的
    // "目标 X / Δ Y / 原因 Z"明显比第一行的"侧别+模式+动作"长，drawer kvList
    // 一格的横向预算大概只够塞 2 条这种长行，第一行更紧凑可以多塞 1 条。
    // 这里把这个 trade-off 写成共享常量，避免读者以为是手抖打错。
    [
      "腿来源与动作",
      items.length
        ? items
          .slice(0, OVERLAY_AUDIT_PREVIEW_SHORT)
          .map((item) => `${drawerText(item.pos_side)} ${drawerText(localizeError(item.execution_mode), drawerText(item.execution_mode))} ${drawerText(item.action)}`)
          .join(" / ")
        : "当前没有 overlay 腿明细",
      items.length
        ? items
          .slice(0, OVERLAY_AUDIT_PREVIEW_LONG)
          .map((item) => `目标 ${drawerText(item.target_position_qty, "0")} / Δ ${drawerText(item.delta_position_qty, "0")} / 原因 ${drawerListText((item.trigger_reason_codes || []).map(localizeError), "当前没有触发原因")}`)
          .join(" / ")
        : "当前没有额外腿级触发说明",
    ],
  ];
}

function decisionOverlayParentPostmortemRows(summary) {
  return [
    [
      "父腿阶段",
      readableOverlayParentSignalSummary(summary, "当前没有额外父腿阶段说明"),
      readableOverlayParentPostmortemMeta(summary, "当前没有额外父腿契约说明"),
    ],
    [
      "双腿数量拆解",
      readableOverlayParentLegQuantitySummary(summary, "当前没有父腿多空数量拆解"),
      `来源 ${drawerText(localizeError(summary?.signal_source), "当前没有额外来源说明")}`,
    ],
  ];
}

function decisionLegOrderRows(legOrders) {
  const items = Array.isArray(legOrders?.items) ? legOrders.items : [];
  return [
    [
      "腿级订单数量",
      `${formatNumber(legOrders?.total_count || 0, 0)} 条`,
      `open ${formatNumber(legOrders?.open_count || 0, 0)} / reduce ${formatNumber(legOrders?.reduce_count || 0, 0)} / close ${formatNumber(legOrders?.close_count || 0, 0)}`,
    ],
    [
      "涉及方向",
      drawerListText(legOrders?.pos_sides, "当前没有 long / short 方向"),
      drawerListText(legOrders?.symbols, "当前没有腿级标的"),
    ],
      [
        "最近腿级订单",
        items.length
          ? items.slice(0, 2).map((item) => `${drawerText(item.symbol)} ${drawerText(item.pos_side)} ${drawerText(item.action)} / ${drawerText(localizeError(item.execution_mode), drawerText(item.execution_mode, "待确认"))}`).join(" / ")
          : "当前没有腿级订单",
        items.length
          ? items.slice(0, 2).map((item) => `数量 ${drawerText(item.quantity, "待确认")} / 角色 ${drawerText(item.strategy_leg_role, "待确认")} / 状态 ${drawerText(item.status, "待同步")} / 成交 ${formatNumber(item.fill_count || 0, 0)} 笔`).join(" / ")
          : "当前没有额外腿级订单明细",
      ],
    ];
}

function decisionLegTrialGuardRows(legTrialGuard) {
  const items = Array.isArray(legTrialGuard?.items) ? legTrialGuard.items : [];
  return [
    [
      "腿级守护状态",
      `${booleanWord(legTrialGuard?.enabled)} / ${drawerText(readableState(legTrialGuard?.mode), "待确认")}`,
      `激活 ${formatNumber(legTrialGuard?.active_count || 0, 0)} / 总计 ${formatNumber(legTrialGuard?.total_count || 0, 0)}`,
    ],
    [
      "long / short 结果",
      items.length
        ? items.map((item) => `${drawerText(item.leg)} ${drawerText(readableState(item.status), "待确认")}`).join(" / ")
        : "当前没有腿级试盘守护结果",
      items.length
        ? items.map((item) => `样本 ${formatNumber(item.recent_closed_trade_count || 0, 0)} / 净收益 ${drawerText(item.recent_net_realized_pnl, "0")} / 胜率 ${formatNumber(item.recent_win_rate || 0, 2)}`).join(" / ")
        : "当前没有额外腿级样本说明",
    ],
    [
      "触发与冷却",
      items.length
        ? items.map((item) => `${drawerText(item.leg)} ${drawerText(localizeError(item.reason_code), item.active ? "已触发腿级试盘守护" : "当前未触发")}`).join(" / ")
        : "当前没有额外腿级试盘守护原因",
      items.length
        ? items.map((item) => `${drawerText(item.leg)} guardrail ${drawerListText((item.guardrail_flags || []).map(localizeError), "当前没有额外 guardrail")} / cooldown ${drawerText(Object.keys(item.cooldowns || {}).length ? JSON.stringify(item.cooldowns) : "当前没有额外冷却")}`).join(" / ")
        : "当前没有额外 guardrail / cooldown 摘要",
    ],
  ];
}

function decisionLegReconciliationRows(legReconciliation) {
  const items = Array.isArray(legReconciliation?.items) ? legReconciliation.items : [];
  return [
    [
      "腿级异常数量",
      `${formatNumber(legReconciliation?.total_count || 0, 0)} 条`,
      `缺执行链 ${formatNumber(legReconciliation?.missing_execution_chain_count || 0, 0)} 条`,
    ],
    [
      "最近腿级异常",
      items.length
        ? items.slice(0, 2).map((item) => `${drawerText(item.symbol)} ${drawerText(item.leg_side)} ${drawerText(item.kind)}`).join(" / ")
        : "当前没有腿级对账异常",
      items.length
        ? items.slice(0, 2).map((item) => `本地 ${drawerText(item.stored_qty, "0")} / 交易所 ${drawerText(item.exchange_qty, "0")}`).join(" / ")
        : "当前没有额外腿级对账明细",
    ],
  ];
}

function decisionSourceLabel(value) {
  const source = String(value || "baseline").toLowerCase();
  if (source === "ai") return "本轮最终采用 AI 结论";
  if (source === "baseline_fallback") return "AI 未被采纳，沿用基础策略";
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

function hedgeModeLabel(value) {
  if (value === "hedge") return "对冲模式";
  if (value === "net") return "净仓模式";
  return drawerText(value, "待确认");
}

function exchangePositionModeLabel(value) {
  if (value === "long_short_mode") return "交易所对冲模式";
  if (value === "net_mode") return "交易所净仓模式";
  return drawerText(value, "交易所未返回");
}

function decisionSourceNarrative(decisionOutcome = null) {
  const source = String(decisionOutcome?.decision_source || "baseline").toLowerCase();
  if (source === "ai") {
    return "虽然系统会继续检查风控和执行条件，但这轮最终结论已经直接采用了 AI。";
  }
  if (source === "baseline_fallback") {
    return `AI 已参与本轮评估，但没有通过最终采纳门槛；系统沿用基础策略的空仓/持仓结论。${drawerListText((decisionOutcome?.decision_blocked_reasons || []).map(localizeError), "当前没有额外阻断说明")}`;
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

// #32 修复：本地 executionSuggestionLabel 已删除，改为 import 自 trade-display.js。

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

// #40 修复：原本 hasLiveField 写成
//   value?.live_order_type || value?.live_time_in_force
//     || value?.live_limit_price !== null && value?.live_limit_price !== undefined
// 这条表达式语义上正确（!== 优先级高于 && 高于 ||），但读到第三段时人脑很容
// 易把它误解析成 (... || (live_limit_price !== null)) && (live_limit_price !== undefined)，
// 实际上 JS 真正执行的是 ... || ((live_limit_price !== null) && (live_limit_price !== undefined))。
// 这里把限价的"!== null && !== undefined"显式括起来，并把整条断成多行，每段
// 含义独立可读。
function liveExecutionSummaryText(value) {
  const hasLiveLimitPrice = value?.live_limit_price !== null && value?.live_limit_price !== undefined;
  const hasLiveField = Boolean(
    value?.live_order_type
    || value?.live_time_in_force
    || hasLiveLimitPrice
  );
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
  const noTradeClassification = extractNoTradeClassification(detail);
  if (!detail.decision_id) return "当前暂无新的策略详情。";
  if (hasNoTradeClassification(noTradeClassification) && noTradeClassification.is_no_trade !== false) {
    return `系统当前对 ${detail.decision_context?.symbol || "当前标的"} 的交易结论是 ${describeDecisionIntent(detail)}。${noTradeClassificationCopy(noTradeClassification)}`;
  }
  if (isNoOrderDecision(detail) && policy.execution_allowed && risk.approved) {
    return `系统当前对 ${detail.decision_context?.symbol || "当前标的"} 的交易结论是 ${describeDecisionIntent(detail)}。策略门禁和风控均未阻断，但本轮没有下单目标。`;
  }
  return `系统当前对 ${detail.decision_context?.symbol || "当前标的"} 的交易结论是 ${describeDecisionIntent(detail)}。`
    + `${policy.execution_allowed ? "策略门禁已通过，" : "策略门禁未通过，"}`
    + `${risk.approved ? "风控当前没有继续阻断。" : `风控仍在拦截：${listOrDash(risk.rejection_reasons)}。`}`;
}

function isNoOrderDecision(detail = {}) {
  const target = detail.position_target || {};
  const rawIntent = String(target.position_intent || "hold").toLowerCase();
  const targetQty = Number(target.target_position_qty ?? 0);
  const deltaQty = Number(target.delta_position_qty ?? 0);
  const orderCount = Array.isArray(detail.order_intents) ? detail.order_intents.length : 0;
  return rawIntent === "hold" && targetQty === 0 && deltaQty === 0 && orderCount === 0;
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
  return readableFamilyExecutionSummary(target, readableState(rawIntent));
}
