import { actionButton, callout, kvList, pill, responsiveTable, statGrid, summaryStrip, surfaceCard } from "../components.js";
import { formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, listOrDash } from "../formatters.js";
import { localizeError, readableState } from "../terms.js";

const AI_STATE_MAP = {
  baseline_only: "仅按基础策略运行",
  ai_assisted: "AI 辅助决策",
  ai_decision_maker: "AI 决策者",
  ai_decision_maker_with_profile_control: "AI 决策者并控制策略档位",
  disabled: "已关闭",
  diagnostic_only: "仅诊断",
  shadow_translation: "影子翻译",
  enabled_live: "可进入实盘执行",
  enabled: "已进入受限实盘翻译",
  healthy: "健康",
  degraded: "已降级",
  review_required: "需要复核",
  underperforming: "表现落后",
  mixed: "表现分化",
  insufficient_data: "样本不足",
  trend: "趋势",
  breakout: "突破",
  range: "震荡",
  uncertain: "不确定",
  same_as_baseline: "与基础策略一致",
  hold_instead: "改为继续观望/持有",
  entry_override: "改为开仓",
  exit_override: "改为退出",
  reverse_override: "改为反手",
  hold: "保持仓位",
  flat: "继续观望",
  long: "偏多",
  short: "偏空",
  ai: "AI 决策",
  baseline: "基础策略决策",
  baseline_fallback: "基础策略回退结果",
  admin_override: "管理员覆盖",
  env_default: "环境默认",
  admin: "管理员手动覆盖",
  system: "系统治理链路",
  reference_only: "基础策略主导",
  advisory: "AI 辅助建议",
  final_decision: "AI 最终决策",
  final_decision_with_profile_control: "AI 最终决策并控制策略档位",
  normal: "正常",
  maker_bias: "偏被动",
  taker_bias: "偏主动",
  bounded_limit_ioc: "受限限价成交",
  bounded_taker_cap: "受限主动成交",
  not_requested: "未请求",
};

const AI_ERROR_MAP = {
  ai_degraded_requires_manual_review: "AI 已降级且未开启自动回退，需要人工确认后再恢复 AI 决策链路。",
  ai_auto_downgraded: "AI 已自动降级，当前只保留基础策略决策链路。",
  output_rejected: "AI 输出结构有效，但没有通过交易语义校验。",
  ai_fallback_used: "本轮使用了回退结果，不能让 AI 直接改写基础策略。",
  ai_output_invalid: "AI 输出没有通过校验。",
  ai_confidence_below_threshold: "校准置信度低于 AI 决策链路最低门槛。",
  ai_uncertainty_above_threshold: "不确定性高于 AI 决策链路允许阈值。",
  ai_directional_edge_too_small: "方向边际不足，不能让 AI 直接改写基础策略。",
  ai_override_not_recommended: "AI 自己都不建议覆盖基础策略。",
  ai_not_economically_actionable: "预期净边际覆盖不了成本和噪声。",
  ai_regime_not_allowed: "当前市场状态不允许 AI 直接改写基础策略。",
  ai_open_orders_present: "当前还有活动委托，不允许 AI 改写方向。",
  ai_flat_context_requires_stronger_edge: "空仓场景下需要更强的方向边际才能开仓。",
  execution_parameter_suggestions_disabled: "执行建议功能当前关闭。",
  diagnostic_only_no_live_execution: "当前只记录建议，不允许进入真实执行。",
  shadow_translation_preview_only: "当前只生成影子翻译结果，不改写真实委托。",
  planner_boundary_disabled: "执行器边界关闭了 AI 建议下探。",
  planner_recorded_suggestion_only: "执行器只保留建议供诊断使用。",
  planner_translated_execution_preview: "执行器已生成影子翻译预览。",
  bounded_live_translation_applied: "执行器已经把建议限制性地转成真实下单字段。",
  live_translation_not_enabled: "当前没有启用实盘授权。",
  live_translation_requires_limit_cap: "只有能转成价格保护型限价保护的建议才允许进入实盘授权。",
  live_translation_requires_reference_price: "缺少参考价格，不能安全生成实盘价格保护。",
  live_translation_requires_limit_offset: "缺少有效价格偏移，不能生成实盘限价保护。",
  live_translation_requires_slippage_guard: "缺少滑点保护，不能启用受限实盘翻译。",
};

function humanState(value) {
  if (value === null || value === undefined || value === "") return "待确认";
  const key = String(value).trim().toLowerCase();
  return AI_STATE_MAP[key] || readableState(value);
}

function humanError(value) {
  if (!value) return "当前没有额外说明";
  const key = String(value).trim();
  return AI_ERROR_MAP[key] || localizeError(key);
}

function textOrFallback(value, fallback = "待确认") {
  if (value === null || value === undefined) return fallback;
  const text = String(value).trim();
  return text || fallback;
}

function signedOrFallback(value, digits = 4, fallback = "待同步") {
  return value === null || value === undefined ? fallback : formatSigned(value, digits);
}

function basisPoints(value, digits = 2, fallback = "待确认") {
  return value === null || value === undefined ? fallback : `${formatNumber(value, digits)} 个基点`;
}

function configuredMode(runtime = {}) {
  return runtime.configured_operating_mode || "unknown";
}

function effectiveMode(runtime = {}) {
  return runtime.effective_operating_mode || "unknown";
}

function toneForRuntime(runtime) {
  if (effectiveMode(runtime) === "baseline_only" && configuredMode(runtime) !== "baseline_only") {
    return "warning";
  }
  if (runtime?.degraded) return "warning";
  if (effectiveMode(runtime) === "baseline_only") return "outline";
  return "positive";
}

function toneForShadowSummary(shadowSummary) {
  if (!shadowSummary?.window_count) return "outline";
  if (shadowSummary.review_required) return "danger";
  if (shadowSummary.status === "underperforming") return "warning";
  if (shadowSummary.status === "healthy") return "positive";
  return "info";
}

function aiRuntimeNarrative(runtime, latestDegradation) {
  if (!runtime || Object.keys(runtime).length === 0) {
    return "当前暂无 AI 决策链路运行状态，可能是页面刚加载完，或当前配置没有启用 AI。";
  }
  if (effectiveMode(runtime) === "baseline_only" && configuredMode(runtime) !== "baseline_only") {
    return `AI 当前没有真实参与下单决策链路。最近一次降级原因：${humanError(runtime.degradation_reason || latestDegradation?.reason_code)}。`;
  }
  if (effectiveMode(runtime) === "baseline_only") {
    return "当前配置本来就是仅基础策略模式。AI 不参与真实交易决策，但仍会保留诊断和影子数据。";
  }
  if (effectiveMode(runtime) === "ai_assisted") {
    return "AI 当前参与辅助判断和诊断，但最终是否进入真实交易，仍由基础策略和治理链路共同决定。";
  }
  if (effectiveMode(runtime) === "ai_decision_maker_with_profile_control") {
    return "AI 当前参与真实交易决策，并可联动控制运行策略档位；但最终落地前仍要通过经济可行动性、风控和执行状态等门禁。";
  }
  return "AI 当前参与真实交易决策，但最终落地前仍要同时通过经济可行动性、风控和执行状态等门禁。";
}

function economicGateRows(latestAssessment, latestOutcome, latestProfileControl) {
  if (!latestAssessment) return [];
  return [
    [
      "预期优势",
      basisPoints(latestAssessment.estimated_edge_bps, 2),
      `方向优势 ${formatNumber(latestAssessment.directional_edge ?? 0, 3)}`,
    ],
    [
      "预期成本",
      basisPoints(latestAssessment.estimated_cost_bps, 2),
      `净优势 ${basisPoints(latestAssessment.estimated_net_edge_bps, 2)}`,
    ],
    [
      "经济可行动性",
      latestAssessment.economically_actionable ? "满足开仓条件" : "净边际不足",
      listOrDash(latestAssessment.validation_flags?.map(humanError)),
    ],
    [
      "本轮决策来源",
      humanState(latestOutcome?.decision_source || "baseline"),
      listOrDash((latestOutcome?.decision_blocked_reasons || latestAssessment.rejection_flags || []).map(humanError)),
    ],
    [
      "策略档位控制",
      latestProfileControl?.applied ? `已切到 ${textOrFallback(latestProfileControl?.requested_profile_id, "待确认")}` : humanState(latestOutcome?.profile_control_source || "env_default"),
      latestProfileControl?.applied
        ? "本轮已由真实决策链路触发运行时策略档位切换"
        : listOrDash((latestProfileControl?.blocked_reasons || []).map(humanError), "本轮没有新的策略档位切换动作"),
    ],
  ];
}

function executionSuggestionRows(summary = {}) {
  const latest = summary.latest_translation || {};
  const preview = latest.translation_preview || {};
  const suggestion = latest.suggestion || summary.assessment_suggestion?.suggestion || {};
  return [
    [
      "执行建议模式",
      humanState(summary.configured_mode || "disabled"),
      summary.translation_present ? "已有翻译结果" : summary.suggestion_present ? "已有建议待翻译" : "最近没有执行建议",
    ],
    [
      "翻译器状态",
      humanState(summary.status || "absent"),
      latest.applied_to_live_execution ? "已进入受限实盘" : "当前不会改写真实下单",
    ],
    [
      "实盘应用",
      latest.applied_to_live_execution ? "已受限进入实盘" : "当前没有进入实盘",
      latest.applied_to_live_execution ? listOrDash(latest.applied_live_fields) : humanError(latest.live_translation_fallback_reason),
    ],
    [
      "建议执行姿态",
      `被动倾向 ${formatNumber(suggestion.passive_bias ?? 0, 2)} / 主被动偏向 ${formatSigned(suggestion.maker_taker_bias ?? 0, 2)}`,
      `拆单 ${formatNumber(suggestion.slice_count ?? 0, 0)} / 参与率 ${formatNumber(suggestion.max_participation_rate ?? 0, 2)}`,
    ],
    [
      "影子翻译预览",
      `${humanState(preview.execution_style || "not_requested")} / ${humanState(preview.order_type || "not_requested")}`,
      preview.order_type
        ? `${textOrFallback(preview.time_in_force, "时效待确认")} / 价格偏移 ${basisPoints(preview.limit_offset_bps, 2)} / 实盘限价 ${formatNumber(summary.live_limit_price ?? 0, 2)}`
        : "当前没有翻译预览",
    ],
    [
      "拒绝或裁剪",
      listOrDash([...(latest.rejection_reasons || []), ...(latest.clipped_fields || [])].map(humanError)),
      listOrDash((latest.notes || []).map(humanError)),
    ],
  ];
}

function renderPaginationFooter(payload, { key, loadAction, collapseAction }) {
  const shown = Array.isArray(payload?.assessments)
    ? payload.assessments.length
    : Array.isArray(payload?.shadow_decisions)
        ? payload.shadow_decisions.length
        : Array.isArray(payload?.evaluations)
          ? payload.evaluations.length
          : 0;
  const total = Number(payload?.total_available || shown);
  const hasMore = Boolean(payload?.has_more);
  const limit = Number(payload?.limit || shown);
  if (!shown) return "";
  return `
    <div class="history-footer">
      <p class="meta-copy">当前显示 ${shown} / ${total} 条${key}。</p>
      <div class="stack-actions">
        ${hasMore ? actionButton(`加载更多${key}`, loadAction, "", "secondary") : ""}
        ${limit > 8 ? actionButton("收起到最新 8 条", collapseAction, "", "ghost") : ""}
      </div>
    </div>
  `;
}

function readableList(items, fallback = "暂无说明") {
  return listOrDash(items, fallback);
}

function assessmentCards(assessments) {
  return assessments.map((item) => ({
    kicker: "判断记录",
    title: `${humanState(item.regime || "unknown")} | ${formatRelativeAge(item.created_at)}`,
    meta: formatMaybeTimestamp(item.created_at),
    tone: item.economically_actionable ? "positive" : "warning",
    badge: pill(item.economically_actionable ? "可交易" : "不建议交易", item.economically_actionable ? "positive" : "warning"),
    fields: [
      { label: "是否建议覆盖", value: item.baseline_override_recommended ? "建议改写基础策略" : "不建议改写" },
      { label: "净边际", value: basisPoints(item.estimated_net_edge_bps, 2), meta: `方向优势 ${formatNumber(item.directional_edge ?? 0, 2)}` },
      { label: "本轮结果", value: item.fallback_used ? "使用回退结果" : "使用模型结果" },
    ],
    details: [
      { label: "覆盖原因", value: readableList(item.override_reason_codes, "当前没有额外覆盖原因") },
      { label: "拒绝 / 校验", value: readableList((item.rejection_flags || item.validation_flags || []).map(humanError), "当前没有额外门禁说明") },
    ],
    detailLabel: "展开本轮判断",
  }));
}

function shadowDecisionCards(items) {
  return items.map((item) => ({
    kicker: "影子记录",
    title: `${humanState(item.ai_shadow_action || "unknown")} | ${formatRelativeAge(item.created_at)}`,
    meta: formatMaybeTimestamp(item.created_at),
    tone: item.would_override_baseline ? "warning" : "positive",
    badge: pill(item.would_override_baseline ? "会改写基础策略" : "与基础策略一致", item.would_override_baseline ? "warning" : "positive"),
    fields: [
      { label: "基础策略动作", value: humanState(item.baseline_action || "unknown"), meta: `目标 ${formatNumber(item.baseline_target_qty ?? 0)}` },
      { label: "影子动作", value: humanState(item.ai_shadow_action || "unknown"), meta: `目标 ${formatNumber(item.ai_shadow_target_qty ?? 0)}` },
      { label: "动作差异", value: humanState(item.shadow_action_type || "unknown") },
    ],
    details: [
      { label: "原因", value: readableList(item.reason_codes, "当前没有额外原因说明") },
    ],
    detailLabel: "展开影子动作",
  }));
}

function shadowEvaluationCards(evaluations) {
  return evaluations.map((item) => ({
    kicker: "收益评估",
    title: `${formatRelativeAge(item.window_end)} | ${item.shadow_outperformed === null ? "待结论" : item.shadow_outperformed ? "影子结果更优" : "基础策略更优"}`,
    meta: `${formatMaybeTimestamp(item.window_start)} ~ ${formatMaybeTimestamp(item.window_end)}`,
    tone: item.shadow_outperformed === null ? "outline" : item.shadow_outperformed ? "positive" : "warning",
    badge: item.shadow_outperformed === null
      ? pill("尚未得出结论", "outline")
      : item.shadow_outperformed
        ? pill("影子结果更优", "positive")
        : pill("基础策略更优", "warning"),
    fields: [
      { label: "基础策略净收益", value: formatNumber(item.baseline_net_pnl ?? 0), meta: `交易 ${formatNumber(item.baseline_trade_count ?? 0, 0)}` },
      { label: "影子净收益", value: formatNumber(item.shadow_net_pnl ?? 0), meta: `交易 ${formatNumber(item.shadow_trade_count ?? 0, 0)}` },
      { label: "手续费 / 来回交易差值", value: `${formatSigned((Number(item.shadow_fee_ratio ?? 0) - Number(item.baseline_fee_ratio ?? 0)), 4)} / ${formatSigned((Number(item.shadow_churn_ratio ?? 0) - Number(item.baseline_churn_ratio ?? 0)), 4)}` },
    ],
    details: [
      { label: "基础策略毛收益", value: formatNumber(item.baseline_gross_pnl ?? 0) },
      { label: "影子毛收益", value: formatNumber(item.shadow_gross_pnl ?? 0) },
    ],
    detailLabel: "展开收益评估",
  }));
}

function performanceReportCards(reports = [], replayContext = {}) {
  return reports.map((item) => ({
    kicker: "表现记录",
    title: `${humanState(item.latest_status || "insufficient_data")} | ${formatRelativeAge(item.generated_at)}`,
    meta: formatMaybeTimestamp(item.generated_at),
    tone: item.review_required ? "warning" : item.latest_status === "healthy" ? "positive" : "outline",
    badge: pill(humanState(item.latest_status || "insufficient_data"), item.review_required ? "warning" : item.latest_status === "healthy" ? "positive" : "outline"),
    fields: [
      {
        label: "短 / 中 / 长窗净收益差",
        value: `${formatSigned(item.windows?.short?.net_pnl_delta_total ?? 0, 4)} / ${formatSigned(item.windows?.medium?.net_pnl_delta_total ?? 0, 4)} / ${formatSigned(item.windows?.long?.net_pnl_delta_total ?? 0, 4)}`,
      },
      {
        label: "结果复核",
        value: item.review_required ? "需要复核" : "未触发",
        meta: `短窗 ${formatNumber(item.windows?.short?.review_required_count ?? 0, 0)} / 中窗 ${formatNumber(item.windows?.medium?.review_required_count ?? 0, 0)} / 长窗 ${formatNumber(item.windows?.long?.review_required_count ?? 0, 0)}`,
      },
      {
        label: "回放上下文",
        value: formatMaybeTimestamp(replayContext.latest_validation?.validated_at),
        meta: `偏差 ${formatNumber(replayContext.latest_validation?.divergence_count ?? 0, 0)}`,
      },
    ],
    detailLabel: "展开长期表现报告",
  }));
}

export function renderAISections(data) {
  const blockerControl = data.blockerControl || {};
  const overview = data.aiOverview || {};
  const performanceView = overview.performance_view || {};
  const runtime = overview.runtime || data.aiRuntime || {};
  const latest = data.aiLatest || {};
  const recentPayload = data.aiRecent || {};
  const recentAssessments = recentPayload.assessments || [];
  const latestBaseline = overview.latest_baseline_reference || latest.baseline_reference || null;
  const latestIntent = overview.latest_ai_decision_intent || latest.ai_decision_intent || null;
  const latestAssessment = overview.latest_assessment || latest.assessment || null;
  const latestOutcome = overview.latest_decision_outcome || latest.decision_outcome || null;
  const latestProfileControl = overview.latest_profile_control_decision || latest.profile_control_decision || null;
  const latestDegradation = overview.latest_degradation || null;
  const latestShadowDecision = overview.latest_shadow_decision || data.aiShadowLatest?.shadow_decision || null;
  const shadowRecentPayload = data.aiShadowRecent || {};
  const shadowRecent = shadowRecentPayload.shadow_decisions || [];
  const evaluationsPayload = data.aiShadowEvaluations || {};
  const evaluations = evaluationsPayload.evaluations || [];
  const shadowSummary = overview.shadow_summary || {};
  const performanceWindows = overview.performance_windows || {};
  const downgradeState = overview.downgrade_state || {};
  const executionSuggestion = overview.latest_execution_suggestion || latest.execution_suggestion || {};
  const aiReviewBlocker = (blockerControl.blockers || []).find((item) => item?.blocker === "ai_degraded_requires_manual_review") || null;

  return {
    aiHero: surfaceCard({
      title: "AI 状态概览",
      kicker: "运行状态",
      copy: "这里集中看配置模式、当前有效模式、降级状态、最近一次真实决策来源，以及影子评估的自动观察结果。",
      classes: "hero-card",
      content: `
        ${callout({
          title:
            effectiveMode(runtime) === "baseline_only"
              ? "AI 当前没有进入真实交易决策链路"
              : `AI 当前有效模式：${humanState(effectiveMode(runtime))}`,
          copy: aiRuntimeNarrative(runtime, latestDegradation),
          pills: [
            pill(`配置模式 ${humanState(configuredMode(runtime))}`, "info"),
            pill(`当前有效模式 ${humanState(effectiveMode(runtime))}`, toneForRuntime(runtime)),
            pill(`模型服务 ${runtime.provider_ready ? "已就绪" : "未就绪"}`, runtime.provider_ready ? "positive" : "warning"),
            pill(`影子评估 ${runtime.shadow_mode_enabled ? "自动常开" : "未开启"}`, runtime.shadow_mode_enabled ? "info" : "outline"),
          ],
        })}
        ${statGrid([
          {
            label: "连续失败 / 成功",
            value: `${formatNumber(runtime.consecutive_failures ?? 0, 0)} / ${formatNumber(runtime.consecutive_successes ?? 0, 0)}`,
            meta: `最近评估 ${formatNumber(runtime.recent_assessment_count ?? 0, 0)} 次`,
          },
          {
            label: "近期回退比率",
            value: formatNumber(runtime.recent_fallback_ratio ?? 0, 3),
            meta: `timeout ${formatNumber(runtime.recent_timeout_count ?? 0, 0)} / 无效输出 ${formatNumber(runtime.recent_invalid_output_count ?? 0, 0)}`,
          },
          {
            label: "最近一次真实决策来源",
            value: humanState(latestOutcome?.decision_source || "baseline"),
            meta: humanState(latestOutcome?.decision_authority || "reference_only"),
          },
          {
            label: "影子结果优于基础策略",
            value: formatNumber(shadowSummary.outperformed_rate ?? 0, 3),
            meta: `评估窗口 ${formatNumber(shadowSummary.window_count ?? 0, 0)} / 状态 ${humanState(shadowSummary.status || "insufficient_data")}`,
          },
        ])}
        ${latestDegradation
          ? kvList([
              ["最近一次降级", humanError(latestDegradation.reason_code), formatMaybeTimestamp(latestDegradation.created_at)],
              ["恢复探测", runtime.recovery_probe_after ? formatMaybeTimestamp(runtime.recovery_probe_after) : "恢复窗口待确认", runtime.recovery_probe_ready ? "可以开始探测恢复" : "还没到恢复窗口"],
            ])
          : ""}
        ${kvList([
          ["模型服务 / 结果状态", humanState(downgradeState.provider_state || "healthy"), humanState(downgradeState.outcome_state || "healthy")],
          ["当前降级原因", humanError(downgradeState.degradation_reason), humanError(downgradeState.outcome_degradation_reason)],
          ["失败预算", `还能承受失败 ${formatNumber(downgradeState.failure_budget?.remaining_failures_until_degrade ?? 0, 0)} 次`, `恢复还需成功 ${formatNumber(downgradeState.failure_budget?.remaining_successes_until_recover ?? 0, 0)} 次`],
          ["结果预算", `坏窗口阈值 ${formatNumber(downgradeState.outcome_policy?.bad_window_threshold ?? 0, 0)}`, `还可承受 ${formatNumber(downgradeState.outcome_policy?.remaining_bad_windows_until_review ?? 0, 0)} 个坏窗口`],
        ])}
      `,
    }),
    aiLatest: surfaceCard({
      title: "决策链概览",
      kicker: "最新结果",
      copy: "这里优先看基础策略参考、AI 意图、最终决策结果和策略档位控制，用来判断 AI 是否真正影响了本轮动作。",
      content: latestAssessment || latestBaseline || latestIntent || latestOutcome
        ? `
            ${summaryStrip([
              {
                label: "基础策略参考",
                value: humanState(latestBaseline?.direction_bias || latestAssessment?.regime || "unknown"),
                meta: latestBaseline ? `置信度 ${formatNumber(latestBaseline.confidence ?? 0, 2)}` : `方向优势 ${formatNumber(latestAssessment?.directional_edge ?? 0, 2)}`,
                tone: "info",
              },
              {
                label: "AI 决策意图",
                value: humanState(latestIntent?.direction || "unknown"),
                meta: latestIntent ? `${humanState(latestIntent.action)} / 目标 ${formatNumber(latestIntent.target_qty ?? 0)}` : "当前暂无新的 AI 决策意图",
                tone: latestIntent ? "warning" : "outline",
              },
              {
                label: "最终决策结果",
                value: humanState(latestOutcome?.final_action || "hold"),
                meta: latestOutcome ? `${humanState(latestOutcome.decision_source)} / 目标 ${formatNumber(latestOutcome.final_target_qty ?? 0)}` : "等待真实决策结果",
                tone: latestOutcome?.decision_source === "ai" ? "positive" : latestOutcome?.decision_source === "baseline_fallback" ? "info" : "warning",
              },
              {
                label: "策略档位控制",
                value: latestProfileControl?.applied ? `已切到 ${textOrFallback(latestProfileControl?.requested_profile_id, "待确认")}` : humanState(latestOutcome?.profile_control_source || "env_default"),
                meta: latestProfileControl?.applied ? "本轮真实决策链路已完成运行时策略档位切换" : "本轮没有新的策略档位切换动作",
                tone: latestProfileControl?.applied ? "positive" : "outline",
              },
            ])}
            ${kvList([
              ["判断时间", formatMaybeTimestamp(latestAssessment?.created_at), formatRelativeAge(latestAssessment?.created_at)],
              ["基础策略参考", humanState(latestBaseline?.direction_bias || "unknown"), latestBaseline ? `置信度 ${formatNumber(latestBaseline.confidence ?? 0, 2)} / ${listOrDash(latestBaseline.reason_codes, "当前没有额外说明")}` : "当前暂无新的基础策略参考"],
              ["AI 决策意图", latestIntent ? `${humanState(latestIntent.direction)} / ${humanState(latestIntent.action)}` : "当前暂无新的 AI 决策意图", latestIntent ? `目标 ${formatNumber(latestIntent.target_qty ?? 0)} / ${listOrDash(latestIntent.reason_codes, "当前没有额外说明")}` : "多半仍在基础策略决策链路，或 AI 已回退"],
              ["最终决策来源", humanState(latestOutcome?.decision_source || "baseline"), latestOutcome ? humanState(latestOutcome.decision_authority || "reference_only") : "当前暂无统一决策结果"],
              ["决策阻断项", listOrDash((latestOutcome?.decision_blocked_reasons || latestAssessment?.rejection_flags || []).map(humanError)), latestOutcome?.decision_source === "ai" ? "当前由 AI 直接给出最终决策" : "当前没有进入 AI 最终决策"],
              ["策略档位控制", latestProfileControl?.applied ? `已切到 ${textOrFallback(latestProfileControl?.requested_profile_id, "待确认")}` : humanState(latestOutcome?.profile_control_source || "env_default"), latestProfileControl?.applied ? "本轮真实决策链路已完成运行时策略档位切换" : listOrDash((latestProfileControl?.blocked_reasons || []).map(humanError), "本轮没有新的策略档位切换动作")],
              ["最新影子动作", humanState(latestShadowDecision?.ai_shadow_action || "unknown"), latestShadowDecision ? `相对基础策略：${humanState(latestShadowDecision.shadow_action_type)}` : "当前暂无影子动作"],
            ])}
            ${surfaceCard({
              title: "经济性概览",
              kicker: "经济门槛",
              copy: "这里把预期优势、成本、净优势和决策链路阻断拆开看。",
              classes: "is-muted",
              content: kvList(economicGateRows(latestAssessment, latestOutcome, latestProfileControl)),
            })}
          `
        : callout({
            title: "当前暂无新的 AI 判断",
            copy: "当前多半仍在仅基础策略模式，或者模型服务处于降级后的自动回退状态。",
            pills: [pill(`当前有效模式 ${humanState(effectiveMode(runtime))}`, "outline")],
          }),
    }),
    aiExecutionSuggestion: surfaceCard({
      title: "执行建议概览",
      kicker: "执行边界",
      copy: "这里集中看建议模式、翻译器状态、影子预览，以及哪些字段被裁剪或拒绝。",
      classes: "ai-side-panel",
      content: kvList(executionSuggestionRows(executionSuggestion)),
    }),
    aiReview: aiReviewBlocker
      ? surfaceCard({
          title: "AI 复核处置",
          kicker: "人工动作",
          copy: "这里处理 AI 结果复核，不直接下单，也不会自动修改仓位；它只决定后续是否继续信任 AI 决策链路。",
          content: `
            ${summaryStrip([
              {
                label: "当前阻断",
                value: textOrFallback(aiReviewBlocker.title, humanError(aiReviewBlocker.blocker)),
                meta: textOrFallback(aiReviewBlocker.impact, "当前阻断会影响 AI 决策链路恢复。"),
                tone: "danger",
              },
              {
                label: "当前建议",
                value: textOrFallback(aiReviewBlocker.recommended_next_step, "请先完成这次人工复核。"),
                meta: "复核通过后可恢复 AI，复核不通过则改为仅基础策略继续运行。",
                tone: "warning",
              },
            ])}
            ${kvList([
              ["复核原因", textOrFallback(aiReviewBlocker.description, "当前已触发 AI 结果复核。"), textOrFallback(latestDegradation?.reason_code, "当前没有额外降级原因说明")],
              ["最近影子评估", readableShadowMeta(shadowSummary), shadowSummary.review_required ? "当前已进入人工复核状态。" : "当前没有新的结果复核。"],
            ])}
            ${renderReviewActions(aiReviewBlocker)}
          `,
        })
      : null,
    aiHistory: surfaceCard({
      title: "AI 记录",
      kicker: "历史记录",
      copy: "这里集中看判断记录、影子动作和收益评估。",
      content: `
        <div class="panel-grid">
          <div class="span-12">
            ${summaryStrip([
              {
                label: "近 3 窗口净收益差",
                value: signedOrFallback(performanceWindows.short?.net_pnl_delta_total, 4, "暂未形成结论"),
                meta: `跑赢率 ${formatNumber(performanceWindows.short?.outperformed_rate ?? 0, 3)}`,
                tone: Number(performanceWindows.short?.net_pnl_delta_total || 0) >= 0 ? "positive" : "warning",
              },
              {
                label: "近 5 窗口净收益差",
                value: signedOrFallback(performanceWindows.medium?.net_pnl_delta_total, 4, "暂未形成结论"),
                meta: `跑赢率 ${formatNumber(performanceWindows.medium?.outperformed_rate ?? 0, 3)}`,
                tone: Number(performanceWindows.medium?.net_pnl_delta_total || 0) >= 0 ? "positive" : "warning",
              },
              {
                label: "近 10 窗口手续费拖累差",
                value: signedOrFallback(performanceWindows.long?.avg_fee_ratio_delta, 4, "暂未形成结论"),
                meta: `需复核 ${formatNumber(performanceWindows.long?.review_required_count ?? 0, 0)} 次`,
                tone: Number(performanceWindows.long?.avg_fee_ratio_delta || 0) <= 0 ? "positive" : "warning",
              },
              {
                label: "近 10 窗口来回交易差",
                value: signedOrFallback(performanceWindows.long?.avg_churn_ratio_delta, 4, "暂未形成结论"),
                meta: `样本 ${formatNumber(performanceWindows.long?.sample_size ?? 0, 0)}`,
                tone: Number(performanceWindows.long?.avg_churn_ratio_delta || 0) <= 0 ? "positive" : "warning",
              },
            ])}
          </div>

          <div class="span-12">
            ${summaryStrip([
              {
                label: "影子状态",
                value: humanState(shadowSummary.status || "insufficient_data"),
                meta: shadowSummary.review_required ? "需要人工复核" : "当前未触发结果复核",
                tone: toneForShadowSummary(shadowSummary),
              },
              {
                label: "最新净收益差值",
                value: signedOrFallback(shadowSummary.latest_net_pnl_delta, 4, "暂未形成结论"),
                meta: "影子净收益 - 基础策略净收益",
                tone: Number(shadowSummary.latest_net_pnl_delta || 0) >= 0 ? "positive" : "warning",
              },
              {
                label: "最新手续费拖累差值",
                value: signedOrFallback(shadowSummary.latest_fee_ratio_delta, 4, "暂未形成结论"),
                meta: "影子手续费比例 - 基础策略手续费比例",
                tone: Number(shadowSummary.latest_fee_ratio_delta || 0) <= 0 ? "positive" : "warning",
              },
              {
                label: "最新来回交易差值",
                value: signedOrFallback(shadowSummary.latest_churn_ratio_delta, 4, "暂未形成结论"),
                meta: "影子来回交易比例 - 基础策略来回交易比例",
                tone: Number(shadowSummary.latest_churn_ratio_delta || 0) <= 0 ? "positive" : "warning",
              },
            ])}
          </div>

          <div class="span-12">
            ${responsiveTable(
              ["记录时间", "市场判断", "覆盖建议", "经济性", "结果摘要"],
              recentAssessments.map((item) => [
                `<div><strong>${formatRelativeAge(item.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.created_at)}</div></div>`,
                `<div><strong>${humanState(item.regime || "unknown")}</strong><div class="table-meta">方向优势 ${formatNumber(item.directional_edge ?? 0, 2)}</div></div>`,
                `<div><strong>${item.baseline_override_recommended ? "建议改写基础策略" : "不建议改写"}</strong><div class="table-meta">${listOrDash(item.override_reason_codes)}</div></div>`,
                `<div><strong>${item.economically_actionable ? "可交易" : "不建议交易"}</strong><div class="table-meta">净边际 ${basisPoints(item.estimated_net_edge_bps, 2)}</div></div>`,
                `<div><strong>${item.fallback_used ? "使用回退结果" : "使用模型结果"}</strong><div class="table-meta">${listOrDash((item.rejection_flags || item.validation_flags || []).map(humanError))}</div></div>`,
              ]),
              "当前暂无 AI 判断记录。",
              assessmentCards(recentAssessments)
            )}
            ${renderPaginationFooter(recentPayload, {
              key: "AI 判断记录",
              loadAction: "load-more-ai-assessments",
              collapseAction: "collapse-ai-assessments",
            })}
          </div>

          <div class="span-12">
            ${responsiveTable(
              ["记录时间", "基础策略动作", "影子动作", "覆盖判断", "差异摘要"],
              shadowRecent.map((item) => [
                `<div><strong>${formatRelativeAge(item.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.created_at)}</div></div>`,
                `<div><strong>${humanState(item.baseline_action || "unknown")}</strong><div class="table-meta">目标 ${formatNumber(item.baseline_target_qty ?? 0)}</div></div>`,
                `<div><strong>${humanState(item.ai_shadow_action || "unknown")}</strong><div class="table-meta">目标 ${formatNumber(item.ai_shadow_target_qty ?? 0)}</div></div>`,
                item.would_override_baseline ? pill("会改写基础策略", "warning") : pill("与基础策略一致", "positive"),
                `<div><strong>${humanState(item.shadow_action_type || "unknown")}</strong><div class="table-meta">${listOrDash(item.reason_codes)}</div></div>`,
              ]),
              "当前暂无影子动作记录。",
              shadowDecisionCards(shadowRecent)
            )}
            ${renderPaginationFooter(shadowRecentPayload, {
              key: "影子动作",
              loadAction: "load-more-ai-shadow-decisions",
              collapseAction: "collapse-ai-shadow-decisions",
            })}
          </div>

          <div class="span-12">
            ${summaryStrip([
              {
                label: "结果复核",
                value: shadowSummary.review_required ? "需要人工复核" : "当前未触发",
                meta: `窗口状态 ${humanState(shadowSummary.status || "insufficient_data")}`,
                tone: shadowSummary.review_required ? "danger" : toneForShadowSummary(shadowSummary),
              },
            ])}
            ${responsiveTable(
              ["评估窗口", "基础策略回放", "影子回放", "成本与来回交易", "结果摘要"],
              evaluations.map((item) => [
                `<div><strong>${formatMaybeTimestamp(item.window_end)}</strong><div class="table-meta">${formatMaybeTimestamp(item.window_start)} ~ ${formatMaybeTimestamp(item.window_end)}</div></div>`,
                `<div><strong>净收益 ${formatNumber(item.baseline_net_pnl ?? 0)}</strong><div class="table-meta">毛收益 ${formatNumber(item.baseline_gross_pnl ?? 0)} / 交易 ${formatNumber(item.baseline_trade_count ?? 0, 0)}</div></div>`,
                `<div><strong>净收益 ${formatNumber(item.shadow_net_pnl ?? 0)}</strong><div class="table-meta">毛收益 ${formatNumber(item.shadow_gross_pnl ?? 0)} / 交易 ${formatNumber(item.shadow_trade_count ?? 0, 0)}</div></div>`,
                `<div><strong>手续费差值 ${formatSigned((Number(item.shadow_fee_ratio ?? 0) - Number(item.baseline_fee_ratio ?? 0)), 4)}</strong><div class="table-meta">来回交易差值 ${formatSigned((Number(item.shadow_churn_ratio ?? 0) - Number(item.baseline_churn_ratio ?? 0)), 4)}</div></div>`,
                item.shadow_outperformed === null
                  ? pill("尚未得出结论", "outline")
                  : item.shadow_outperformed
                    ? pill("影子结果更优", "positive")
                    : pill("基础策略更优", "warning"),
              ]),
              "当前暂无影子收益评估。",
              shadowEvaluationCards(evaluations)
            )}
            ${renderPaginationFooter(evaluationsPayload, {
              key: "影子收益评估",
              loadAction: "load-more-ai-shadow-evaluations",
              collapseAction: "collapse-ai-shadow-evaluations",
            })}
          </div>
        </div>
      `,
    }),
    aiPerformanceReports: surfaceCard({
      title: "表现报告",
      kicker: "长期表现",
      copy: "这里不再只看单次影子评估，而是看已经持久化的长期表现序列，以及最近回放健康度对 AI 决策链路的约束。",
      content: `
        ${summaryStrip([
          {
            label: "已持久化报告",
            value: formatNumber(performanceView.report_count ?? 0, 0),
            meta: listOrDash(Object.entries(performanceView.status_counts || {}).map(([key, value]) => `${humanState(key)}:${value}`)),
            tone: "info",
          },
          {
            label: "平均短窗净收益差",
            value: signedOrFallback(performanceView.trend?.avg_short_net_pnl_delta, 4, "暂未形成结论"),
            meta: "基于最近 3 个窗口",
            tone: Number(performanceView.trend?.avg_short_net_pnl_delta || 0) >= 0 ? "positive" : "warning",
          },
          {
            label: "平均中窗净收益差",
            value: signedOrFallback(performanceView.trend?.avg_medium_net_pnl_delta, 4, "暂未形成结论"),
            meta: "基于最近 5 个窗口",
            tone: Number(performanceView.trend?.avg_medium_net_pnl_delta || 0) >= 0 ? "positive" : "warning",
          },
          {
            label: "回放健康率",
            value: formatNumber(performanceView.replay_context?.healthy_rate ?? 0, 3),
            meta: `验证 ${formatNumber(performanceView.replay_context?.validation_count ?? 0, 0)} 次`,
            tone: Number(performanceView.replay_context?.healthy_rate || 0) >= 0.8 ? "positive" : "warning",
          },
        ])}
        ${responsiveTable(
          ["记录时间", "状态", "净收益差", "复核状态", "回放上下文"],
          (performanceView.recent_reports || []).map((item) => [
            `<div><strong>${formatRelativeAge(item.generated_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.generated_at)}</div></div>`,
            pill(humanState(item.latest_status || "insufficient_data"), item.review_required ? "warning" : item.latest_status === "healthy" ? "positive" : "outline"),
            `<div><strong>${formatSigned(item.windows?.short?.net_pnl_delta_total ?? 0, 4)}</strong><div class="table-meta">中窗 ${formatSigned(item.windows?.medium?.net_pnl_delta_total ?? 0, 4)} / 长窗 ${formatSigned(item.windows?.long?.net_pnl_delta_total ?? 0, 4)}</div></div>`,
            `<div><strong>${item.review_required ? "需要复核" : "未触发"}</strong><div class="table-meta">短窗 ${formatNumber(item.windows?.short?.review_required_count ?? 0, 0)} / 中窗 ${formatNumber(item.windows?.medium?.review_required_count ?? 0, 0)} / 长窗 ${formatNumber(item.windows?.long?.review_required_count ?? 0, 0)}</div></div>`,
            `<div><strong>${formatMaybeTimestamp(performanceView.replay_context?.latest_validation?.validated_at)}</strong><div class="table-meta">偏差 ${formatNumber(performanceView.replay_context?.latest_validation?.divergence_count ?? 0, 0)}</div></div>`,
          ]),
          "当前暂无持久化的 AI 长周期表现报告。",
          performanceReportCards(performanceView.recent_reports || [], performanceView.replay_context || {})
        )}
      `,
    }),
  };
}

export function renderAIView(data) {
  const sections = renderAISections(data);
  return `
    <div class="panel-grid">
      <div class="span-12">${sections.aiHero}</div>
      ${sections.aiReview ? `<div class="span-12">${sections.aiReview}</div>` : ""}
      <div class="span-7">${sections.aiLatest}</div>
      <div class="span-5">${sections.aiExecutionSuggestion}</div>
      <div class="span-12">${sections.aiPerformanceReports}</div>
      <div class="span-12">${sections.aiHistory}</div>
    </div>
  `;
}

function renderReviewActions(blocker) {
  const actions = Array.isArray(blocker?.actions) ? blocker.actions : [];
  if (!actions.length) return "";
  return `
    <div class="stack-actions">
      ${actions.map((action) => {
        if (action.kind === "client") {
          return actionButton(
            action.label,
            action.client_action || "refresh-dashboard",
            action.value || "",
            action.tone || "ghost",
            {
              disabled: action.enabled === false,
              title: action.disabled_reason || action.expected_effect || "",
            },
          );
        }
        return actionButton(
          action.label,
          "trigger-blocker-action",
          `${action.action_id}::${blocker.blocker}`,
          action.tone || "secondary",
          {
            disabled: action.enabled === false,
            title: action.disabled_reason || action.expected_effect || "",
          },
        );
      }).join("")}
    </div>
  `;
}
