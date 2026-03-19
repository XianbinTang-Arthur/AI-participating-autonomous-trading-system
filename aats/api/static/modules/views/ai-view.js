import { actionButton, callout, kvList, pill, responsiveTable, statGrid, summaryStrip, surfaceCard } from "../components.js";
import { formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, listOrDash } from "../formatters.js";
import { localizeError, readableState } from "../terms.js";

const AI_STATE_MAP = {
  baseline_only: "仅按基础策略运行",
  ai_advisory: "AI 参与评估",
  ai_blended: "AI 一致性过滤",
  ai_primary: "AI 主导方向",
  ai_primary_shadow: "AI 影子主导",
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
  normal: "正常",
  maker_bias: "偏被动",
  taker_bias: "偏主动",
  bounded_limit_ioc: "受限限价成交",
  bounded_taker_cap: "受限主动成交",
  not_requested: "未请求",
};

const AI_ERROR_MAP = {
  ai_degraded_requires_manual_review: "AI 已降级且未开启自动回退，需要人工确认后再恢复 AI 主链。",
  ai_auto_downgraded: "AI 已自动降级，当前只保留基础策略主链。",
  output_rejected: "AI 输出结构有效，但没有通过交易语义校验。",
  ai_fallback_used: "本轮使用了回退结果，不能让 AI 接管。",
  ai_output_invalid: "AI 输出没有通过校验。",
  ai_confidence_below_threshold: "校准置信度低于 AI 主链最低门槛。",
  ai_uncertainty_above_threshold: "不确定性高于 AI 主链允许阈值。",
  ai_directional_edge_too_small: "方向边际不足，不能接管。",
  ai_override_not_recommended: "AI 自己都不建议覆盖基础策略。",
  ai_not_economically_actionable: "预期净边际覆盖不了成本和噪声。",
  ai_regime_not_allowed: "当前市场状态不允许 AI 直接接管。",
  ai_open_orders_present: "当前还有活动委托，不允许 AI 改写方向。",
  ai_flat_context_requires_stronger_edge: "空仓场景下需要更强的方向边际才能开仓。",
  execution_parameter_suggestions_disabled: "执行建议功能当前关闭。",
  diagnostic_only_no_live_execution: "当前只记录建议，不允许进入真实执行。",
  shadow_translation_preview_only: "当前只生成影子翻译结果，不改写真实委托。",
  planner_boundary_disabled: "执行器边界关闭了 AI 建议下探。",
  planner_recorded_suggestion_only: "执行器只保留建议供诊断使用。",
  planner_translated_execution_preview: "执行器已生成影子翻译预览。",
  bounded_live_translation_applied: "执行器已经把建议限制性地转成真实下单字段。",
  live_translation_not_enabled: "当前没有启用真实执行放权。",
  live_translation_requires_limit_cap: "只有能转成价格保护型限价保护的建议才允许实盘放权。",
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

function toneForRuntime(runtime) {
  if (runtime?.effective_operating_mode === "baseline_only" && runtime?.configured_operating_mode !== "baseline_only") {
    return "warning";
  }
  if (runtime?.degraded) return "warning";
  if (runtime?.effective_operating_mode === "baseline_only") return "outline";
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
    return "当前还没有拿到 AI 主链运行态，可能是页面刚加载完，或当前配置没有启用 AI。";
  }
  if (runtime.effective_operating_mode === "baseline_only" && runtime.configured_operating_mode !== "baseline_only") {
    return `AI 当前没有真实参与下单主链。最近一次降级原因：${humanError(runtime.degradation_reason || latestDegradation?.reason_code)}。`;
  }
  if (runtime.effective_operating_mode === "baseline_only") {
    return "当前配置本来就是仅基础策略模式。AI 不参与真实方向接管，但仍会保留诊断和影子数据。";
  }
  return "AI 当前仍在主链里，但是否真的接管基础策略，还要同时满足覆盖建议、经济可行动性、风控和执行状态等门禁。";
}

function economicGateRows(latestAssessment, latestTakeover) {
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
      "接管门禁",
      latestTakeover?.ai_takeover_applied ? "AI 已接管" : latestTakeover?.ai_takeover_allowed ? "允许接管但未应用" : "未通过接管门禁",
      listOrDash((latestTakeover?.ai_takeover_blockers || latestAssessment.rejection_flags || []).map(humanError)),
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
    : Array.isArray(payload?.takeovers)
      ? payload.takeovers.length
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

function takeoverAuditCards(takeovers, takeoverSummary) {
  return takeovers.map((item) => ({
    kicker: "AI 接管审计",
    title: `${humanState(item.final_direction || "unknown")} | ${formatRelativeAge(item.created_at)}`,
    meta: formatMaybeTimestamp(item.created_at),
    tone: item.ai_takeover_applied ? "positive" : item.ai_takeover_allowed ? "info" : "warning",
    badge: item.ai_takeover_applied
      ? pill("AI 已接管", "positive")
      : item.ai_takeover_allowed
        ? pill("允许接管但未应用", "info")
        : pill("接管被阻断", "warning"),
    fields: [
      { label: "基础策略 / AI", value: `${humanState(item.baseline_direction)} / ${humanState(item.ai_direction)}` },
      { label: "阻断项", value: readableList((item.ai_takeover_blockers || []).map(humanError), "当前没有阻断项") },
      { label: "方向分歧", value: item.direction_disagreement ? "AI 与基础策略分歧" : "方向一致" },
    ],
    details: [
      { label: "最终方向", value: humanState(item.final_direction || "unknown") },
      {
        label: "高频阻断项",
        value: readableList((takeoverSummary.top_blockers || []).slice(0, 3).map((row) => `${humanError(row.blocker)} x${row.count}`), "暂无高频阻断项"),
      },
    ],
    detailLabel: "展开接管审计",
  }));
}

function assessmentCards(assessments) {
  return assessments.map((item) => ({
    kicker: "AI 判断记录",
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
    kicker: "影子动作",
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
    kicker: "影子收益评估",
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
    kicker: "长期表现报告",
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
  const overview = data.aiOverview || {};
  const performanceView = overview.performance_view || {};
  const runtime = overview.runtime || data.aiRuntime || {};
  const latest = data.aiLatest || {};
  const recentPayload = data.aiRecent || {};
  const recentAssessments = recentPayload.assessments || [];
  const latestAssessment = overview.latest_assessment || latest.assessment || null;
  const latestTakeover = overview.latest_takeover || latest.takeover || null;
  const latestDegradation = overview.latest_degradation || null;
  const latestShadowDecision = overview.latest_shadow_decision || data.aiShadowLatest?.shadow_decision || null;
  const takeoversPayload = data.aiTakeoversRecent || { takeovers: [] };
  const takeovers = takeoversPayload.takeovers || [];
  const shadowRecentPayload = data.aiShadowRecent || {};
  const shadowRecent = shadowRecentPayload.shadow_decisions || [];
  const evaluationsPayload = data.aiShadowEvaluations || {};
  const evaluations = evaluationsPayload.evaluations || [];
  const takeoverSummary = overview.takeover_summary || {};
  const shadowSummary = overview.shadow_summary || {};
  const performanceWindows = overview.performance_windows || {};
  const downgradeState = overview.downgrade_state || {};
  const executionSuggestion = overview.latest_execution_suggestion || latest.execution_suggestion || {};

  return {
    aiHero: surfaceCard({
      title: "AI 主链当前状态",
      kicker: "先回答 AI 现在到底有没有真实参与",
      copy: "这里集中看配置模式、当前有效模式、降级状态、接管频率和影子回放表现，不用再翻原始记录。",
      actions: actionButton("立即生成影子收益回放", "evaluate-ai-shadow", "", "secondary"),
      classes: "hero-card",
      content: `
        ${callout({
          title:
            runtime.effective_operating_mode === "baseline_only"
              ? "AI 当前没有进入真实交易主链"
              : `AI 当前有效模式：${humanState(runtime.effective_operating_mode || runtime.configured_operating_mode || "unknown")}`,
          copy: aiRuntimeNarrative(runtime, latestDegradation),
          pills: [
            pill(`配置模式 ${humanState(runtime.configured_operating_mode || "unknown")}`, "info"),
            pill(`当前有效模式 ${humanState(runtime.effective_operating_mode || "unknown")}`, toneForRuntime(runtime)),
            pill(`模型服务 ${runtime.provider_ready ? "已就绪" : "未就绪"}`, runtime.provider_ready ? "positive" : "warning"),
            pill(`影子回放 ${runtime.shadow_mode_enabled ? "已开启" : "未开启"}`, runtime.shadow_mode_enabled ? "info" : "outline"),
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
            label: "接管应用率",
            value: formatNumber(takeoverSummary.applied_rate ?? 0, 3),
            meta: `尝试 ${formatNumber(takeoverSummary.attempted_count ?? 0, 0)} / 应用 ${formatNumber(takeoverSummary.applied_count ?? 0, 0)}`,
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
      title: "最新判断与门禁",
      kicker: "最近一次 AI 看到什么、想做什么、为什么没接管",
      copy: "重点看最新 AI 判断、经济可行动性、接管阻断项和最新影子动作。",
      content: latestAssessment
        ? `
            ${summaryStrip([
              {
                label: "市场结论",
                value: humanState(latestAssessment.regime || "unknown"),
                meta: `方向优势 ${formatNumber(latestAssessment.directional_edge ?? 0, 2)}`,
                tone: "info",
              },
              {
                label: "是否建议改写基础策略",
                value: latestAssessment.baseline_override_recommended ? "建议覆盖" : "不建议覆盖",
                meta: listOrDash(latestAssessment.override_reason_codes),
                tone: latestAssessment.baseline_override_recommended ? "warning" : "positive",
              },
              {
                label: "经济可行动性",
                value: latestAssessment.economically_actionable ? "满足交易条件" : "净边际不足",
                meta: `净边际 ${basisPoints(latestAssessment.estimated_net_edge_bps, 2)}`,
                tone: latestAssessment.economically_actionable ? "positive" : "warning",
              },
              {
                label: "是否已接管",
                value: latestTakeover?.ai_takeover_applied ? "AI 已接管" : "AI 未接管",
                meta: formatMaybeTimestamp(latestAssessment.created_at),
                tone: latestTakeover?.ai_takeover_applied ? "positive" : "warning",
              },
            ])}
            ${kvList([
              ["判断时间", formatMaybeTimestamp(latestAssessment.created_at), formatRelativeAge(latestAssessment.created_at)],
              ["接管阻断项", listOrDash((latestTakeover?.ai_takeover_blockers || latestAssessment.rejection_flags || []).map(humanError)), latestTakeover?.ai_takeover_allowed ? "通过基础接管门禁" : "未通过接管门禁"],
              ["最新影子动作", humanState(latestShadowDecision?.ai_shadow_action || "unknown"), latestShadowDecision ? `相对基础策略：${humanState(latestShadowDecision.shadow_action_type)}` : "最近还没有影子动作"],
            ])}
            ${surfaceCard({
              title: "AI 经济可行动性",
              kicker: "方向对不代表值得交易",
              copy: "这里把预期优势、成本、净优势和接管阻断拆开看。",
              classes: "is-muted",
              content: kvList(economicGateRows(latestAssessment, latestTakeover)),
            })}
          `
        : callout({
            title: "最近没有新的 AI 判断",
            copy: "当前多半仍在仅基础策略模式，或者模型服务处于降级后的自动回退状态。",
            pills: [pill(`当前有效模式 ${humanState(runtime.effective_operating_mode || "unknown")}`, "outline")],
          }),
    }),
    aiExecutionSuggestion: surfaceCard({
      title: "受限执行建议",
      kicker: "AI 可提建议，但执行器当前只做受限翻译和诊断",
      copy: "这里集中看建议模式、翻译器状态、影子预览，以及哪些字段被裁剪或拒绝。",
      classes: "ai-side-panel",
      content: kvList(executionSuggestionRows(executionSuggestion)),
    }),
    aiHistory: surfaceCard({
      title: "接管审计与影子对比",
      kicker: "按接管、判断、影子动作、回放四层看 AI 是否真的有效",
      copy: "先看接管尝试，再看为什么被阻断，最后看影子回放是否比基础策略更好。",
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
                label: "接管允许率",
                value: formatNumber(takeoverSummary.allowed_rate ?? 0, 3),
                meta: `允许 ${formatNumber(takeoverSummary.allowed_count ?? 0, 0)} / 尝试 ${formatNumber(takeoverSummary.attempted_count ?? 0, 0)}`,
                tone: "info",
              },
              {
                label: "接管应用率",
                value: formatNumber(takeoverSummary.applied_rate ?? 0, 3),
                meta: `应用 ${formatNumber(takeoverSummary.applied_count ?? 0, 0)} / 阻断 ${formatNumber(takeoverSummary.blocked_count ?? 0, 0)}`,
                tone: takeoverSummary.applied_count ? "positive" : "warning",
              },
              {
                label: "方向分歧率",
                value: formatNumber(takeoverSummary.disagreement_rate ?? 0, 3),
                meta: `分歧 ${formatNumber(takeoverSummary.disagreement_count ?? 0, 0)} 次`,
                tone: "warning",
              },
              {
                label: "影子状态",
                value: humanState(shadowSummary.status || "insufficient_data"),
                meta: shadowSummary.review_required ? "需要人工复核" : "当前未触发结果复核",
                tone: toneForShadowSummary(shadowSummary),
              },
            ])}
          </div>

          <div class="span-12">
            ${responsiveTable(
              ["时间", "基础策略 / AI / 最终", "接管门禁", "阻断项", "分歧"],
              takeovers.map((item) => [
                `<div><strong>${formatRelativeAge(item.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.created_at)}</div></div>`,
                `<div><strong>${humanState(item.baseline_direction)}</strong><div class="table-meta">AI ${humanState(item.ai_direction)} / 最终 ${humanState(item.final_direction)}</div></div>`,
                item.ai_takeover_applied
                  ? pill("AI 已接管", "positive")
                  : item.ai_takeover_allowed
                    ? pill("允许接管但未应用", "info")
                    : pill("接管被阻断", "warning"),
                `<div><strong>${listOrDash((item.ai_takeover_blockers || []).map(humanError))}</strong><div class="table-meta">${listOrDash((takeoverSummary.top_blockers || []).slice(0, 3).map((row) => `${humanError(row.blocker)} x${row.count}`))}</div></div>`,
                item.direction_disagreement ? pill("AI 与基础策略分歧", "warning") : pill("方向一致", "outline"),
              ]),
              "最近还没有 AI 接管审计记录。",
              takeoverAuditCards(takeovers, takeoverSummary)
            )}
            ${renderPaginationFooter(takeoversPayload, {
              key: "AI 接管记录",
              loadAction: "load-more-ai-takeovers",
              collapseAction: "collapse-ai-takeovers",
            })}
          </div>

          <div class="span-12">
            ${responsiveTable(
              ["时间", "市场判断", "是否建议覆盖", "经济可行动性", "结果"],
              recentAssessments.map((item) => [
                `<div><strong>${formatRelativeAge(item.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.created_at)}</div></div>`,
                `<div><strong>${humanState(item.regime || "unknown")}</strong><div class="table-meta">方向优势 ${formatNumber(item.directional_edge ?? 0, 2)}</div></div>`,
                `<div><strong>${item.baseline_override_recommended ? "建议改写基础策略" : "不建议改写"}</strong><div class="table-meta">${listOrDash(item.override_reason_codes)}</div></div>`,
                `<div><strong>${item.economically_actionable ? "可交易" : "不建议交易"}</strong><div class="table-meta">净边际 ${basisPoints(item.estimated_net_edge_bps, 2)}</div></div>`,
                `<div><strong>${item.fallback_used ? "使用回退结果" : "使用模型结果"}</strong><div class="table-meta">${listOrDash((item.rejection_flags || item.validation_flags || []).map(humanError))}</div></div>`,
              ]),
              "最近还没有 AI 判断记录。",
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
              ["时间", "基础策略动作", "影子动作", "是否改写基础策略", "动作差异"],
              shadowRecent.map((item) => [
                `<div><strong>${formatRelativeAge(item.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.created_at)}</div></div>`,
                `<div><strong>${humanState(item.baseline_action || "unknown")}</strong><div class="table-meta">目标 ${formatNumber(item.baseline_target_qty ?? 0)}</div></div>`,
                `<div><strong>${humanState(item.ai_shadow_action || "unknown")}</strong><div class="table-meta">目标 ${formatNumber(item.ai_shadow_target_qty ?? 0)}</div></div>`,
                item.would_override_baseline ? pill("会改写基础策略", "warning") : pill("与基础策略一致", "positive"),
                `<div><strong>${humanState(item.shadow_action_type || "unknown")}</strong><div class="table-meta">${listOrDash(item.reason_codes)}</div></div>`,
              ]),
              "最近还没有影子动作记录。",
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
              {
                label: "结果复核",
                value: shadowSummary.review_required ? "需要人工复核" : "当前未触发",
                meta: `窗口状态 ${humanState(shadowSummary.status || "insufficient_data")}`,
                tone: shadowSummary.review_required ? "danger" : toneForShadowSummary(shadowSummary),
              },
            ])}
            ${responsiveTable(
              ["评估窗口", "基础策略回放", "影子回放", "成本与来回交易", "结论"],
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
              "最近还没有影子收益评估。",
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
      title: "长周期表现报告",
      kicker: "持久化表现报告、趋势和回放约束",
      copy: "这里不再只看单次影子评估，而是看已经持久化的长期表现序列，以及最近回放健康度对 AI 主链的约束。",
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
          ["生成时间", "状态", "短窗 / 中窗 / 长窗净收益差", "复核", "回放上下文"],
          (performanceView.recent_reports || []).map((item) => [
            `<div><strong>${formatRelativeAge(item.generated_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.generated_at)}</div></div>`,
            pill(humanState(item.latest_status || "insufficient_data"), item.review_required ? "warning" : item.latest_status === "healthy" ? "positive" : "outline"),
            `<div><strong>${formatSigned(item.windows?.short?.net_pnl_delta_total ?? 0, 4)}</strong><div class="table-meta">中窗 ${formatSigned(item.windows?.medium?.net_pnl_delta_total ?? 0, 4)} / 长窗 ${formatSigned(item.windows?.long?.net_pnl_delta_total ?? 0, 4)}</div></div>`,
            `<div><strong>${item.review_required ? "需要复核" : "未触发"}</strong><div class="table-meta">短窗 ${formatNumber(item.windows?.short?.review_required_count ?? 0, 0)} / 中窗 ${formatNumber(item.windows?.medium?.review_required_count ?? 0, 0)} / 长窗 ${formatNumber(item.windows?.long?.review_required_count ?? 0, 0)}</div></div>`,
            `<div><strong>${formatMaybeTimestamp(performanceView.replay_context?.latest_validation?.validated_at)}</strong><div class="table-meta">偏差 ${formatNumber(performanceView.replay_context?.latest_validation?.divergence_count ?? 0, 0)}</div></div>`,
          ]),
          "当前还没有持久化的 AI 长周期表现报告。",
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
      <div class="span-7">${sections.aiLatest}</div>
      <div class="span-5">${sections.aiExecutionSuggestion}</div>
      <div class="span-12">${sections.aiPerformanceReports}</div>
      <div class="span-12">${sections.aiHistory}</div>
    </div>
  `;
}
