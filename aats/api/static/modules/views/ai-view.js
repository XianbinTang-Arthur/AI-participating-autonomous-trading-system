import { actionButton, actorTags, callout, kvList, pill, responsiveTable, statGrid, summaryStrip, surfaceCard } from "../components.js";
import { hasMeaningfulValue, localizeList, meaningfulEntries, splitCodeList, summarizeLocalizedList, textOrFallback } from "../copy.js";
import { formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned } from "../formatters.js";
import { localizeError, readableState } from "../terms.js";

const AI_STATE_MAP = {
  baseline_only: "仅按基础策略运行",
  ai_assisted: "AI 辅助决策",
  ai_decision_maker: "AI 决策者",
  ai_decision_maker_with_profile_control: "AI 决策者并控制策略档位",
  disabled: "已关闭",
  diagnostic_only: "仅诊断",
  shadow_translation: "执行层 shadow",
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
  baseline_fallback: "本轮回退为基础策略",
  admin_override: "管理员覆盖",
  env_default: "沿用启动默认档位",
  admin: "管理员手动覆盖",
  system: "系统自动决定保持当前档位",
  reference_only: "基础策略主导",
  advisory: "AI 辅助建议",
  final_decision: "AI 最终决策",
  final_decision_with_profile_control: "AI 最终决策并可联动切档",
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
  operator_manual_ai_mode_override: "管理员已手动覆盖当前 AI 运行模式。",
  operator_manual_ai_mode_override_expired: "人工覆盖冻结时间已结束，系统恢复为自动运行模式逻辑。",
  operator_manual_ai_mode_override_cleared: "管理员已提前结束人工覆盖，系统恢复为自动运行模式逻辑。",
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
  shadow_translation_preview_only: "当前只生成执行层 shadow 预演，不改写真委托。",
  planner_boundary_disabled: "执行器边界关闭了 AI 建议下探。",
  planner_recorded_suggestion_only: "执行器只保留建议供诊断使用。",
  planner_translated_execution_preview: "执行器已生成执行层 shadow 预演。",
  bounded_live_translation_applied: "执行器已经把建议限制性地转成真实下单字段。",
  live_translation_not_enabled: "当前没有启用实盘授权。",
  live_translation_requires_limit_cap: "只有能转成价格保护型限价保护的建议才允许进入实盘授权。",
  live_translation_requires_reference_price: "缺少参考价格，不能安全生成实盘价格保护。",
  live_translation_requires_limit_offset: "缺少有效价格偏移，不能生成实盘限价保护。",
  live_translation_requires_slippage_guard: "缺少滑点保护，不能启用受限实盘翻译。",
};

const EXECUTION_SUGGESTION_LABELS = {
  passive_bias: "被动倾向",
  maker_taker_bias: "主被动偏向",
  slice_count: "拆单数",
  max_participation_rate: "最大参与率",
  max_cross_spread_bps: "最大跨价差",
  cancel_replace_patience_ms: "撤改单等待",
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

function executionSuggestionLabel(key) {
  const normalized = String(key || "").trim();
  return EXECUTION_SUGGESTION_LABELS[normalized] || normalized;
}

function activeDegradationReasons(downgradeState = {}) {
  const reasons = [];
  if (downgradeState.provider_state && downgradeState.provider_state !== "healthy" && hasMeaningfulValue(downgradeState.degradation_reason)) {
    reasons.push(downgradeState.degradation_reason);
  }
  if (downgradeState.outcome_state && downgradeState.outcome_state !== "healthy" && hasMeaningfulValue(downgradeState.outcome_degradation_reason)) {
    reasons.push(downgradeState.outcome_degradation_reason);
  }
  return reasons;
}

function reviewResolutionSummary(runtime = {}, latestDegradation = null) {
  const resolution = runtime.review_resolution || latestDegradation?.review_resolution || latestDegradation?.reason_code;
  if (!hasMeaningfulValue(resolution)) return null;
  return humanError(resolution);
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
  if (runtime?.operating_mode_source === "manual_selection") return "warning";
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

function readableShadowMeta(shadowSummary = {}) {
  if (!shadowSummary?.window_count) {
    return "当前样本不足，暂时还没有形成稳定的策略层 shadow 结论。";
  }
  return `窗口 ${formatNumber(shadowSummary.window_count ?? 0, 0)} / 状态 ${humanState(shadowSummary.status || "insufficient_data")} / 跑赢率 ${formatNumber(shadowSummary.outperformed_rate ?? 0, 3)}`;
}

function decisionSourceSummary(latestOutcome) {
  if (!latestOutcome) return "当前暂无统一决策结果。";
  if (latestOutcome.decision_source === "ai") return "当前由 AI 直接给出最终决策。";
  if (latestOutcome.decision_source === "baseline_fallback") return "AI 已给出建议，但系统自动决定回退到基础策略。";
  if (latestOutcome.decision_source === "admin_override") return "当前由管理员手动覆盖决策链路。";
  return "当前由系统自动决定继续按基础策略执行。";
}

function profileControlSummary(latestOutcome, latestProfileControl) {
  if (latestProfileControl?.applied) {
    return {
      value: `已切到 ${humanState(latestProfileControl.requested_profile_id || "unknown")}`,
      meta: "本轮真实决策链路已完成策略档位切换。",
    };
  }
  if (latestProfileControl?.blocked_reasons?.length) {
    return {
      value: "已评估，未执行",
      meta: localizeList(latestProfileControl.blocked_reasons, "当前没有新的自动切换阻断原因。"),
    };
  }
  if (latestOutcome?.profile_control_source === "admin") {
    return { value: "管理员手动覆盖并保持当前策略档位", meta: "当前策略档位由管理员手动覆盖保持。" };
  }
  if (latestOutcome?.profile_control_source === "system") {
    return { value: "系统自动决定保持当前策略档位", meta: "本轮没有新的策略档位切换动作。" };
  }
  return {
    value: humanState(latestOutcome?.profile_control_source || "env_default"),
    meta: "本轮没有新的策略档位切换动作。",
  };
}

function latestDecisionCallout(latestOutcome, latestAssessment, latestProfileControl) {
  const decisionValue = humanState(latestOutcome?.decision_source || "baseline");
  const blocker = blockerSummary(latestOutcome, latestAssessment);
  const profileControl = profileControlSummary(latestOutcome, latestProfileControl);
  const title = latestOutcome?.decision_source === "ai"
    ? "本轮由 AI 直接给出最终决策"
    : latestOutcome?.decision_source === "baseline_fallback"
      ? "系统自动决定回退到基础策略"
      : "本轮仍由基础策略主导";
  const copy = latestOutcome?.decision_source === "ai"
    ? `AI 这轮已经通过最终门禁，真实动作会按 AI 决策链路落地。当前档位控制状态：${profileControl.value}。`
    : latestOutcome?.decision_source === "baseline_fallback"
      ? `AI 这轮给出了建议，但没有通过最终门禁，所以系统自动决定回退到基础策略。主要原因：${blocker.value}。`
      : `当前还没有形成新的 AI 最终决策，所以系统自动决定继续沿用基础策略链路。当前档位控制状态：${profileControl.value}。`;
  return callout({
    title,
    copy,
    pills: [
      actorTags(
        latestOutcome?.decision_source === "ai"
          ? "ai"
          : latestOutcome?.decision_source === "admin_override"
            ? "admin"
            : "system",
      ),
      pill(`最终来源 ${decisionValue}`, latestOutcome?.decision_source === "ai" ? "positive" : "warning"),
    ],
  });
}

function reviewCallout(aiReviewBlocker, latestDegradation) {
  const title = textOrFallback(aiReviewBlocker?.recommended_next_step, "请先完成这次人工复核。");
  const copy = latestDegradation?.reason_code
    ? `当前 AI 决策链路被复核流程拦住，主要原因是：${humanError(latestDegradation.reason_code)}。处理完这次复核后，系统才会决定恢复 AI 还是继续只用基础策略。`
    : "当前 AI 决策链路被复核流程拦住。处理完这次复核后，系统才会决定恢复 AI 还是继续只用基础策略。";
  return callout({
    title,
    copy,
    pills: [actorTags("admin", "system"), pill("人工动作", "warning")],
  });
}

function executionSuggestionCallout(summary = {}) {
  const latest = summary.latest_translation || {};
  if (latest.applied_to_live_execution) {
    return callout({
      title: "这轮执行建议已经进入受限实盘翻译",
      copy: "系统已经把建议限制性地翻译成真实下单字段，但仍保留价格保护和滑点保护，不会把建议原样裸奔下到市场。",
      pills: [pill("已受限落地", "positive")],
    });
  }
  if (summary.translation_present) {
    return callout({
      title: "这轮已经生成执行层 shadow 预演",
      copy: "系统已经算出了更接近真实下单的执行建议，但目前还停留在执行层 shadow 预演或诊断层，没有直接改写真实委托。",
      pills: [pill("仅诊断/预览", "info")],
    });
  }
  return callout({
    title: "当前没有新的执行建议需要关注",
    copy: "没有新的翻译结果时，说明这轮没有值得下钻的执行层差异。下面保留的是最近一次有意义的执行建议细节。",
    pills: [pill("暂无新增", "outline")],
  });
}

function historyCallout(shadowSummary = {}, performanceWindows = {}) {
  const title = shadowSummary.review_required
    ? "策略层 shadow 近期需要人工复核"
    : shadowSummary.window_count
      ? "策略层 shadow 已经形成一组可比较样本"
      : "策略层 shadow 样本还不够";
  const copy = shadowSummary.review_required
    ? "最近策略层 shadow 已经触发复核条件，后续要重点看它到底是净收益差转负、手续费拖累升高，还是来回交易过多。这里所有“窗口”都表示最近一组评估样本，不是自然日。"
    : shadowSummary.window_count
      ? `最近 ${formatNumber(shadowSummary.window_count, 0)} 个窗口里，策略层 shadow 相对基础策略的短窗净收益差为 ${signedOrFallback(performanceWindows.short?.net_pnl_delta_total, 4, "待同步")}。净收益差正数通常更好；手续费拖累差和来回交易差则相反，越低越好。`
      : "当前样本还不够，适合先看趋势，不适合据此下强结论。这里的窗口表示最近几组评估样本，不是自然日。";
  return callout({
    title,
    copy,
    pills: [pill(`策略层 shadow 状态 ${humanState(shadowSummary.status || "insufficient_data")}`, toneForShadowSummary(shadowSummary))],
  });
}

function performanceCallout(performanceView = {}) {
  const statusCounts = Object.keys(performanceView.status_counts || {}).length
    ? Object.entries(performanceView.status_counts || {}).map(([key, value]) => `${humanState(key)} ${value} 条`).join("、")
    : "当前暂无已持久化状态统计。";
  return callout({
    title: performanceView.report_count ? "长期表现报告已经开始积累" : "当前还没有长期表现报告",
    copy: performanceView.report_count
      ? `系统目前已经持久化 ${formatNumber(performanceView.report_count, 0)} 条长期表现报告。这里更适合看趋势是否稳定，而不是拿单次窗口结果下判断。收益差正数通常代表策略层 shadow 更好；回放健康率越接近 1 越稳定。`
      : "只有当长周期表现开始稳定积累后，这里的长期报告才真正有参考价值。这里的窗口表示最近几组评估样本，不是自然日。",
    pills: [pill(statusCounts, "info")],
  });
}

export function renderAIAnalysisSectionCards(data) {
  const aiOverview = data.aiOverview || {};
  const aiLatest = data.aiLatest || {};
  const aiRecent = data.aiRecent || {};
  const aiShadowRecent = data.aiShadowRecent || {};
  const aiShadowEvaluations = data.aiShadowEvaluations || {};
  const executionSuggestion = aiOverview.latest_execution_suggestion || aiLatest.execution_suggestion || {};
  const recentPayload = aiRecent || {};
  const recentAssessments = recentPayload.assessments || [];
  const shadowRecentPayload = aiShadowRecent || {};
  const shadowRecent = shadowRecentPayload.shadow_decisions || [];
  const evaluationsPayload = aiShadowEvaluations || {};
  const evaluations = evaluationsPayload.evaluations || [];
  const shadowSummary = aiOverview.shadow_summary || {};
  const performanceView = aiOverview.performance_view || {};
  const performanceWindows = performanceView.windows || {};
  const recentReports = performanceView.recent_reports || [];
  const replayContext = performanceView.replay_context || {};
  const hasHistoryRecords = recentAssessments.length > 0 || shadowRecent.length > 0 || evaluations.length > 0;
  const hasPerformanceRecords =
    recentReports.length > 0
    || Number(performanceView.report_count ?? 0) > 0
    || Number(replayContext.validation_count ?? 0) > 0
    || Boolean(replayContext.latest_validation?.validated_at);

  return {
    aiExecutionSuggestion: surfaceCard({
      title: "执行层 shadow / 执行建议",
      kicker: "执行边界",
      copy: "只有存在真实建议或翻译结果时才显示，避免把默认值当成真实执行信号。",
      classes: "ai-side-panel",
      content: `
        ${executionSuggestionCallout(executionSuggestion)}
        ${kvList(executionSuggestionRows(executionSuggestion))}
      `,
    }),
    aiHistory: surfaceCard({
      title: "AI 记录",
      kicker: "历史记录",
      panelKey: "aiShadowEvaluations",
      copy: "这里集中看策略层 shadow 的动作记录和收益评估。",
      content: hasHistoryRecords
        ? `
            <div class="panel-grid">
              <div class="span-12">
                ${historyCallout(shadowSummary, performanceWindows)}
              </div>
              <div class="span-12">
                ${summaryStrip([
                  {
                    label: "近 3 窗口净收益差",
                    value: signedOrFallback(performanceWindows.short?.net_pnl_delta_total, 4, "暂未形成结论"),
                    meta: `跑赢率 ${formatNumber(performanceWindows.short?.outperformed_rate ?? 0, 3)} | 正数通常表示策略层 shadow 更好`,
                    tone: Number(performanceWindows.short?.net_pnl_delta_total || 0) >= 0 ? "positive" : "warning",
                  },
                  {
                    label: "近 5 窗口净收益差",
                    value: signedOrFallback(performanceWindows.medium?.net_pnl_delta_total, 4, "暂未形成结论"),
                    meta: `跑赢率 ${formatNumber(performanceWindows.medium?.outperformed_rate ?? 0, 3)} | 正数通常表示策略层 shadow 更好`,
                    tone: Number(performanceWindows.medium?.net_pnl_delta_total || 0) >= 0 ? "positive" : "warning",
                  },
                  {
                    label: "近 10 窗口手续费拖累差",
                    value: signedOrFallback(performanceWindows.long?.avg_fee_ratio_delta, 4, "暂未形成结论"),
                    meta: `需复核 ${formatNumber(performanceWindows.long?.review_required_count ?? 0, 0)} 次 | 负数通常更好`,
                    tone: Number(performanceWindows.long?.avg_fee_ratio_delta || 0) <= 0 ? "positive" : "warning",
                  },
                  {
                    label: "近 10 窗口来回交易差",
                    value: signedOrFallback(performanceWindows.long?.avg_churn_ratio_delta, 4, "暂未形成结论"),
                    meta: `样本 ${formatNumber(performanceWindows.long?.sample_size ?? 0, 0)} | 负数通常更好`,
                    tone: Number(performanceWindows.long?.avg_churn_ratio_delta || 0) <= 0 ? "positive" : "warning",
                  },
                ])}
              </div>
              <div class="span-12">
                ${summaryStrip([
                  {
                    label: "策略层 shadow 状态",
                    value: humanState(shadowSummary.status || "insufficient_data"),
                    meta: shadowSummary.review_required ? "需要人工复核" : "当前未触发人工复核",
                    tone: toneForShadowSummary(shadowSummary),
                  },
                  {
                    label: "最新净收益差值",
                    value: signedOrFallback(shadowSummary.latest_net_pnl_delta, 4, "暂未形成结论"),
                    meta: "策略层 shadow 净收益 - 基础策略净收益，正数通常更好",
                    tone: Number(shadowSummary.latest_net_pnl_delta || 0) >= 0 ? "positive" : "warning",
                  },
                  {
                    label: "最新手续费拖累差值",
                    value: signedOrFallback(shadowSummary.latest_fee_ratio_delta, 4, "暂未形成结论"),
                    meta: "策略层 shadow 手续费比例 - 基础策略手续费比例，负数通常更好",
                    tone: Number(shadowSummary.latest_fee_ratio_delta || 0) <= 0 ? "positive" : "warning",
                  },
                  {
                    label: "最新来回交易差值",
                    value: signedOrFallback(shadowSummary.latest_churn_ratio_delta, 4, "暂未形成结论"),
                    meta: "策略层 shadow 来回交易比例 - 基础策略来回交易比例，负数通常更好",
                    tone: Number(shadowSummary.latest_churn_ratio_delta || 0) <= 0 ? "positive" : "warning",
                  },
                ])}
              </div>
              <div class="span-12">
                ${responsiveTable(
                  ["时间", "这轮怎么看市场", "AI 会不会改主策略", "值不值得做", "最终怎么处理"],
                  recentAssessments.map((item) => [
                    `<div><strong>${formatRelativeAge(item.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.created_at)}</div><div class="inline-pills">${actorTags(item.fallback_used ? "system" : "ai", item.fallback_used ? "ai" : null)}</div></div>`,
                    `<div><strong>${humanState(item.regime || "unknown")}</strong><div class="table-meta">方向优势 ${formatNumber(item.directional_edge ?? 0, 2)}</div></div>`,
                    `<div><strong>${item.baseline_override_recommended ? "建议改写基础策略" : "不建议改写"}</strong><div class="table-meta">${localizeList(item.override_reason_codes, "当前没有额外改写理由。")}</div></div>`,
                    `<div><strong>${item.economically_actionable ? "值得继续做" : "现在不值得做"}</strong><div class="table-meta">净边际 ${basisPoints(item.estimated_net_edge_bps, 2)}</div></div>`,
                    `<div><strong>${item.fallback_used ? "最终回退到基础策略" : "最终采用模型结果"}</strong><div class="table-meta">${localizeList(item.rejection_flags || item.validation_flags, "当前没有额外处理说明。")}</div></div>`,
                  ]),
                  "当前还没有可复盘的 AI 判断记录。",
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
                  ["时间", "基础策略会怎么做", "AI 影子会怎么做", "会不会真的改动", "主要差异"],
                  shadowRecent.map((item) => [
                    `<div><strong>${formatRelativeAge(item.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.created_at)}</div><div class="inline-pills">${actorTags("system", "ai")}</div></div>`,
                    `<div><strong>${humanState(item.baseline_action || "unknown")}</strong><div class="table-meta">目标 ${formatNumber(item.baseline_target_qty ?? 0)}</div></div>`,
                    `<div><strong>${humanState(item.ai_shadow_action || "unknown")}</strong><div class="table-meta">目标 ${formatNumber(item.ai_shadow_target_qty ?? 0)}</div></div>`,
                    item.would_override_baseline ? pill("会改写基础策略", "warning") : pill("与基础策略一致", "positive"),
                    `<div><strong>${humanState(item.shadow_action_type || "unknown")}</strong><div class="table-meta">${localizeList(item.reason_codes, "当前没有额外差异说明。")}</div></div>`,
                  ]),
                  "当前还没有可复盘的影子动作记录。",
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
                    label: "人工复核",
                    value: shadowSummary.review_required ? "需要人工复核" : "当前未触发",
                    meta: `窗口状态 ${humanState(shadowSummary.status || "insufficient_data")}`,
                    tone: shadowSummary.review_required ? "danger" : toneForShadowSummary(shadowSummary),
                  },
                ])}
                ${responsiveTable(
                  ["评估窗口", "基础策略结果", "AI 影子结果", "成本与来回交易", "这一窗谁更好"],
                  evaluations.map((item) => [
                    `<div><strong>${formatMaybeTimestamp(item.window_end)}</strong><div class="table-meta">${formatMaybeTimestamp(item.window_start)} ~ ${formatMaybeTimestamp(item.window_end)}</div><div class="inline-pills">${actorTags("system")}</div></div>`,
                    `<div><strong>净收益 ${formatNumber(item.baseline_net_pnl ?? 0)}</strong><div class="table-meta">毛收益 ${formatNumber(item.baseline_gross_pnl ?? 0)} / 成交 ${formatNumber(item.baseline_trade_count ?? 0, 0)} 笔</div></div>`,
                    `<div><strong>净收益 ${formatNumber(item.shadow_net_pnl ?? 0)}</strong><div class="table-meta">毛收益 ${formatNumber(item.shadow_gross_pnl ?? 0)} / 成交 ${formatNumber(item.shadow_trade_count ?? 0, 0)} 笔</div></div>`,
                    `<div><strong>手续费差值 ${formatSigned((Number(item.shadow_fee_ratio ?? 0) - Number(item.baseline_fee_ratio ?? 0)), 4)}</strong><div class="table-meta">来回交易差值 ${formatSigned((Number(item.shadow_churn_ratio ?? 0) - Number(item.baseline_churn_ratio ?? 0)), 4)}</div></div>`,
                    item.shadow_outperformed === null
                      ? pill("尚未得出结论", "outline")
                      : item.shadow_outperformed
                        ? pill("影子结果更优", "positive")
                        : pill("基础策略更优", "warning"),
                  ]),
                  "当前还没有影子收益对比结果。",
                  shadowEvaluationCards(evaluations)
                )}
                ${renderPaginationFooter(evaluationsPayload, {
                  key: "影子收益评估",
                  loadAction: "load-more-ai-shadow-evaluations",
                  collapseAction: "collapse-ai-shadow-evaluations",
                })}
              </div>
            </div>
          `
        : `
            ${callout({
              title: "当前暂无可复盘的 AI 历史记录",
              copy: "后端当前还没有 AI 判断、影子动作或收益对比样本。这不是断链，而是当前样本确实为 0。",
              pills: [
                pill(`AI 判断 ${formatNumber(recentPayload.total_available ?? recentAssessments.length, 0)} 条`, "outline"),
                pill(`影子动作 ${formatNumber(shadowRecentPayload.total_available ?? shadowRecent.length, 0)} 条`, "outline"),
                pill(`收益对比 ${formatNumber(evaluationsPayload.total_available ?? evaluations.length, 0)} 条`, "outline"),
              ],
            })}
          `,
    }),
    aiPerformanceReports: surfaceCard({
      title: "表现报告",
      kicker: "长期表现",
      copy: "这里不再只看单次影子评估，而是看已经持久化的长期表现序列，以及最近回放健康度对 AI 决策链路的约束。",
      content: hasPerformanceRecords
        ? `
            ${performanceCallout(performanceView)}
            ${summaryStrip([
              {
                label: "已持久化报告",
                value: formatNumber(performanceView.report_count ?? 0, 0),
                meta: Object.keys(performanceView.status_counts || {}).length
                  ? Object.entries(performanceView.status_counts || {}).map(([key, value]) => `${humanState(key)} ${value} 条`).join("、")
                  : "当前暂无已持久化状态统计。",
                tone: "info",
              },
              {
                label: "平均短窗净收益差",
                value: signedOrFallback(performanceView.trend?.avg_short_net_pnl_delta, 4, "暂未形成结论"),
                meta: "基于最近 3 个窗口，正数通常表示 AI 影子更好",
                tone: Number(performanceView.trend?.avg_short_net_pnl_delta || 0) >= 0 ? "positive" : "warning",
              },
              {
                label: "平均中窗净收益差",
                value: signedOrFallback(performanceView.trend?.avg_medium_net_pnl_delta, 4, "暂未形成结论"),
                meta: "基于最近 5 个窗口，正数通常表示 AI 影子更好",
                tone: Number(performanceView.trend?.avg_medium_net_pnl_delta || 0) >= 0 ? "positive" : "warning",
              },
              {
                label: "回放健康率",
                value: formatNumber(replayContext.healthy_rate ?? 0, 3),
                meta: `验证 ${formatNumber(replayContext.validation_count ?? 0, 0)} 次 | 越接近 1 越稳定`,
                tone: Number(replayContext.healthy_rate || 0) >= 0.8 ? "positive" : "warning",
              },
            ])}
            ${responsiveTable(
              ["时间", "整体状态", "收益差趋势", "是否需要人工复核", "回放健康度"],
              recentReports.map((item) => [
                `<div><strong>${formatRelativeAge(item.generated_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.generated_at)}</div><div class="inline-pills">${actorTags("system")}</div></div>`,
                pill(humanState(item.latest_status || "insufficient_data"), item.review_required ? "warning" : item.latest_status === "healthy" ? "positive" : "outline"),
                `<div><strong>${formatSigned(item.windows?.short?.net_pnl_delta_total ?? 0, 4)}</strong><div class="table-meta">中窗 ${formatSigned(item.windows?.medium?.net_pnl_delta_total ?? 0, 4)} / 长窗 ${formatSigned(item.windows?.long?.net_pnl_delta_total ?? 0, 4)}</div></div>`,
                `<div><strong>${item.review_required ? "需要复核" : "暂不需要"}</strong><div class="table-meta">短窗 ${formatNumber(item.windows?.short?.review_required_count ?? 0, 0)} / 中窗 ${formatNumber(item.windows?.medium?.review_required_count ?? 0, 0)} / 长窗 ${formatNumber(item.windows?.long?.review_required_count ?? 0, 0)}</div></div>`,
                `<div><strong>${formatMaybeTimestamp(replayContext.latest_validation?.validated_at)}</strong><div class="table-meta">最近回放偏差 ${formatNumber(replayContext.latest_validation?.divergence_count ?? 0, 0)}</div></div>`,
              ]),
              "当前暂无持久化的 AI 长周期表现报告。",
              performanceReportCards(recentReports, replayContext)
            )}
          `
        : `
            ${callout({
              title: "当前还没有长期表现报告",
              copy: "后端当前没有持久化的 AI 长周期表现，也没有新的回放验证结果，所以这里先不铺开长表。",
              pills: [
                pill(`已持久化报告 ${formatNumber(performanceView.report_count ?? 0, 0)} 条`, "outline"),
                pill(`回放验证 ${formatNumber(replayContext.validation_count ?? 0, 0)} 次`, "outline"),
              ],
            })}
          `,
    }),
  };
}

function aiRuntimeNarrative(runtime, latestDegradation) {
  if (!runtime || Object.keys(runtime).length === 0) {
    return "当前暂无 AI 决策链路运行状态，可能是页面刚加载完，或当前配置没有启用 AI。";
  }
  if (runtime.operating_mode_source === "manual_selection") {
    return `当前运行模式已手动切到 ${humanState(effectiveMode(runtime))}。配置默认仍是 ${humanState(configuredMode(runtime))}。`;
  }
  if (effectiveMode(runtime) === "baseline_only" && configuredMode(runtime) !== "baseline_only") {
    return `AI 当前没有真实参与下单决策链路。最近一次降级原因：${humanError(runtime.degradation_reason || latestDegradation?.reason_code)}。`;
  }
  if (runtime.review_resolution && latestDegradation?.reason_code === "operator_review_restore_ai") {
    return "当前运行模式已经恢复为 AI 决策链路。最近一次人工处理是确认恢复 AI 决策，但最近一轮真实决策结果是否采用 AI，仍取决于当轮门禁。";
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
      localizeList(latestAssessment.validation_flags, "当前没有额外校验限制。"),
    ],
    [
      "本轮决策来源",
      humanState(latestOutcome?.decision_source || "baseline"),
      localizeList(latestOutcome?.decision_blocked_reasons || latestAssessment.rejection_flags, "当前没有额外决策链路阻断。"),
    ],
    [
      "策略档位控制",
      profileControlSummary(latestOutcome, latestProfileControl).value,
      profileControlSummary(latestOutcome, latestProfileControl).meta,
    ],
  ];
}

function executionSuggestionRows(summary = {}) {
  const latest = summary.latest_translation || {};
  const preview = latest.translation_preview || {};
  const suggestion = latest.suggestion || summary.assessment_suggestion?.suggestion || {};
  const suggestionPairs = meaningfulEntries(suggestion).filter(([, value]) => {
    if (typeof value === "number") return value !== 0;
    return true;
  });
  const previewActive = hasMeaningfulValue(preview.order_type) || hasMeaningfulValue(preview.execution_style);
  const hasSuggestion = Boolean(
    latest.applied_to_live_execution
      || summary.translation_present
      || latest.order_type
      || previewActive
      || suggestionPairs.length
      || (latest.rejection_reasons || []).length
      || (latest.clipped_fields || []).length
      || (latest.notes || []).length
  );
  if (!hasSuggestion) return [];
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
      latest.applied_to_live_execution
        ? localizeList(latest.applied_live_fields, "当前没有额外实盘落地字段说明。")
        : humanError(latest.live_translation_fallback_reason),
    ],
    [
      "建议执行姿态",
      suggestionPairs.length
        ? suggestionPairs
          .slice(0, 2)
          .map(([key, value]) => `${executionSuggestionLabel(key)} ${formatNumber(value ?? 0, 2)}`)
          .join(" / ")
        : "当前没有新的执行姿态建议",
      suggestionPairs.length > 2
        ? suggestionPairs
          .slice(2, 4)
          .map(([key, value]) => `${executionSuggestionLabel(key)} ${formatNumber(value ?? 0, 2)}`)
          .join(" / ")
        : "当前没有额外拆单或参与率建议",
    ],
    [
      "执行层 shadow 预演",
      previewActive
        ? `${humanState(preview.execution_style || "not_requested")} / ${humanState(preview.order_type || "not_requested")}`
        : "当前没有新的翻译预览",
      previewActive
        ? `${textOrFallback(preview.time_in_force, "时效待确认")} / 价格偏移 ${basisPoints(preview.limit_offset_bps, 2)} / 实盘限价 ${formatNumber(summary.live_limit_price ?? 0, 2)}`
        : "当前没有新的执行层 shadow 预演结果。",
    ],
    [
      "拒绝或裁剪",
      localizeList([...(latest.rejection_reasons || []), ...(latest.clipped_fields || [])], "当前没有额外拒绝或裁剪说明。"),
      localizeList(latest.notes, "当前没有额外翻译备注。"),
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
  return localizeList(items, fallback);
}

export function hasExecutionSuggestionContent(summary = {}) {
  const latest = summary.latest_translation || {};
  const preview = latest.translation_preview || {};
  const suggestion = latest.suggestion || summary.assessment_suggestion?.suggestion || {};
  const suggestionPairs = meaningfulEntries(suggestion).filter(([, value]) => {
    if (typeof value === "number") return value !== 0;
    return true;
  });
  return Boolean(
    summary.translation_present
      || latest.order_type
      || preview.order_type
      || preview.execution_style
      || latest.applied_to_live_execution
      || suggestionPairs.length
      || (latest.rejection_reasons || []).length
      || (latest.clipped_fields || []).length
      || (latest.notes || []).length
  );
}

function blockerSummary(latestOutcome, latestAssessment) {
  const reasons = splitCodeList(latestOutcome?.decision_blocked_reasons || latestAssessment?.rejection_flags || []);
  if (!reasons.length) {
    return {
      value: "当前没有额外决策链路阻断",
      meta: latestOutcome ? decisionSourceSummary(latestOutcome) : "当前还没有形成统一决策结果。",
    };
  }
  const primary = humanError(reasons[0]);
  const extra = reasons.length > 1 ? `，另外还有 ${reasons.length - 1} 条门禁。` : "。";
  return {
    value: primary,
    meta: `本轮主要是这个门禁阻止 AI 进入最终决策${extra}`,
  };
}

function assessmentCards(assessments) {
  return assessments.map((item) => ({
    kicker: "判断复盘",
    title: `${humanState(item.regime || "unknown")} | ${formatRelativeAge(item.created_at)}`,
    meta: formatMaybeTimestamp(item.created_at),
    tone: item.economically_actionable ? "positive" : "warning",
    badge: `<div class="inline-pills">${actorTags(item.fallback_used ? "system" : "ai", item.fallback_used ? "ai" : null)}${pill(item.economically_actionable ? "值得继续做" : "现在不值得做", item.economically_actionable ? "positive" : "warning")}</div>`,
    fields: [
      { label: "AI 会不会改主策略", value: item.baseline_override_recommended ? "建议改写基础策略" : "不建议改写" },
      { label: "值不值得做", value: basisPoints(item.estimated_net_edge_bps, 2), meta: `方向优势 ${formatNumber(item.directional_edge ?? 0, 2)}` },
      { label: "最终怎么处理", value: item.fallback_used ? "最终走回退结果" : "最终采用模型结果" },
    ],
    details: [
      { label: "建议这样处理的原因", value: readableList(item.override_reason_codes, "当前没有额外改写理由") },
      { label: "没有采用的补充说明", value: readableList((item.rejection_flags || item.validation_flags || []).map(humanError), "当前没有额外处理说明") },
    ],
    detailLabel: "展开这轮判断说明",
  }));
}

function shadowDecisionCards(items) {
  return items.map((item) => ({
    kicker: "策略层 shadow 复盘",
    title: `${humanState(item.ai_shadow_action || "unknown")} | ${formatRelativeAge(item.created_at)}`,
    meta: formatMaybeTimestamp(item.created_at),
    tone: item.would_override_baseline ? "warning" : "positive",
    badge: `<div class="inline-pills">${actorTags("system", "ai")}${pill(item.would_override_baseline ? "会改写基础策略" : "与基础策略一致", item.would_override_baseline ? "warning" : "positive")}</div>`,
    fields: [
      { label: "基础策略会怎么做", value: humanState(item.baseline_action || "unknown"), meta: `目标 ${formatNumber(item.baseline_target_qty ?? 0)}` },
      { label: "策略层 shadow 会怎么做", value: humanState(item.ai_shadow_action || "unknown"), meta: `目标 ${formatNumber(item.ai_shadow_target_qty ?? 0)}` },
      { label: "主要差异", value: humanState(item.shadow_action_type || "unknown") },
    ],
    details: [
      { label: "为什么会不同", value: readableList(item.reason_codes, "当前没有额外差异说明") },
    ],
    detailLabel: "展开这轮策略层 shadow 对比",
  }));
}

function shadowEvaluationCards(evaluations) {
  return evaluations.map((item) => ({
    kicker: "策略层 shadow 收益复盘",
    title: `${formatRelativeAge(item.window_end)} | ${item.shadow_outperformed === null ? "待结论" : item.shadow_outperformed ? "策略层 shadow 更好" : "基础策略更好"}`,
    meta: `${formatMaybeTimestamp(item.window_start)} ~ ${formatMaybeTimestamp(item.window_end)}`,
    tone: item.shadow_outperformed === null ? "outline" : item.shadow_outperformed ? "positive" : "warning",
    badge: item.shadow_outperformed === null
      ? `<div class="inline-pills">${actorTags("system")}${pill("尚未得出结论", "outline")}</div>`
      : item.shadow_outperformed
        ? `<div class="inline-pills">${actorTags("system")}${pill("策略层 shadow 更好", "positive")}</div>`
        : `<div class="inline-pills">${actorTags("system")}${pill("基础策略更好", "warning")}</div>`,
    fields: [
      { label: "基础策略净收益", value: formatNumber(item.baseline_net_pnl ?? 0), meta: `成交 ${formatNumber(item.baseline_trade_count ?? 0, 0)} 笔` },
      { label: "策略层 shadow 净收益", value: formatNumber(item.shadow_net_pnl ?? 0), meta: `成交 ${formatNumber(item.shadow_trade_count ?? 0, 0)} 笔` },
      { label: "成本差异", value: `${formatSigned((Number(item.shadow_fee_ratio ?? 0) - Number(item.baseline_fee_ratio ?? 0)), 4)} / ${formatSigned((Number(item.shadow_churn_ratio ?? 0) - Number(item.baseline_churn_ratio ?? 0)), 4)}`, meta: "手续费占比差 / 来回交易差" },
    ],
    details: [
      { label: "基础策略毛收益", value: formatNumber(item.baseline_gross_pnl ?? 0) },
      { label: "策略层 shadow 毛收益", value: formatNumber(item.shadow_gross_pnl ?? 0) },
    ],
    detailLabel: "展开这组策略层 shadow 窗口复盘",
  }));
}

function performanceReportCards(reports = [], replayContext = {}) {
  return reports.map((item) => ({
    kicker: "长期复盘",
    title: `${humanState(item.latest_status || "insufficient_data")} | ${formatRelativeAge(item.generated_at)}`,
    meta: formatMaybeTimestamp(item.generated_at),
    tone: item.review_required ? "warning" : item.latest_status === "healthy" ? "positive" : "outline",
    badge: `<div class="inline-pills">${actorTags("system")}${pill(humanState(item.latest_status || "insufficient_data"), item.review_required ? "warning" : item.latest_status === "healthy" ? "positive" : "outline")}</div>`,
    fields: [
      {
        label: "短 / 中 / 长窗收益差",
        value: `${formatSigned(item.windows?.short?.net_pnl_delta_total ?? 0, 4)} / ${formatSigned(item.windows?.medium?.net_pnl_delta_total ?? 0, 4)} / ${formatSigned(item.windows?.long?.net_pnl_delta_total ?? 0, 4)}`,
      },
      {
        label: "是否需要复核",
        value: item.review_required ? "需要复核" : "暂不需要",
        meta: `短窗 ${formatNumber(item.windows?.short?.review_required_count ?? 0, 0)} / 中窗 ${formatNumber(item.windows?.medium?.review_required_count ?? 0, 0)} / 长窗 ${formatNumber(item.windows?.long?.review_required_count ?? 0, 0)}`,
      },
      {
        label: "最近回放健康度",
        value: formatMaybeTimestamp(replayContext.latest_validation?.validated_at),
        meta: `偏差 ${formatNumber(replayContext.latest_validation?.divergence_count ?? 0, 0)}`,
      },
    ],
    detailLabel: "展开长期表现复盘",
  }));
}

export function renderAISections(data) {
  const session = data.session || {};
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
  const latestEconomicRows = economicGateRows(latestAssessment, latestOutcome, latestProfileControl);

  const analysisSections = renderAIAnalysisSectionCards(data);
  return {
    aiHero: surfaceCard({
      title: "AI 状态概览",
      kicker: "运行状态",
      copy: "先看当前运行模式、最近一轮真实决策结果，以及是否还有仍在生效的异常状态。",
      classes: "hero-card is-compact",
      content: `
        ${summaryStrip([
          {
            label: "当前运行模式",
            value: humanState(effectiveMode(runtime)),
            meta: `默认运行模式 ${humanState(configuredMode(runtime))}`,
            tone: toneForRuntime(runtime),
            badge: actorTags(runtime.operating_mode_source === "manual_selection" ? "admin" : "system"),
          },
          {
            label: "最近一轮真实决策结果",
            value: humanState(latestOutcome?.decision_source || "baseline"),
            meta: latestOutcome ? decisionSourceSummary(latestOutcome) : "当前暂无统一决策结果。",
            tone: latestOutcome?.decision_source === "ai" ? "positive" : latestOutcome?.decision_source === "baseline_fallback" ? "warning" : "info",
            badge: actorTags(
              latestOutcome?.decision_source === "ai"
                ? "ai"
                : latestOutcome?.decision_source === "admin_override"
                  ? "admin"
                  : "system",
            ),
          },
          {
            label: "模型服务状态",
            value: humanState(downgradeState.provider_state || "healthy"),
            meta: runtime.provider_ready ? "模型服务当前可用。" : "模型服务当前不可用。",
            tone: runtime.provider_ready ? "positive" : "warning",
            badge: actorTags("ai"),
          },
          {
            label: "人工复核状态",
            value: humanState(downgradeState.outcome_state || "healthy"),
            meta: shadowSummary.review_required ? "当前已进入人工复核流程。" : "当前没有新的人工复核。",
            tone: shadowSummary.review_required ? "warning" : "positive",
            badge: actorTags("admin"),
          },
        ])}
        ${callout({
          title:
            effectiveMode(runtime) === "baseline_only"
              ? "当前不让 AI 参与真实交易决策"
              : "当前允许 AI 参与真实交易决策",
          copy: aiRuntimeNarrative(runtime, latestDegradation),
          pills: [
            actorTags(runtime.operating_mode_source === "manual_selection" ? "admin" : effectiveMode(runtime) === "baseline_only" ? "system" : "ai"),
            pill(`策略层 shadow ${runtime.shadow_mode_enabled ? "已开启" : "未开启"}`, runtime.shadow_mode_enabled ? "info" : "outline"),
          ],
        })}
        ${statGrid([
          {
            label: "连续失败 / 成功",
            value: `${formatNumber(runtime.consecutive_failures ?? 0, 0)} / ${formatNumber(runtime.consecutive_successes ?? 0, 0)}`,
            meta: `最近评估 ${formatNumber(runtime.recent_assessment_count ?? 0, 0)} 次`,
          },
          {
            label: "近期回退到基础策略比率",
            value: formatNumber(runtime.recent_fallback_ratio ?? 0, 3),
            meta: `timeout ${formatNumber(runtime.recent_timeout_count ?? 0, 0)} / 无效输出 ${formatNumber(runtime.recent_invalid_output_count ?? 0, 0)}`,
          },
              {
                label: "最近一次状态变化",
                value: reviewResolutionSummary(runtime, latestDegradation) || humanError(latestDegradation?.reason_code),
                meta: formatMaybeTimestamp(latestDegradation?.created_at),
              },
              {
                label: "策略层 shadow 优于基础策略",
            value: formatNumber(shadowSummary.outperformed_rate ?? 0, 3),
            meta: `评估窗口 ${formatNumber(shadowSummary.window_count ?? 0, 0)} / 状态 ${humanState(shadowSummary.status || "insufficient_data")}`,
          },
        ])}
        ${latestDegradation
          ? kvList([
              ...(runtime.provider_degraded && runtime.recovery_probe_after
                ? [[
                    "恢复探测",
                    formatMaybeTimestamp(runtime.recovery_probe_after),
                    runtime.recovery_probe_ready ? "可以开始探测恢复。" : "还没到恢复探测时间。",
                  ]]
                : []),
            ])
          : ""}
        ${kvList([
          ...(activeDegradationReasons(downgradeState).length
            ? [[
                "当前仍生效的降级原因",
                localizeList(activeDegradationReasons(downgradeState), "当前没有新的降级原因。"),
                reviewResolutionSummary(runtime, latestDegradation) || "",
              ]]
            : []),
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
            ${latestDecisionCallout(latestOutcome, latestAssessment, latestProfileControl)}
            ${summaryStrip([
              {
                label: "基础策略参考",
                value: humanState(latestBaseline?.direction_bias || latestAssessment?.regime || "unknown"),
                meta: latestBaseline
                  ? `置信度 ${formatNumber(latestBaseline.confidence ?? 0, 2)} / ${summarizeLocalizedList(latestBaseline.reason_codes, { fallback: "当前没有额外信号说明。", limit: 4 })}`
                  : `方向优势 ${formatNumber(latestAssessment?.directional_edge ?? 0, 2)}`,
                tone: "info",
                badge: actorTags("system"),
              },
              {
                label: "AI 决策意图",
                value: humanState(latestIntent?.direction || "unknown"),
                meta: latestIntent
                  ? `${humanState(latestIntent.action)} / 目标 ${formatNumber(latestIntent.target_qty ?? 0)} / ${summarizeLocalizedList(latestIntent.reason_codes, { fallback: "当前没有额外意图说明。", limit: 3 })}`
                  : "当前暂无新的 AI 决策意图",
                tone: latestIntent ? "warning" : "outline",
                badge: actorTags("ai"),
              },
              {
                label: "最终决策结果",
                value: humanState(latestOutcome?.final_action || "hold"),
                meta: latestOutcome
                  ? `${humanState(latestOutcome.decision_source)} / 目标 ${formatNumber(latestOutcome.final_target_qty ?? 0)} / ${decisionSourceSummary(latestOutcome)}`
                  : "等待真实决策结果",
                tone: latestOutcome?.decision_source === "ai" ? "positive" : latestOutcome?.decision_source === "baseline_fallback" ? "info" : "warning",
                badge: actorTags(
                  latestOutcome?.decision_source === "ai"
                    ? "ai"
                    : latestOutcome?.decision_source === "admin_override"
                      ? "admin"
                      : "system",
                ),
              },
              {
                label: "策略档位控制",
                value: profileControlSummary(latestOutcome, latestProfileControl).value,
                meta: profileControlSummary(latestOutcome, latestProfileControl).meta,
                tone: latestProfileControl?.applied ? "positive" : "outline",
                badge: actorTags(
                  latestOutcome?.profile_control_source === "admin"
                    ? "admin"
                    : latestOutcome?.profile_control_source === "system"
                      ? "system"
                      : "system",
                  latestProfileControl?.applied || latestProfileControl?.blocked_reasons?.length ? "ai" : null,
                ),
              },
            ])}
            ${kvList([
              ["判断时间", formatMaybeTimestamp(latestAssessment?.created_at), formatRelativeAge(latestAssessment?.created_at)],
              ["基础策略参考", humanState(latestBaseline?.direction_bias || "unknown"), latestBaseline ? `置信度 ${formatNumber(latestBaseline.confidence ?? 0, 2)} / ${summarizeLocalizedList(latestBaseline.reason_codes, { fallback: "当前没有额外信号说明。", limit: 4 })}` : "当前暂无新的基础策略参考"],
              ["AI 决策意图", latestIntent ? `${humanState(latestIntent.direction)} / ${humanState(latestIntent.action)}` : "当前暂无新的 AI 决策意图", latestIntent ? `目标 ${formatNumber(latestIntent.target_qty ?? 0)} / ${summarizeLocalizedList(latestIntent.reason_codes, { fallback: "当前没有额外意图说明。", limit: 3 })}` : "多半仍在基础策略决策链路，或 AI 已回退"],
              ["最终决策来源", humanState(latestOutcome?.decision_source || "baseline"), latestOutcome ? humanState(latestOutcome.decision_authority || "reference_only") : "当前暂无统一决策结果"],
              ["AI 未被采用的主要原因", blockerSummary(latestOutcome, latestAssessment).value, blockerSummary(latestOutcome, latestAssessment).meta],
              ["策略档位控制", profileControlSummary(latestOutcome, latestProfileControl).value, profileControlSummary(latestOutcome, latestProfileControl).meta],
              ["最新策略层 shadow 动作", humanState(latestShadowDecision?.ai_shadow_action || "unknown"), latestShadowDecision ? `相对基础策略：${humanState(latestShadowDecision.shadow_action_type)}` : "当前暂无策略层 shadow 动作"],
            ])}
            ${latestEconomicRows.length
              ? surfaceCard({
                  title: "经济性概览",
                  kicker: "经济门槛",
                  copy: "这里把预期优势、成本、净优势和决策链路阻断拆开看。",
                  classes: "is-muted",
                  content: kvList(latestEconomicRows),
                })
              : ""}
          `
        : callout({
            title: "当前暂无新的 AI 判断",
            copy: "当前多半仍在仅基础策略模式，或者模型服务处于降级后的自动回退状态。",
            pills: [pill(`当前有效模式 ${humanState(effectiveMode(runtime))}`, "outline")],
          }),
    }),
    aiExecutionSuggestion: analysisSections.aiExecutionSuggestion,
    aiReview: aiReviewBlocker
      ? surfaceCard({
          title: "AI 人工复核",
          kicker: "人工动作",
          copy: "这里处理 AI 人工复核，不直接下单，也不会自动修改仓位；它只决定后续是否继续信任 AI 决策链路。",
          content: `
            ${reviewCallout(aiReviewBlocker, latestDegradation)}
            ${summaryStrip([
              {
                label: "当前阻断",
                value: textOrFallback(aiReviewBlocker.title, humanError(aiReviewBlocker.blocker)),
                meta: textOrFallback(aiReviewBlocker.impact, "当前阻断会影响系统是否恢复 AI 决策链路。"),
                tone: "danger",
                badge: actorTags("system"),
              },
              {
                label: "当前建议",
                value: textOrFallback(aiReviewBlocker.recommended_next_step, "请先完成这次人工复核。"),
                meta: "管理员完成复核后，系统才会决定恢复 AI 还是继续只用基础策略。",
                tone: "warning",
                badge: actorTags("admin"),
              },
            ])}
            ${kvList([
              ["人工复核原因", textOrFallback(aiReviewBlocker.description, "当前已触发 AI 人工复核。"), hasMeaningfulValue(latestDegradation?.reason_code) ? humanError(latestDegradation.reason_code) : ""],
              ["最近策略层 shadow 评估", readableShadowMeta(shadowSummary), shadowSummary.review_required ? "当前已进入人工复核状态。" : "当前没有新的人工复核。"],
            ])}
            ${renderReviewActions(aiReviewBlocker)}
          `,
        })
      : null,
    aiHistory: analysisSections.aiHistory,
    aiPerformanceReports: analysisSections.aiPerformanceReports,
  };
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
