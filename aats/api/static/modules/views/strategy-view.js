import { actionButton, callout, kvList, pill, responsiveTable, statGrid, summaryStrip, surfaceCard } from "../components.js";
import { localizeList, summarizeLocalizedList } from "../copy.js";
import { escapeHtml, formatDuration, formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, middleEllipsis } from "../formatters.js";
import {
  hasFamilyExecutionSummary,
  localizeError,
  readableBookExpectancySummary,
  readableBookRuntimeStateSummary,
  readableIndependentAdaptiveMeta,
  readableIndependentAdaptiveSummary,
  readableExpectedVsRealizedMeta,
  readableExpectedVsRealizedSummary,
  readableFamilyExecutionDirection,
  readableFamilyExecutionMeta,
  readableFamilyExecutionSummary,
  readableIndependentTransitionExceptionMeta,
  readableIndependentTransitionExceptionSummary,
  readableOverlayParentSignalSummary,
  readableState,
} from "../terms.js";
import { decisionTableHeaders, inferTradeScene } from "../trade-display.js";

export function renderStrategySections(data) {
  const latestDecision = data.latestDecision || {};
  const recentPayload = data.recentDecisions || {};
  const recentDecisions = recentPayload.decisions || [];
  const executionLatest = data.executionLatest || {};
  const strategyRuntime = data.strategyRuntime || {};
  const strategyRuntimeSummary = strategyRuntime.summary || {};
  const strategyRuntimeSnapshot = strategyRuntime.latest_snapshot || {};
  const strategyCandidates = strategyRuntimeSnapshot.candidates || [];
  const latestBundle = strategyRuntime.latest_bundle || {};
  const recentBundles = strategyRuntime.recent_execution_bundles || [];
  const latestAllocationDecision = strategyRuntime.latest_allocation_decision || {};
  const recentSleeveIntents = strategyRuntime.recent_sleeve_intents || [];
  const recentBudgetSnapshots = strategyRuntime.recent_budget_snapshots || [];
  const recentConflictResolutions = strategyRuntime.recent_conflict_resolutions || [];
  const recentNettingDecisions = strategyRuntime.recent_netting_decisions || [];
  const strategyAppliedTarget = strategyRuntime.latest_applied_target || {};
  const automationDecisions = strategyRuntimeSnapshot.automation_decisions || [];
  const smartArbitrageConfig = strategyRuntime.configured_parameters?.smart_arbitrage || {};
  const smartArbitrageCostSummary = strategyRuntime.smart_arbitrage_cost_summary || {};
  const strategyAttribution = data.strategyAttribution || {};
  const attributionSummary = strategyAttribution.summary || {};
  const sleeveProfitability = strategyAttribution.profitability_by_strategy_sleeve || [];
  const sleeveInventorySummary = strategyAttribution.sleeve_inventory_summary || [];
  const strategyFamilyEnablement = strategyRuntime.family_enablement || {};
  const baseline = latestDecision.baseline_assessment || {};
  const targetExpectancy = resolvedTargetExpectancyMetrics(latestDecision.position_target || strategyAppliedTarget || {});
  const ai = latestDecision.ai_assessment || {};
  const target = latestDecision.position_target || {};
  const policy = latestDecision.policy_decision || {};
  const risk = latestDecision.risk_decision || {};
  const intentLabel = readableIntent(latestDecision);
  const regimeLabel = readableRegime(latestDecision);
  const decisionScene = inferDecisionScene(latestDecision, recentDecisions);
  const strategyHealth = latestDecision.strategy_execution_health || data.metrics?.strategy_execution_health || {};
  const trialReviewSummaryModel = data.trialReviewSummary || data.trialReviewPacket || {};
  const trialReviewSummary = trialReviewSummaryModel.summary || {};
  const trialReviewRecommendation = trialReviewSummaryModel.recommendation || {};
  const trialReviewSections = trialReviewSummaryModel.sections || {};
  const trialReviewWorkbench = trialReviewSections.workbench || {};
  const trialReviewHistory = data.trialReviewHistory || {};
  const trialReviewActions = trialReviewWorkbench.available_actions || [];
  const trialReviewRecentActions = trialReviewWorkbench.recent_actions || trialReviewHistory.actions || [];
  const trialReviewLatestAction = trialReviewWorkbench.latest_action || {};
  const forwardValidation = trialReviewSections.forward_validation || data.forwardValidation || {};
  const forwardSummary = forwardValidation.summary || {};
  const forwardPeriods = forwardValidation.periods || [];
  const scalingReadiness = trialReviewSections.scaling_readiness || data.scalingReadiness || {};
  const scalingRequirements = scalingReadiness.requirements || {};
  const trialGuardHardStop = scalingReadiness.trial_guard_hard_stop || trialReviewSections.trial_guard_hard_stop || {};
  const runtimeConstraints = scalingReadiness.runtime_constraints || trialReviewSections.runtime_constraints || {};
  const latestForwardPeriod = forwardPeriods[0] || {};
  const trialVerdict = chooseTrialVerdict(scalingReadiness.readiness, forwardSummary.verdict, trialReviewSummary.readiness);
  const trialHeadline =
    scalingReadiness.summary
    || forwardSummary.summary
    || trialReviewSummary.headline
    || "当前还没有形成稳定的系统试盘结论。";
  const trialReasons = mergeReasonLists(
    scalingReadiness.reasons,
    scalingReadiness.forward_validation_summary?.reasons,
    forwardSummary.reasons,
    trialReviewRecommendation.reasons,
  );
  const displayedStrategyCandidates = strategyCandidates.slice(0, 4);
  const displayedAutomationDecisions = automationDecisions.slice(0, 5);
  const displayedSleeveProfitability = sleeveProfitability.slice(0, 6);
  const displayedForwardPeriods = forwardPeriods.slice(0, 4);
  const displayedTrialReviewActions = trialReviewRecentActions.slice(0, 5);
  const tradeCostConfig = strategyRuntime.configured_parameters?.trade_costs || {};
  const directionalConfig = strategyRuntime.configured_parameters?.directional || {};
  const independentExpectedVsRealized = strategyRuntime.independent_expected_vs_realized_summary || {};
  const independentAdaptiveSummary = strategyRuntime.independent_adaptive_summary || {};
  const independentTransitionExceptionSummary = strategyRuntime.independent_transition_exception_summary || {};
  const independentAdaptiveCopy = readableIndependentAdaptiveSummary(independentAdaptiveSummary, "");
  const independentAdaptiveMeta = independentAdaptiveCopy
    ? readableIndependentAdaptiveMeta(independentAdaptiveSummary, "当前没有额外自适应说明")
    : "";
  const independentTransitionExceptionCopy = readableIndependentTransitionExceptionSummary(
    independentTransitionExceptionSummary,
    "",
  );
  const independentTransitionExceptionMeta = independentTransitionExceptionCopy
    ? readableIndependentTransitionExceptionMeta(independentTransitionExceptionSummary, "当前没有额外迁移异常说明")
    : "";
  const expectedVsRealizedCopy = readableExpectedVsRealizedSummary(independentExpectedVsRealized, "");
  const expectedVsRealizedMeta = expectedVsRealizedCopy
    ? readableExpectedVsRealizedMeta(independentExpectedVsRealized, "当前没有额外诊断说明")
    : "";
  const coordinatorDiagnostics = [];
  if (independentAdaptiveCopy) {
    coordinatorDiagnostics.push([
      "独立双书自适应阈值",
      independentAdaptiveCopy,
      independentAdaptiveMeta,
    ]);
  }
  if (expectedVsRealizedCopy) {
    coordinatorDiagnostics.push([
      "独立双书预期 vs 已实现",
      expectedVsRealizedCopy,
      expectedVsRealizedMeta,
    ]);
  }
  if (independentTransitionExceptionCopy) {
    coordinatorDiagnostics.push([
      "独立双书迁移异常",
      independentTransitionExceptionCopy,
      independentTransitionExceptionMeta,
    ]);
  }

  return {
    strategyHero: surfaceCard({
      title: "策略结论",
      kicker: "决策结果",
      copy: "先看结论，再看是否真的准备进入执行。",
      classes: "hero-card",
      actions: latestDecision.decision_id ? actionButton("查看完整决策链", "inspect-decision", latestDecision.decision_id) : "",
      content: `
        ${callout({
          title: latestDecision.decision_id ? `当前策略结论：${intentLabel}` : "当前暂无新的策略输出",
          copy: strategyNarrative(latestDecision),
          pills: [
            pill(`市场状态：${regimeLabel}`, "info"),
            pill(`策略门禁：${readableState(policy.execution_allowed ? "ready" : "blocked")}`, policy.execution_allowed ? "positive" : "danger"),
            pill(`风控：${readableState(risk.approved ? "ready" : "blocked")}`, risk.approved ? "positive" : "danger"),
          ],
        })}
        ${statGrid([
          {
            label: decisionScene === "derivatives" ? "目标净仓位变化" : "目标持仓变化",
            value: targetDeltaValue(target, decisionScene),
            meta: targetDirectionLabel(target, decisionScene),
          },
          {
            label: decisionScene === "derivatives" ? "当前净仓位" : "当前持仓",
            value: currentPositionValue(target, decisionScene),
            meta: decisionScene === "derivatives" ? "按净仓位口径展示" : "按现货持仓口径展示",
          },
          {
            label: "最新决策时间",
            value: formatMaybeTimestamp(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts),
            meta: formatRelativeAge(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts),
          },
          {
            label: "最近执行结果",
            value: latestOrderStatusLabel(executionLatest.latest_order),
            meta: middleEllipsis(executionLatest.latest_order?.client_order_id, 10, 6, "暂未生成委托"),
          },
        ])}
      `,
    }),
    strategyDecisionWorkbench: surfaceCard({
      title: "本轮判断与执行约束",
      kicker: "工作区",
      copy: "把这轮最关键的信号、目标、门禁和执行约束压成一屏，先看这里，不必先翻长表。",
      classes: "strategy-compact-card",
      content: `
        ${summaryStrip([
          {
            label: "交易场景",
            value: decisionScene === "derivatives" ? "合约" : "现货",
            meta: decisionScene === "derivatives" ? optionalState(target.margin_mode, "当前没有保证金模式信息") : "现金买卖",
            tone: "info",
          },
          {
            label: "基础信号",
            value: formattedOrText(baseline.confidence, "暂无基础信号"),
            meta: numberMeta("综合强度", baseline.composite_alpha_score, "当前没有综合强度"),
            tone: "info",
          },
          {
            label: "AI 参考",
            value: optionalState(ai.summary || ai.direction_bias, "暂无 AI 参考"),
            meta: numberMeta("AI 置信度", ai.confidence, "当前没有 AI 置信度"),
            tone: "info",
          },
          {
            label: decisionScene === "derivatives" ? "目标净仓位" : "目标持仓",
            value: targetPositionValue(target, decisionScene),
            meta: targetPlanMeta(target, decisionScene),
            tone: "info",
          },
          {
            label: "策略门禁",
            value: policy.execution_allowed ? "允许进入执行" : "仍在阻断",
            meta: listText(policy.execution_allowed ? policy.allow_reasons : policy.blocker_reasons, "当前没有额外门禁说明"),
            tone: policy.execution_allowed ? "positive" : "danger",
          },
          {
            label: "风控结论",
            value: risk.approved ? "风控放行" : "风控拦截",
            meta: listText(risk.approved ? risk.approval_reasons : risk.rejection_reasons, "当前没有额外风控说明"),
            tone: risk.approved ? "positive" : "danger",
          },
        ])}
        ${kvList([
          ["本轮结论", strategyNarrative(latestDecision), `${readableState(target.strategy_family || latestDecision.decision_outcome?.selected_strategy_family || "directional")} | ${regimeLabel}`],
          ["执行约束", listText(target.guardrail_flags, "当前没有额外执行限制"), targetExpectancySummary(targetExpectancy)],
          ["当前保护规则", listText(strategyHealth.guardrail_flags, "当前没有额外保护规则"), cooldownSummary(strategyHealth.cooldowns)],
          ["最近执行质量", `${formatNumber(strategyHealth.recent_closed_trade_count, 0)} 笔闭合样本 | 胜率 ${formatRatio(strategyHealth.recent_win_rate)}`, `费用拖累 ${formatRatio(strategyHealth.recent_fee_drag_ratio)} | 来回交易占比 ${formatRatio(strategyHealth.recent_churn_ratio)}`],
        ])}
      `,
    }),
    strategyCoordinator: surfaceCard({
      title: "多策略调度",
      kicker: "并行策略",
      copy: "默认只保留当前调度结论和候选概览；预算快照、冲突解算和净额决策收进展开区，避免这一块占满整页。",
      classes: "strategy-compact-card",
      content: `
        ${summaryStrip([
          {
            label: "策略家族模式",
            value: strategyRuntimeSummary.automatic_selection_enabled ? "全自动" : readableState(strategyRuntimeSummary.configured_active_family || "directional"),
            meta: familyEnablementSummary(strategyFamilyEnablement),
            tone: "info",
          },
          {
            label: "最近一次选中",
            value: readableState(strategyRuntimeSummary.latest_selected_family || "directional"),
            meta: readableState(strategyRuntimeSummary.latest_selected_state || "unknown"),
            tone: "info",
          },
          {
            label: "最近执行包",
            value: readableState(strategyRuntimeSummary.latest_bundle_status || "unknown"),
            meta: latestBundle.bundle_id ? middleEllipsis(latestBundle.bundle_id, 10, 8, "当前没有策略执行包") : "当前没有策略执行包",
            tone: strategyRuntimeSummary.latest_bundle_status === "blocked" ? "warning" : "info",
          },
          {
            label: "组合预算变化",
            value: `${formatSigned(strategyRuntimeSummary.latest_portfolio_requested_notional)} -> ${formatSigned(strategyRuntimeSummary.latest_portfolio_approved_notional)}`,
            meta: `削减 ${formatSigned(strategyRuntimeSummary.latest_portfolio_budget_cut_notional)}`,
            tone: "info",
          },
        ])}
        ${kvList([
          ["调度结论", strategyRuntimeSummary.operator_summary || "当前还没有多策略调度快照。", reasonListText(strategyRuntimeSummary.latest_selection_reason_codes, "当前没有额外调度原因说明")],
          ["当前路由", strategyRouteActionLabel(strategyRuntimeSummary.latest_selected_route_action, strategyRuntimeSummary.latest_selected_family_action), strategyRuntimeSummary.protective_fallback_active ? "当前保留了保护性减仓/退出路径。" : "当前没有触发保护性回退。"],
          ["组合分配结论", latestAllocationDecision.operator_summary || "当前还没有组合分配决策。", reasonListText(latestAllocationDecision.reason_codes, "当前没有额外分配原因说明")],
          [
            "Hedge 保护 / 方向削减",
            `${formatSigned(strategyRuntimeSummary.latest_hedge_protected_notional)} / ${formatSigned(strategyRuntimeSummary.latest_directional_reduced_notional)}`,
            "前者表示为保护 hedge 结构而保留的名义金额，后者表示组合分配器主动削减的方向暴露。",
          ],
          [
            "最近执行包 / 已应用目标",
            `${readableState(latestBundle.status || strategyRuntimeSummary.latest_bundle_status || "unknown")} / ${formatSigned(strategyAppliedTarget.target_position_qty)}`,
            `${formatNumber(recentBundles[0]?.legs?.length ?? latestBundle.legs?.length ?? 0, 0, "0")} 条腿 | ${readableFamilyExecutionSummary(strategyAppliedTarget, "保持当前仓位")}${familyExpectancySuffix(strategyAppliedTarget)}`,
          ],
        ])}
        ${renderStrategyCandidateTable(displayedStrategyCandidates, smartArbitrageConfig, { policy, risk })}
        ${renderRecentSleeveIntentTable(recentSleeveIntents.slice(0, 5), { policy, risk })}
        ${coordinatorDiagnostics.length ? renderExpandableSection("高级诊断：独立双书", kvList(coordinatorDiagnostics), {
          meta: "默认收起，只在核对阈值和预期偏差时查看",
        }) : ""}
        ${renderExpandableSection("预算快照", renderAllocatorBudgetSnapshotTable(recentBudgetSnapshots), {
          meta: `${formatNumber(strategyRuntimeSummary.latest_budget_snapshot_count, 0, "0")} 条`,
          open: true,
        })}
        ${renderExpandableSection("冲突解算", renderAllocatorConflictResolutionTable(recentConflictResolutions), {
          meta: `${formatNumber(strategyRuntimeSummary.latest_conflict_resolution_count, 0, "0")} 条`,
          open: true,
        })}
        ${renderExpandableSection("净额决策", renderAllocatorNettingDecisionTable(recentNettingDecisions), {
          meta: `${formatNumber(strategyRuntimeSummary.latest_netting_decision_count, 0, "0")} 条`,
          open: true,
        })}
      `,
    }),
    strategyTradeCosts: renderTradeCostConfigCard(tradeCostConfig),
    strategyDirectionalConfig: renderDirectionalShortConfigCard(directionalConfig, latestDecision, decisionScene),
    strategySmartArbitrageConfig: renderSmartArbitrageConfigCard(
      smartArbitrageConfig,
      tradeCostConfig,
      strategyFamilyEnablement?.smart_arbitrage || {}
    ),
    strategySmartArbitrageCost: renderSmartArbitrageCostCard(smartArbitrageCostSummary),
    strategyAutomation: surfaceCard({
      title: "自动预算与启停",
      kicker: "全自动并行运行",
      copy: "这里保留当前最关键的自动控制结果，更多预算细节已经收进调度卡的展开区。",
      classes: "strategy-compact-card",
      content: `
        ${summaryStrip([
          {
            label: "自动并行运行",
            value: strategyRuntimeSummary.auto_parallel_enabled ? "已启用" : "未启用",
            meta: strategyRuntimeSummary.auto_parallel_enabled ? "当前按系统规则自动启停和分配预算。" : "当前没有启用 sleeve 自动预算控制。",
            tone: strategyRuntimeSummary.auto_parallel_enabled ? "positive" : "warning",
          },
          {
            label: "活跃子策略",
            value: formatNumber(strategyRuntimeSummary.automation_active_count, 0, "0"),
            meta: "当前在自动预算内正常运行",
            tone: "positive",
          },
          {
            label: "收缩中的子策略",
            value: formatNumber(strategyRuntimeSummary.automation_contracted_count, 0, "0"),
            meta: "预算已收缩或只保留保护性管理",
            tone: strategyRuntimeSummary.automation_contracted_count ? "warning" : "info",
          },
          {
            label: "暂停中的子策略",
            value: formatNumber(strategyRuntimeSummary.automation_paused_count, 0, "0"),
            meta: "当前已被系统自动暂停",
            tone: strategyRuntimeSummary.automation_paused_count ? "danger" : "info",
          },
        ])}
        ${kvList([
          [
            "最新预算权重",
            Object.keys(strategyRuntimeSummary.latest_approved_sleeve_weights || {}).length ? "已生成预算权重" : "当前没有新的预算权重",
            Object.entries(strategyRuntimeSummary.latest_approved_sleeve_weights || {})
              .map(([sleeveId, weight]) => `${sleeveId}: ${formatNumber(weight, 2, "0")}`)
              .join(" | ") || "本轮没有批准新的 sleeve 预算。",
          ],
          [
            "自动控制阈值",
            strategyRuntime.configured_parameters?.strategy_sleeve_auto_parallel_enabled ? "按运行参数自动控制" : "当前未启用自动控制",
            [
              `最小预算倍率 ${formatNumber(strategyRuntime.configured_parameters?.strategy_sleeve_auto_min_budget_multiplier, 2, "暂无预算阈值")}`,
              `软亏损 ${formatSigned(strategyRuntime.configured_parameters?.strategy_sleeve_auto_soft_loss_usdt)}`,
              `硬亏损 ${formatSigned(strategyRuntime.configured_parameters?.strategy_sleeve_auto_hard_loss_usdt)}`,
            ].join(" | "),
          ],
        ])}
        ${responsiveTable(
          ["子策略", "自动状态", "预算倍率", "权重", "最近净收益"],
          displayedAutomationDecisions.map((item) => [
            `<div><strong>${escapeHtml(item.strategy_sleeve_id || "未归属")}</strong><div class="table-meta">${escapeHtml(readableState(item.family || "unknown"))}</div></div>`,
            `<div><strong>${escapeHtml(readableState(item.automation_state || "unknown"))}</strong><div class="table-meta">${escapeHtml(item.operator_summary || "当前没有额外说明")}</div></div>`,
            formatNumber(item.budget_multiplier, 2, "0"),
            formatNumber(item.allocator_weight, 2, "0"),
            formatSigned(item.recent_net_pnl),
          ]),
          "当前还没有自动预算与启停决策。"
        )}
      `,
    }),
    strategyAttribution: surfaceCard({
      title: "策略归因",
      kicker: "组合报表",
      copy: "这里只保留最能解释“谁在赚钱、谁还占库存”的摘要，避免归因卡片本身反过来挤占工作区。",
      classes: "strategy-compact-card",
      content: `
        ${summaryStrip([
          {
            label: "归因记录数",
            value: formatNumber(attributionSummary.sleeve_pnl_record_count, 0, "0"),
            meta: `${formatNumber((strategyAttribution.profitability_by_attribution_type || []).length, 0, "0")} 类归因方式`,
            tone: "info",
          },
          {
            label: "组合净收益",
            value: formatSigned(attributionSummary.combined_net_realized_pnl),
            meta: `资金费 ${formatSigned(attributionSummary.funding_fee_net_pnl)}`,
            tone: Number(attributionSummary.combined_net_realized_pnl || 0) >= 0 ? "positive" : "warning",
          },
          {
            label: "库存最大子策略",
            value: attributionSummary.top_inventory_sleeve_id || "当前没有库存",
            meta: attributionSummary.top_inventory_notional == null ? "当前没有库存名义金额" : `名义金额 ${formatSigned(attributionSummary.top_inventory_notional)}`,
            tone: "info",
          },
          {
            label: "受保护成交",
            value: formatNumber(attributionSummary.protected_fill_count, 0, "0"),
            meta: `未受保护 ${formatNumber(attributionSummary.unprotected_fill_count, 0, "0")} 笔`,
            tone: attributionSummary.protected_fill_count ? "warning" : "positive",
          },
        ])}
        ${kvList([
          [
            "主要子策略收益",
            sleeveProfitability[0]?.strategy_sleeve_id || "当前没有子策略收益记录",
            sleeveProfitability[0] ? `净收益 ${formatSigned(sleeveProfitability[0].combined_net_realized_pnl)} | 记录 ${formatNumber(sleeveProfitability[0].record_count, 0, "0")} 条` : "等第一批 fill / funding fee 归因记录落地后，这里会自动出现。",
          ],
          [
            "库存归属概览",
            sleeveInventorySummary[0]?.strategy_sleeve_id || "当前没有持仓批次",
            sleeveInventorySummary[0] ? `库存名义金额 ${formatSigned(sleeveInventorySummary[0].inventory_notional)} | ${formatNumber(sleeveInventorySummary[0].open_lot_count, 0, "0")} 个持仓批次` : "当前没有需要追踪的子策略库存。",
          ],
          [
            "执行包归因",
            formatNumber((strategyAttribution.profitability_by_strategy_bundle || []).length, 0, "0"),
            "按 allocation / 执行包 的收益归因已经进入组合报表，可用于排查多腿执行后的收益归属。",
          ],
        ])}
        ${responsiveTable(
          ["子策略", "净收益", "资金费", "库存变化", "库存名义金额"],
          displayedSleeveProfitability.map((item) => {
            const inventory = sleeveInventorySummary.find((row) => row.strategy_sleeve_id === item.strategy_sleeve_id) || {};
            return [
              `<div><strong>${escapeHtml(item.strategy_sleeve_id || "未归属")}</strong><div class="table-meta">${escapeHtml((item.families || []).join(" / ") || "当前没有家族标签")}</div></div>`,
              `<div><strong>${formatSigned(item.combined_net_realized_pnl)}</strong><div class="table-meta">实现 ${formatSigned(item.realized_pnl)}</div></div>`,
              `<div><strong>${formatSigned(item.funding_fee_amount)}</strong><div class="table-meta">费用 ${formatRawFeeImpact(item.fee_amount)}</div></div>`,
              `<div><strong>${formatSigned(item.inventory_move_qty)}</strong><div class="table-meta">${formatNumber(item.record_count, 0, "0")} 条记录</div></div>`,
              `<div><strong>${formatSigned(inventory.inventory_notional)}</strong><div class="table-meta">${formatNumber(inventory.open_lot_count, 0, "0")} 个持仓批次</div></div>`,
            ];
          }),
          "当前还没有可展示的 sleeve 归因记录。"
        )}
      `,
    }),
    strategyTrialVerdict: surfaceCard({
      title: "系统自动试盘结论",
      kicker: "试盘审查",
      copy: "试盘工作台只保留本轮是否该继续、是否硬停机、现在该按哪个按钮处理；历史和周期明细默认折叠。",
      classes: "strategy-compact-card",
      actions: renderTrialVerdictActions(trialReviewActions, {
        trialGuardStatus: scalingRequirements.trial_guard_status,
        trialGuardHardStopActive: Boolean(trialGuardHardStop.active),
        trialVerdict,
      }),
      content: `
        ${summaryStrip([
          {
            label: "当前系统建议",
            value: scalingVerdictLabel(trialVerdict),
            meta: trialHeadline,
            tone: scalingVerdictTone(trialVerdict),
          },
          {
            label: "最近净收益",
            value: formatSigned(latestForwardPeriod.net_realized_pnl ?? trialReviewSummary.net_realized_pnl),
            meta: `${formatNumber(latestForwardPeriod.closed_fill_count ?? trialReviewSummary.closed_fill_count, 0, "0")} 笔已完成成交`,
            tone: Number((latestForwardPeriod.net_realized_pnl ?? trialReviewSummary.net_realized_pnl) ?? 0) >= 0 ? "positive" : "warning",
          },
          {
            label: "费用拖累",
            value: formatRatio(latestForwardPeriod.fee_to_notional_ratio ?? trialReviewSummary.fee_to_notional_ratio),
            meta: `平均滑点 ${formatBps(latestForwardPeriod.avg_adverse_slippage_bps)}`,
            tone: "info",
          },
          {
            label: "试盘守护硬停机",
            value: trialGuardHardStopLabel(trialGuardHardStop, scalingRequirements.trial_guard_status),
            meta: trialGuardHardStop.summary || "当前没有命中试盘守护硬停机阈值。",
            tone: trialGuardHardStop.active ? "danger" : scalingRequirements.trial_guard_status === "warming_up" ? "warning" : "positive",
          },
          {
            label: "运行前置条件",
            value: runtimeConstraints.can_continue_runtime ? "当前可继续观察" : "当前先不要推进",
            meta: `${scalingRequirements.review_required ? "仍需人工复核" : "当前无需人工复核"} | 阻断 ${formatNumber(scalingRequirements.active_blocker_count, 0, "0")} 项`,
            tone: runtimeConstraints.can_continue_runtime ? "positive" : "danger",
          },
        ])}
        ${kvList([
          [
            "为什么系统给出这个结论",
            trialHeadline,
            reasonListText(trialReasons, "当前没有额外原因说明"),
          ],
          [
            "试盘守护硬停机",
            trialGuardHardStopLabel(trialGuardHardStop, scalingRequirements.trial_guard_status),
            hardStopRequirementText(trialGuardHardStop),
          ],
          [
            "最近观察周期",
            forwardVerdictLabel(forwardSummary.verdict),
            reasonListText(forwardSummary.reasons, "当前还没有形成额外的周期说明"),
          ],
          [
            "当前运行前置条件",
            trialObservationLabel(scalingRequirements),
            [
              runtimeConstraints.can_continue_runtime ? "当前运行前置条件已满足" : "当前运行前置条件仍受限",
              reasonListText(runtimeConstraints.reasons, scalingRequirements.safe_to_trade ? "恢复状态允许继续运行" : "恢复状态暂不允许继续自动运行"),
              Number(scalingRequirements.active_blocker_count || 0) > 0 ? `仍有 ${formatNumber(scalingRequirements.active_blocker_count, 0, "0")} 项阻断未清除` : "当前没有新的执行阻断",
            ].join("；"),
          ],
          [
            "最近一周复盘参考",
            plainListText(trialReviewRecommendation.action_items, "当前没有新的周度动作建议"),
            trialReviewSummary.headline || reasonListText(trialReviewRecommendation.reasons, "当前没有额外复盘说明"),
          ],
          [
            "最近处理记录",
            trialReviewLatestAction.label || "当前还没有新的试盘处理记录",
            [
              trialReviewLatestAction.created_at ? `记录时间 ${formatRelativeAge(trialReviewLatestAction.created_at)}` : "",
              trialReviewLatestAction.actor_identity ? `操作人 ${trialReviewLatestAction.actor_identity}` : "",
              trialReviewLatestAction.reason ? `原因 ${trialReviewLatestAction.reason}` : "",
            ].filter(Boolean).join("；") || "当前还没有新的试盘处理记录",
          ],
          ["最强 / 最弱分层切片", formatSegmentLabel(trialReviewSections.strategy_segments?.strongest_segment?.segment), `${formatSigned(trialReviewSections.strategy_segments?.strongest_segment?.net_realized_pnl)} | ${formatSigned(trialReviewSections.strategy_segments?.weakest_segment?.net_realized_pnl)}`],
        ])}
        ${renderExpandableSection("最近观察周期", renderForwardValidationPeriods(displayedForwardPeriods), {
          meta: `${formatNumber(displayedForwardPeriods.length, 0, "0")} 个周期`,
          open: true,
        })}
        ${renderExpandableSection("最近处理记录", renderTrialReviewHistory(displayedTrialReviewActions), {
          meta: `${formatNumber(displayedTrialReviewActions.length, 0, "0")} 条`,
          open: true,
        })}
      `,
    }),
    strategyHistory: surfaceCard({
      title: "决策记录",
      kicker: "历史记录",
      copy: "这里只保留最近决策和快速详情；更老的记录继续按需展开，不再把历史本身放成主工作区。",
      classes: "strategy-compact-card",
      content: `${responsiveTable(
        decisionTableHeaders(decisionScene),
        recentDecisions.map((item) => [
          `<div><strong>${formatRelativeAge(item.decision_time)}</strong><div class="table-meta">${formatMaybeTimestamp(item.decision_time)}</div></div>`,
          `<div><strong>${item.symbol || "标的待确认"}</strong><div class="table-meta">${item.timeframe || "周期待确认"}</div></div>`,
          `<div><strong>${readableRecentIntent(item)}</strong><div class="table-meta">${recentDecisionNarrative(item, decisionScene)}</div></div>`,
          `<div class="inline-pills">${pill(item.policy_result ? "策略允许" : "策略拦截", item.policy_result ? "positive" : "danger")}${pill(item.risk_result ? "风控允许" : "风控拦截", item.risk_result ? "positive" : "danger")}</div>`,
          item.decision_id ? actionButton("查看详情", "inspect-decision", item.decision_id) : "",
        ]),
        "当前暂无决策记录。",
        recentDecisions.map((item) => ({
          kicker: "策略记录",
          title: `${readableRecentIntent(item)} | ${item.symbol || "标的待确认"}`,
          meta: `${formatRelativeAge(item.decision_time)} | ${item.timeframe || "周期待确认"}`,
          tone: item.policy_result && item.risk_result ? "positive" : item.risk_result || item.policy_result ? "warning" : "danger",
          badge: `<div class="inline-pills">${pill(item.policy_result ? "策略允许" : "策略拦截", item.policy_result ? "positive" : "danger")}${pill(item.risk_result ? "风控允许" : "风控拦截", item.risk_result ? "positive" : "danger")}</div>`,
          fields: [
            { label: "决策时间", value: formatMaybeTimestamp(item.decision_time), meta: formatRelativeAge(item.decision_time) },
            { label: "决策摘要", value: readableRecentIntent(item), meta: recentDecisionNarrative(item, decisionScene) },
          ],
          details: [
            { label: "标的", value: item.symbol || "标的待确认", meta: item.timeframe || "周期待确认" },
            { label: "策略结果", value: item.policy_result ? "允许执行" : "已阻断" },
            { label: "风控结果", value: item.risk_result ? "允许执行" : "已阻断" },
            { label: "决策编号", value: item.decision_id || "当前没有编号" },
          ],
          detailLabel: "展开本次决策详情",
          action: item.decision_id ? actionButton("查看详情", "inspect-decision", item.decision_id) : "",
        }))
      )}${renderPaginationFooter(recentPayload, {
        singular: "策略记录",
        loadAction: "load-more-decisions",
        collapseAction: "collapse-decisions",
      })}`,
    }),
  };
}

export function renderStrategyView(data) {
  const sections = renderStrategySections(data);
  const hasSmartArbitrageReference = Object.keys(
    data?.strategyRuntime?.configured_parameters?.smart_arbitrage || {}
  ).length > 0;
  const referenceCards = [
    `<div class="span-12">${sections.strategyTradeCosts}</div>`,
    `<div class="span-12">${sections.strategyDirectionalConfig}</div>`,
  ];
  if (hasSmartArbitrageReference) {
    referenceCards.push(
      `<div class="span-12">${sections.strategySmartArbitrageConfig}</div>`,
      `<div class="span-12">${sections.strategySmartArbitrageCost}</div>`
    );
  }
  return `
    <div class="workspace-stack strategy-workspace">
      <nav class="section-nav strategy-section-nav" aria-label="策略判断分区导航">
        <a class="section-nav__link" href="#strategy-overview">本轮结论</a>
        <a class="section-nav__link" href="#strategy-opportunities">当前机会</a>
        <a class="section-nav__link" href="#strategy-health">运行质量</a>
        <a class="section-nav__link" href="#strategy-reference">配置参考</a>
        <a class="section-nav__link" href="#strategy-history">历史归因</a>
      </nav>
      ${renderStrategyWorkspaceSection(
        "strategy-overview",
        "当前结论",
        "本轮策略到底想做什么",
        "先看当前决策、门禁和目标变化。这里回答的是“这轮会不会动手”，不是配置细节。 ",
        `
          <div class="panel-grid strategy-page-grid">
            <div class="span-7">${sections.strategyHero}</div>
            <div class="span-5">${sections.strategyDecisionWorkbench}</div>
          </div>
        `
      )}
      ${renderStrategyWorkspaceSection(
        "strategy-opportunities",
        "当前机会",
        "当前候选与自动调度",
        "这里只保留这轮真的会影响下单的候选、路由和 sleeve 状态；预算明细已经在卡片内部折叠。",
        `
          <div class="panel-grid strategy-page-grid">
            <div class="span-12">${sections.strategyCoordinator}</div>
          </div>
        `
      )}
      ${renderStrategyWorkspaceSection(
        "strategy-health",
        "运行质量",
        "试盘与自动运行状态",
        "这部分只回答一个问题：这条运行线现在适不适合继续放量，还是应该先缩容、暂停或复盘。",
        `
          <div class="panel-grid strategy-page-grid">
            <div class="span-7">${sections.strategyTrialVerdict}</div>
            <div class="span-5">${sections.strategyAutomation}</div>
          </div>
        `
      )}
      ${renderStrategyWorkspaceSection(
        "strategy-reference",
        "配置参考",
        "配置与成本参考",
        "默认折叠。只有在你需要调阈值、解释为什么不做、或者核对成本假设时，再展开这一层。",
        renderExpandableSection(
          "展开配置与成本参考",
          `
            <div class="panel-grid strategy-page-grid">
              ${referenceCards.join("")}
            </div>
          `,
          { meta: "默认折叠，避免配置卡占满主工作区" }
        )
      )}
      ${renderStrategyWorkspaceSection(
        "strategy-history",
        "历史归因",
        "归因与历史记录",
        "默认折叠。只有在复盘最近为什么赚钱/亏钱、或者核对历史策略输出时，再展开这一层。",
        renderExpandableSection(
          "展开归因与历史记录",
          `
            <div class="panel-grid strategy-page-grid">
              <div class="span-12">${sections.strategyAttribution}</div>
              <div class="span-12">${sections.strategyHistory}</div>
            </div>
          `,
          { meta: "默认折叠，保留复盘能力但不抢主视线" }
        )
      )}
    </div>
  `;
}

function renderStrategyWorkspaceSection(id, kicker, title, copy, content) {
  return `
    <section class="workspace-section strategy-workspace-section" id="${escapeHtml(id)}">
      <header class="strategy-workspace-section__head">
        <div class="strategy-workspace-section__copy">
          <p class="panel-kicker">${escapeHtml(kicker)}</p>
          <h2>${escapeHtml(title)}</h2>
          <p class="meta-copy">${escapeHtml(copy)}</p>
        </div>
      </header>
      ${content}
    </section>
  `;
}

function renderTradeCostConfigCard(config = {}) {
  const commonRows = tradeCostCommonConfigRows(config);
  const advancedRows = tradeCostAdvancedConfigRows(config);
  return surfaceCard({
    title: "统一交易成本配置",
    kicker: "全局手续费与磨损",
    copy: "现货趋势、定投、现货网格、合约趋势和智能套利都共享这条交易成本链路；这里的手续费值按 bps 填写，是百分比费率兜底，不是固定 USDT。实盘优先读取账户费率，拿不到时再回退到这里。",
    classes: "strategy-compact-card",
    content: `
      ${summaryStrip([
        {
          label: "现货费率",
          value: `${formatBps(config?.spot_maker_fee_bps)} / ${formatBps(config?.spot_taker_fee_bps)}`,
          meta: "现货趋势、定投、现货网格与套利现货腿",
          tone: "info",
        },
        {
          label: "保证金费率",
          value: `${formatBps(config?.margin_maker_fee_bps)} / ${formatBps(config?.margin_taker_fee_bps)}`,
          meta: "保证金现货与 margin-backed 反套现货腿",
          tone: "info",
        },
        {
          label: "合约费率",
          value: `${formatBps(config?.derivatives_maker_fee_bps)} / ${formatBps(config?.derivatives_taker_fee_bps)}`,
          meta: "合约趋势与智能套利对冲腿",
          tone: "info",
        },
        {
          label: "费率口径 / 来源",
          value: `${String(config?.rate_unit || "bps").toUpperCase()} | 8 = 0.08%`,
          meta: config?.live_fee_resolution === "account_schedule_fallback_to_configured"
            ? "实盘优先账户费率，失败时回退到配置兜底"
            : "当前按配置兜底费率解释",
          tone: "info",
        },
      ])}
      ${kvList([
        ["当前适用策略", "现货趋势 / 定投 / 现货网格 -> 现货；合约趋势 / 智能套利对冲腿 -> 合约；保证金反套 -> 保证金现货。", "同一个产品类型的费率、spread 和 slippage 会被统一复用。"],
        ["统一链路", "当前所有主策略都通过统一手续费解析器和磨损模型估算成本。", "这里填的是 bps 百分比费率兜底，修改后会同时影响趋势、定投、网格和智能套利。"],
        ["当前主要提示", tradeCostPrimaryRisk(config), tradeCostSecondaryRisk(config)],
      ])}
      ${responsiveTable(
        ["参数", "当前值", "适用范围", "风险提示"],
        commonRows,
        "当前没有统一交易成本配置。"
      )}
      ${renderExpandableSection(
        "高级参数",
        responsiveTable(
          ["参数", "当前值", "适用范围", "风险提示"],
          advancedRows,
          "当前没有额外交易成本参数。"
        ),
        {
          meta: "交割结算、提现与保守兜底说明",
        }
      )}
    `,
  });
}

function renderDirectionalShortConfigCard(config = {}, latestDecision = {}, decisionScene = "spot") {
  const baseline = latestDecision?.baseline_assessment || {};
  const target = latestDecision?.position_target || {};
  const shortingSupported = config?.shorting_runtime_supported === true;
  const shortConfigVisible = shortingSupported && (config?.product_type || decisionScene) === "derivatives";
  const shortBiasEnabled = config?.short_bias_enabled === true;
  const hedgeOverlayDecision = target?.hedge_overlay_decision || {};
  const overlayLabel = directionalOverlayLabel(config, hedgeOverlayDecision);
  const effectiveShortBiasEnabled = (
    config?.short_bias_enabled === true
    && !(Array.isArray(config?.runtime_shorting_blockers) && config.runtime_shorting_blockers.length)
  );
  const summaryItems = [
    {
      label: "做空能力",
      value: directionalShortCapabilityLabel(config, decisionScene),
      meta: directionalShortCapabilityMeta(config, decisionScene),
      tone: effectiveShortBiasEnabled ? "positive" : "warning",
    },
    {
      label: "当前偏空信号",
      value: directionalShortSignalLabel(baseline, latestDecision?.ai_assessment || {}),
      meta: directionalShortSignalMeta(baseline, latestDecision?.ai_assessment || {}),
      tone: baseline?.direction_bias === "short" ? "warning" : "info",
    },
    shortConfigVisible
      ? {
        label: "做空开仓门槛",
        value: directionalThresholdTriple(
          config?.short_entry_min_signal_edge_bps,
          config?.short_entry_alpha_min,
          config?.short_entry_confidence_min
        ),
        meta: `允许状态 ${localizeList(config?.short_entry_allowed_regimes || [], "、") || "全部"}`,
        tone: shortBiasEnabled ? "info" : "warning",
      }
      : {
        label: "当前运行说明",
        value: "当前运行域不支持自动做空",
        meta: "现货 cash 运行域不会生成方向空头仓位，也不会展示合约专属的 short 阈值。",
        tone: "warning",
      },
    shortConfigVisible
      ? {
        label: "做空反手门槛",
        value: directionalThresholdTriple(
          config?.short_reversal_min_signal_edge_bps,
          config?.short_reversal_alpha_min,
          config?.short_reversal_confidence_min
        ),
        meta: `当前执行 ${escapeHtml(readableFamilyExecutionSummary(target, "保持当前仓位"))}`,
        tone: shortBiasEnabled ? "info" : "warning",
      }
      : {
        label: "long/共享门槛",
        value: directionalThresholdTriple(
          config?.entry_min_signal_edge_bps,
          config?.entry_alpha_min,
          config?.entry_confidence_min
        ),
        meta: `允许状态 ${localizeList(config?.entry_allowed_regimes || [], "、") || "全部"}`,
        tone: "info",
    },
    {
      label: overlayLabel,
      value: directionalHedgeOverlayStatus(config, target, decisionScene),
      meta: directionalHedgeOverlayMeta(config, target, decisionScene),
      tone: directionalHedgeOverlayTone(config, target, decisionScene),
    },
  ];
  return surfaceCard({
    title: "方向策略做空能力",
    kicker: "做空开关与阈值",
    copy: `方向策略现在把 long 和 short 的开仓、加仓、反手阈值拆开配置。在合约 hedge mode 下，还会额外显示 ${overlayLabel} 的腿级状态。这里重点回答三件事：当前能不能自动做空，这轮为什么没有触发做空，以及 ${overlayLabel} 有没有介入。`,
    classes: "strategy-compact-card",
    content: `
      ${summaryStrip(summaryItems)}
      ${kvList([
        ["当前未触发原因", directionalShortReasonText(latestDecision, config, decisionScene), directionalShortReasonMeta(target)],
        ["当前路径", directionalCurrentPathSummary(latestDecision), directionalCurrentPathMeta(latestDecision)],
        [overlayLabel, directionalHedgeOverlayDetail(hedgeOverlayDecision, config, decisionScene, target), directionalHedgeOverlayDetailMeta(hedgeOverlayDecision, config, target)],
        [
          "阈值对比",
          directionalThresholdComparison(config),
          shortConfigVisible
            ? "上面一组是当前 long/共享阈值，下面一组是 short 独立阈值；现在不会再只靠一个布尔值决定能不能翻空。"
            : "当前运行域不支持自动做空；这里只保留 long/共享阈值，避免把合约 short 参数误当成现货配置。",
        ],
      ])}
      ${responsiveTable(
        ["参数", "当前值", "适用阶段", "说明"],
        directionalShortConfigRows(config),
        "当前没有方向策略做空参数。"
      )}
    `,
  });
}

function renderSmartArbitrageConfigCard(config = {}, tradeCosts = {}, familyStatus = {}) {
  const pairDefinitions = smartArbitrageConfigPairs(config);
  const risks = smartArbitrageConfigRisks(config, tradeCosts, familyStatus);
  const commonRows = smartArbitrageCommonConfigRows(config, tradeCosts);
  const advancedRows = smartArbitrageAdvancedConfigRows(config, tradeCosts);
  const enabled = config?.enabled === true;
  const runtimeSupported = familyStatus?.runtime_supported !== false;
  return surfaceCard({
    title: "智能套利配置",
    kicker: "运行参数",
    copy: runtimeSupported
      ? "这里展示智能套利自己的机会阈值、配对和持有窗口参数；当前默认按双向套利设计，正基差走现货-永续 carry，负基差走保证金反套。手续费、spread 和 slippage 已统一收口到上面的全局交易成本链路。"
      : "这里展示智能套利自己的机会阈值、配对和持有窗口参数；当前配置按双向套利设计，但自动双腿执行仍只在合约运行域生效。手续费、spread 和 slippage 已统一收口到上面的全局交易成本链路。",
    classes: "strategy-compact-card",
    content: `
      ${summaryStrip([
        {
          label: "策略开关",
          value: enabled ? "已启用" : "未启用",
          meta: enabled ? `当前按 ${formatNumber(pairDefinitions.length, 0, "0")} 组配对持续评估基差` : "当前不会生成新的智能套利候选",
          tone: enabled ? "positive" : "warning",
        },
        {
          label: "入场 / 退出阈值",
          value: `${formatBps(config?.basis_entry_bps)} / ${formatBps(config?.basis_exit_bps)}`,
          meta: smartArbitrageThresholdMeta(config),
          tone: smartArbitrageThresholdTone(config),
        },
        {
          label: "每次预算 / 单组上限",
          value: `${formatQuoteAmount(config?.quote_budget_per_trade)} / ${formatQuoteAmount(config?.max_pair_notional)}`,
          meta: `实际初始名义金额取两者较小值：${formatQuoteAmount(smartArbitrageEffectiveBudget(config), "暂未生效")}`,
          tone: "info",
        },
        {
          label: "负基差模式",
          value: smartArbitrageNegativeModeLabel(config),
          meta: smartArbitrageNegativeModeMeta(config),
          tone: smartArbitrageNegativeModeTone(config),
        },
      ])}
      ${kvList([
        ["当前生效范围", smartArbitrageEffectiveScopeLabel(config, familyStatus), smartArbitrageEffectiveScopeMeta(config, familyStatus)],
        ["配对定义", smartArbitragePairSummary(pairDefinitions), smartArbitragePairSummaryMeta(pairDefinitions)],
        ["统一交易成本", tradeCostCompactLabel(tradeCosts), "现货腿、保证金现货腿和对冲腿的手续费 / spread / slippage 已从智能套利专属配置剥离，统一使用全局 trade_costs。"],
        ["主要风险提示", risks[0] || "当前这组配置关系清晰，正负基差的自动执行链条都已经明确。", risks.slice(1).join("；") || "当前主要需要继续盯真实磨损、funding 和借币窗口，而不是再补旧的策略私有费用字段。"],
      ])}
      ${responsiveTable(
        ["参数", "当前值", "联动关系", "风险提示"],
        commonRows,
        "当前还没有智能套利配置。"
      )}
      ${renderExpandableSection(
        "高级参数",
        responsiveTable(
          ["参数", "当前值", "联动关系", "风险提示"],
          advancedRows,
          "当前没有额外高级参数。"
        ),
        {
          meta: "排序、并行、细分成本与负基差细节",
        }
      )}
    `,
  });
}

function directionalShortCapabilityLabel(config = {}, decisionScene = "spot") {
  const productType = config?.product_type || decisionScene;
  if (productType !== "derivatives" || config?.shorting_runtime_supported !== true) return "当前运行域不支持";
  if (Array.isArray(config?.runtime_shorting_blockers) && config.runtime_shorting_blockers.includes("kill_switch_active")) {
    return "配置允许，但当前运行线已暂停";
  }
  return config?.short_bias_enabled === true ? "配置允许自动做空" : "配置关闭自动做空";
}

function directionalShortCapabilityMeta(config = {}, decisionScene = "spot") {
  const productType = config?.product_type || decisionScene;
  if (productType !== "derivatives" || config?.shorting_runtime_supported !== true) {
    return "现货 cash 运行域当前不会生成方向空头仓位。";
  }
  if (config?.short_bias_enabled !== true) {
    return "当前运行域支持做空，但 direction short bias 开关仍是关闭状态。";
  }
  if (Array.isArray(config?.runtime_shorting_blockers) && config.runtime_shorting_blockers.includes("kill_switch_active")) {
    return "当前虽然是合约运行域，且配置允许做空，但 kill switch 正在阻断任何新增暴露。";
  }
  return "当前是合约运行域，且配置允许独立按 short 开仓、加仓和反手阈值触发做空；实际仍会受冷却、风控、only-reduce 和 kill switch 约束。";
}

function directionalShortSignalLabel(baseline = {}, ai = {}) {
  if (baseline?.direction_bias === "short") return "baseline 偏空";
  const aiEdge = Number(ai?.directional_edge);
  if (Number.isFinite(aiEdge) && aiEdge < 0) return "AI 偏空";
  if (baseline?.direction_bias === "flat") return "当前偏中性";
  return "当前不偏空";
}

function directionalShortSignalMeta(baseline = {}, ai = {}) {
  const parts = [];
  if (baseline?.direction_bias) parts.push(`baseline ${readableState(baseline.direction_bias)}`);
  if (baseline?.composite_alpha_score !== undefined && baseline?.composite_alpha_score !== null) {
    parts.push(`alpha ${formatNumber(Math.abs(Number(baseline.composite_alpha_score)), 2, "0.00")}`);
  }
  if (baseline?.confidence !== undefined && baseline?.confidence !== null) {
    parts.push(`置信度 ${formatNumber(baseline.confidence, 2, "0.00")}`);
  }
  if (ai?.directional_edge !== undefined && ai?.directional_edge !== null) {
    parts.push(`AI edge ${formatSigned(ai.directional_edge)}`);
  }
  return parts.join(" | ") || "当前没有足够的偏空信号摘要。";
}

function directionalThresholdTriple(edge, alpha, confidence) {
  return `edge ${formatBps(edge)} | alpha ${formatNumber(alpha, 2, "待确认")} | 置信度 ${formatNumber(confidence, 2, "待确认")}`;
}

function directionalShortReasonText(latestDecision = {}, config = {}, decisionScene = "spot") {
  const baseline = latestDecision?.baseline_assessment || {};
  const ai = latestDecision?.ai_assessment || {};
  const target = latestDecision?.position_target || {};
  const flags = Array.isArray(target?.guardrail_flags) ? target.guardrail_flags : [];
  const shortFlags = flags.filter((value) => String(value || "").startsWith("short_") || value === "short_bias_disabled");
  const productType = config?.product_type || decisionScene;
  const bearishSignal = baseline?.direction_bias === "short" || Number(ai?.directional_edge) < 0;
  if (productType !== "derivatives" || config?.shorting_runtime_supported !== true) {
    return "当前运行域不是合约，方向策略不会自动开空。";
  }
  if (target?.target_exposure_side === "short" || String(target?.position_intent || "").includes("short")) {
    return "当前这轮已经触发做空路径，系统会按开空、减空或反手做空执行。";
  }
  if (!bearishSignal) {
    return "当前这轮基础信号并不偏空，所以不会主动走做空路径。";
  }
  if (Array.isArray(config?.runtime_shorting_blockers) && config.runtime_shorting_blockers.includes("kill_switch_active")) {
    return "当前已经识别到偏空机会，但 kill switch 正在阻断任何新增暴露。";
  }
  if (shortFlags.length) {
    return summarizeLocalizedList(shortFlags, { limit: 4, suffix: "等阻断原因" });
  }
  if (config?.short_bias_enabled !== true) {
    return "当前已经识别到偏空机会，但方向策略做空开关仍是关闭状态。";
  }
  if (target?.position_intent === "reduce_long") {
    return "当前已经识别到偏空，但强度只够减多，还没有达到新开空或反手做空门槛。";
  }
  if (target?.position_intent === "hold") {
    return "当前虽然出现偏空信号，但还没有达到做空开仓或反手阈值。";
  }
  return "当前没有触发方向做空；通常是偏空信号、冷却、风控或已有持仓状态共同作用。";
}

function directionalShortReasonMeta(target = {}) {
  const flags = Array.isArray(target?.guardrail_flags) ? target.guardrail_flags : [];
  return flags.length
    ? `当前 guardrail：${localizeList(flags)}`
    : "当前没有额外的 short guardrail 标志。";
}

function directionalCurrentPathSummary(latestDecision = {}) {
  const baseline = latestDecision?.baseline_assessment || {};
  const target = latestDecision?.position_target || {};
  return [
    `baseline ${readableState(baseline?.direction_bias || "unknown")}`,
    `${readableState(target?.position_intent || "hold")}`,
    `目标 ${readableState(target?.target_exposure_side || "flat")}`,
  ].join(" -> ");
}

function directionalCurrentPathMeta(latestDecision = {}) {
  const target = latestDecision?.position_target || {};
  return `当前仓位 ${formatSigned(target?.current_position_qty)} | 目标仓位 ${formatSigned(target?.target_position_qty)} | 变化 ${formatSigned(target?.delta_position_qty)}`;
}

function directionalThresholdComparison(config = {}) {
  if (config?.shorting_runtime_supported !== true) {
    return `long/共享：${directionalThresholdTriple(config?.entry_min_signal_edge_bps, config?.entry_alpha_min, config?.entry_confidence_min)}；当前运行域不支持自动做空`;
  }
  return [
    `long/共享：${directionalThresholdTriple(config?.entry_min_signal_edge_bps, config?.entry_alpha_min, config?.entry_confidence_min)}`,
    `short：${directionalThresholdTriple(config?.short_entry_min_signal_edge_bps, config?.short_entry_alpha_min, config?.short_entry_confidence_min)}`,
  ].join("；");
}

function directionalShortConfigRows(config = {}) {
  if (config?.shorting_runtime_supported !== true) {
    return [
      [
        "运行说明",
        "当前运行域不支持自动做空",
        "适用范围",
        "现货运行域只展示做空能力说明；合约专属的 short 开仓、加仓和反手阈值不在这里展示。",
      ],
      [
        "long/共享开仓阈值",
        directionalThresholdTriple(
          config?.entry_min_signal_edge_bps,
          config?.entry_alpha_min,
          config?.entry_confidence_min
        ),
        "当前仍生效的方向开仓门槛",
        `允许状态 ${localizeList(config?.entry_allowed_regimes || [], "、") || "全部"}`,
      ],
      [
        "long/共享反手阈值",
        directionalThresholdTriple(
          config?.reversal_min_signal_edge_bps,
          config?.reversal_alpha_min,
          config?.reversal_confidence_min
        ),
        "当前仍生效的方向反手门槛",
        "现货只按 long/共享阈值评估减仓、持有和退出。",
      ],
      [
        "overlay",
        "当前运行域不支持",
        "对冲 overlay",
        "只有合约 hedge mode 才会启用这组 overlay；现货不会展示这些阈值。",
      ],
    ];
  }
  return [
    [
      "strategy_short_bias_enabled",
      config?.short_bias_enabled ? "true" : "false",
      "总开关",
      config?.shorting_runtime_supported
        ? "合约运行域才会真正用到这个开关；关闭后偏空信号最多只会减多，不会新开空。"
        : "当前运行域本身就不支持自动做空，这个开关会被忽略。",
    ],
    [
      "strategy_short_entry_allowed_regimes",
      localizeList(config?.short_entry_allowed_regimes || [], "、") || "全部",
      "新开空",
      "只有 baseline/AI 给出偏空，且市场状态属于这些 regime 时，方向策略才允许从空仓直接开空。",
    ],
    [
      "strategy_short_entry_*",
      directionalThresholdTriple(
        config?.short_entry_min_signal_edge_bps,
        config?.short_entry_alpha_min,
        config?.short_entry_confidence_min
      ),
      "新开空",
      "用于 flat -> short；现在不会再和做多开仓共用一组阈值。",
    ],
    [
      "strategy_short_scale_in_*",
      directionalThresholdTriple(
        config?.short_scale_in_min_signal_edge_bps,
        config?.short_scale_in_alpha_min,
        config?.short_scale_in_confidence_min
      ),
      "加空",
      "用于已有 short 持仓时继续加空；数值越高，系统越少追加空头。",
    ],
    [
      "strategy_short_reversal_*",
      directionalThresholdTriple(
        config?.short_reversal_min_signal_edge_bps,
        config?.short_reversal_alpha_min,
        config?.short_reversal_confidence_min
      ),
      "反手做空",
      "用于 long -> short；如果这组门槛过高，系统就会长期只减多不翻空。",
    ],
    [
      "strategy_hedge_overlay_enabled / strategy_hedge_overlay_mode",
      `${config?.hedge_overlay_enabled ? "true" : "false"} / ${escapeHtml(readableState(config?.hedge_overlay_mode || "protective"))}`,
      "overlay 总开关",
      config?.hedge_overlay_runtime_supported
        ? (
            config?.hedge_overlay_mode_ready === false
              ? "当前是合约 hedge mode，但所选 overlay 模式还没有单独启用；这轮只保留配置展示。"
              : "当前是合约 hedge mode，directional 可以在主腿之外额外挂上一条 overlay 腿。"
          )
        : "当前运行线不是合约 hedge mode，这组 overlay 配置不会真正生效。",
    ],
    [
      "strategy_hedge_opportunistic_rollout_stage / strategy_hedge_independent_rollout_stage",
      `${escapeHtml(readableState(config?.hedge_opportunistic_rollout_stage || "replay_only"))} / ${escapeHtml(readableState(config?.hedge_independent_rollout_stage || "replay_only"))}`,
      "灰度阶段",
      "机会型 overlay 按 replay_only / dry-run / live 分层放开；independent 当前阶段只允许到 dry-run，不允许直接进实盘。",
    ],
    [
      "overlay rollout",
      directionalOverlayRolloutSummary(config),
      "当前运行线",
      directionalOverlayRolloutMeta(config),
    ],
    [
      "strategy_hedge_protective_enabled",
      config?.hedge_protective_enabled ? "true" : "false",
      "protective 单独开关",
      "只有这个开关打开，且 overlay mode 选中 protective 时，系统才会真正评估保护腿。",
    ],
    [
      "strategy_hedge_open_threshold / strategy_hedge_close_threshold",
      `${formatNumber(config?.hedge_open_threshold, 2, "待确认")} / ${formatNumber(config?.hedge_close_threshold, 2, "待确认")}`,
      "protective 打开 / 收回",
      "压力分数超过 open 阈值才开保护腿；回落到 close 阈值下方后，系统才会考虑把保护腿收回。",
    ],
    [
      "strategy_hedge_max_ratio / strategy_hedge_min_hold_seconds / strategy_hedge_rebalance_cooldown_seconds",
      `${formatRatio(config?.hedge_max_ratio)} / ${formatDuration(config?.hedge_min_hold_seconds, "待确认")} / ${formatDuration(config?.hedge_rebalance_cooldown_seconds, "待确认")}`,
      "protective 比例 / 最小持有 / 重平衡冷却",
      "max ratio 控制保护腿最多覆盖主腿多少；最小持有和重平衡冷却用于避免保护腿刚开就被频繁来回改动。",
    ],
    [
      "strategy_hedge_opportunistic_enabled",
      config?.hedge_opportunistic_enabled ? "true" : "false",
      "机会腿单独开关",
      "只有这个开关打开，且 overlay mode 选中 opportunistic 时，系统才会真正评估机会腿。",
    ],
    [
      "strategy_hedge_opportunistic_open_threshold / strategy_hedge_opportunistic_close_threshold",
      `${formatNumber(config?.hedge_opportunistic_open_threshold, 2, "待确认")} / ${formatNumber(config?.hedge_opportunistic_close_threshold, 2, "待确认")}`,
      "opportunistic 打开 / 收回",
      "机会分数超过 open 阈值才开机会腿；回落到 close 阈值下方后，系统才会考虑把机会腿收回。",
    ],
    [
      "strategy_hedge_opportunistic_max_ratio / strategy_hedge_opportunistic_min_hold_seconds / strategy_hedge_opportunistic_rebalance_cooldown_seconds",
      `${formatRatio(config?.hedge_opportunistic_max_ratio)} / ${formatDuration(config?.hedge_opportunistic_min_hold_seconds, "待确认")} / ${formatDuration(config?.hedge_opportunistic_rebalance_cooldown_seconds, "待确认")}`,
      "机会腿比例 / 最小持有 / 重平衡冷却",
      `机会腿仍受独立比例、最小持有和冷却约束；同时还会额外受费耗上限 ${formatRatio(config?.hedge_opportunistic_max_fee_drag_ratio)} / churn 上限 ${formatRatio(config?.hedge_opportunistic_max_churn_ratio)} 约束。`,
    ],
    [
      "strategy_hedge_opportunistic_min_safe_net_edge_bps / strategy_hedge_opportunistic_expected_slippage_buffer_bps / strategy_hedge_opportunistic_expected_execution_buffer_bps",
      `${formatNumber(config?.hedge_opportunistic_min_safe_net_edge_bps, 2, "待确认")} / ${formatNumber(config?.hedge_opportunistic_expected_slippage_buffer_bps, 2, "待确认")} / ${formatNumber(config?.hedge_opportunistic_expected_execution_buffer_bps, 2, "待确认")}`,
      "机会腿净边际安全垫 / 滑点缓冲 / 执行缓冲",
      "机会腿现在也会先检查预期净边际是否覆盖安全净边际、预估滑点与执行缓冲，而不是只看机会分本身。",
    ],
    [
      "strategy_hedge_opportunistic_weak_edge_execution_mode / strategy_hedge_opportunistic_max_acceptable_cost_bps / strategy_hedge_opportunistic_passive_first_enabled",
      `${String(config?.hedge_opportunistic_weak_edge_execution_mode || "待确认")} / ${formatNumber(config?.hedge_opportunistic_max_acceptable_cost_bps, 2, "待确认")} / ${config?.hedge_opportunistic_passive_first_enabled ? "true" : "false"}`,
      "机会腿弱边际执行 / 成本上限 / 被动优先",
      "当机会腿边际偏弱时，系统会根据这组约束决定是直接阻止、仅保留报告，还是要求 planner 优先走更保守的被动执行。",
    ],
    [
      "strategy_hedge_independent_enabled",
      config?.hedge_independent_enabled ? "true" : "false",
      "独立双书总开关",
      "只有这个开关打开，且 overlay mode 选中 independent 时，long book / short book 才会按各自状态机独立运行。",
    ],
    [
      "strategy_hedge_independent_long_entry_threshold / strategy_hedge_independent_short_entry_threshold",
      `${formatNumber(config?.hedge_independent_long_entry_threshold, 2, "待确认")} / ${formatNumber(config?.hedge_independent_short_entry_threshold, 2, "待确认")}`,
      "双书开仓阈值",
      "这组阈值分别决定 long book / short book 什么时候允许独立开仓。",
    ],
    [
      "strategy_hedge_independent_long_scale_in_threshold / strategy_hedge_independent_short_scale_in_threshold",
      `${formatNumber(config?.hedge_independent_long_scale_in_threshold, 2, "待确认")} / ${formatNumber(config?.hedge_independent_short_scale_in_threshold, 2, "待确认")}`,
      "双书加仓阈值",
      "只有当某一边自己的双书分继续抬高时，系统才会独立放大该腿。",
    ],
    [
      "strategy_hedge_independent_long_close_threshold / strategy_hedge_independent_short_close_threshold",
      `${formatNumber(config?.hedge_independent_long_close_threshold, 2, "待确认")} / ${formatNumber(config?.hedge_independent_short_close_threshold, 2, "待确认")}`,
      "双书收回阈值",
      "只有当对应 book 的双书分回落到 close 阈值下方后，系统才会考虑独立收回该腿。",
    ],
    [
      "strategy_hedge_independent_long_min_hold_seconds / strategy_hedge_independent_short_min_hold_seconds / strategy_hedge_independent_rebalance_cooldown_seconds",
      `${formatDuration(config?.hedge_independent_long_min_hold_seconds, "待确认")} / ${formatDuration(config?.hedge_independent_short_min_hold_seconds, "待确认")} / ${formatDuration(config?.hedge_independent_rebalance_cooldown_seconds, "待确认")}`,
      "双书最小持有 / 重平衡冷却",
      `long / short 会分别遵守自己的最小持有；此外两边共用 ${formatDuration(config?.hedge_independent_rebalance_cooldown_seconds, "待确认")} 的重平衡冷却。`,
    ],
    [
      "strategy_hedge_independent_trial_guard_enabled",
      config?.hedge_independent_trial_guard_enabled ? "true" : "false",
      "腿级试盘守护",
      "独立双书会按 long / short 两条腿分别评估样本、胜率和近期净收益，不再把一条腿的坏表现直接扩散到另一条腿。",
    ],
    [
      "strategy_hedge_independent_min_confirm_ticks / strategy_hedge_independent_min_score_stability_bps / strategy_hedge_independent_min_liquidity_quality",
      `${formatNumber(config?.hedge_independent_min_confirm_ticks, 0, "待确认")} / ${formatNumber(config?.hedge_independent_min_score_stability_bps, 2, "待确认")} / ${formatNumber(config?.hedge_independent_min_liquidity_quality, 2, "待确认")}`,
      "确认次数 / 稳定性 / 流动性门槛",
      "独立双书开仓前，会要求当前机会具备足够确认次数、分数稳定性和流动性质量，避免只凭刚过线的一跳噪声直接入场。",
    ],
    [
      "strategy_hedge_independent_require_execution_health_ok",
      config?.hedge_independent_require_execution_health_ok ? "true" : "false",
      "执行健康度必须正常",
      "打开后，long / short 任一腿如果近期执行健康度已经退化，本轮就不会继续新增独立风险暴露。",
    ],
    [
      "strategy_hedge_independent_max_thesis_age_seconds / strategy_hedge_independent_de_risk_net_edge_bps / strategy_hedge_independent_failed_thesis_net_edge_bps",
      `${formatDuration(config?.hedge_independent_max_thesis_age_seconds, "待确认")} / ${formatNumber(config?.hedge_independent_de_risk_net_edge_bps, 2, "待确认")} / ${formatNumber(config?.hedge_independent_failed_thesis_net_edge_bps, 2, "待确认")}`,
      "thesis 时效 / 降风险边际 / 失败边际",
      "独立双书退出已升级成 thesis-aware state machine，会按 thesis 时效、边际变薄和 thesis 失效三类路径决定 hold、de-risk 或 close。",
    ],
    [
      "strategy_hedge_independent_execution_health_de_risk_enabled / strategy_hedge_independent_liquidity_de_risk_enabled",
      `${config?.hedge_independent_execution_health_de_risk_enabled ? "true" : "false"} / ${config?.hedge_independent_liquidity_de_risk_enabled ? "true" : "false"}`,
      "执行健康 / 流动性降风险开关",
      "打开后，执行健康度或流动性一旦恶化，独立双书会优先降风险，而不是继续维持原始风险暴露。",
    ],
    [
      "strategy_hedge_independent_min_safe_net_edge_bps / strategy_hedge_independent_expected_slippage_buffer_bps / strategy_hedge_independent_expected_execution_buffer_bps",
      `${formatNumber(config?.hedge_independent_min_safe_net_edge_bps, 2, "待确认")} / ${formatNumber(config?.hedge_independent_expected_slippage_buffer_bps, 2, "待确认")} / ${formatNumber(config?.hedge_independent_expected_execution_buffer_bps, 2, "待确认")}`,
      "净边际安全垫 / 滑点缓冲 / 执行缓冲",
      "独立双书会先要求预期净边际覆盖安全净边际、预估滑点和执行缓冲，再决定是否允许开腿。",
    ],
    [
      "strategy_hedge_independent_weak_edge_execution_mode / strategy_hedge_independent_max_acceptable_cost_bps / strategy_hedge_independent_passive_first_enabled",
      `${String(config?.hedge_independent_weak_edge_execution_mode || "待确认")} / ${formatNumber(config?.hedge_independent_max_acceptable_cost_bps, 2, "待确认")} / ${config?.hedge_independent_passive_first_enabled ? "true" : "false"}`,
      "弱边际执行 / 成本上限 / 被动优先",
      "当双书边际偏弱时，系统会根据这组约束决定是否只做报告、限制可接受成本，并优先尝试更保守的被动执行。",
    ],
    [
      "strategy_hedge_independent_entry_execution_mode / strategy_hedge_independent_scale_in_execution_mode / strategy_hedge_independent_de_risk_execution_mode / strategy_hedge_independent_close_failed_thesis_execution_mode / strategy_hedge_independent_close_stale_execution_mode",
      `${String(config?.hedge_independent_entry_execution_mode || "待确认")} / ${String(config?.hedge_independent_scale_in_execution_mode || "待确认")} / ${String(config?.hedge_independent_de_risk_execution_mode || "待确认")} / ${String(config?.hedge_independent_close_failed_thesis_execution_mode || "待确认")} / ${String(config?.hedge_independent_close_stale_execution_mode || "待确认")}`,
      "开仓 / 加仓 / 降风险 / thesis失效 / thesis过期",
      "这组 execution mode 决定 independent 在不同 book_action 下采用哪一类执行风格，而不是继续共用单一的弱边际分支。",
    ],
    [
      "strategy_hedge_independent_limit_offset_bps_entry / strategy_hedge_independent_limit_offset_bps_scale_in / strategy_hedge_independent_limit_offset_bps_stale_close",
      `${formatNumber(config?.hedge_independent_limit_offset_bps_entry, 2, "待确认")} / ${formatNumber(config?.hedge_independent_limit_offset_bps_scale_in, 2, "待确认")} / ${formatNumber(config?.hedge_independent_limit_offset_bps_stale_close, 2, "待确认")}`,
      "开仓 / 加仓 / stale close 限价偏移",
      "当 independent 使用 bounded-limit IOC 路径时，会按这三组偏移值分别约束 entry、scale-in 和 stale thesis close 的限价保护。",
    ],
    [
      "strategy_hedge_independent_emit_book_level_metrics / strategy_hedge_independent_emit_expected_vs_realized_metrics / strategy_hedge_independent_emit_close_reason_metrics / strategy_hedge_independent_emit_execution_policy_metrics",
      `${config?.hedge_independent_emit_book_level_metrics ? "true" : "false"} / ${config?.hedge_independent_emit_expected_vs_realized_metrics ? "true" : "false"} / ${config?.hedge_independent_emit_close_reason_metrics ? "true" : "false"} / ${config?.hedge_independent_emit_execution_policy_metrics ? "true" : "false"}`,
      "book级 / 预期对比 / 退出原因 / 执行策略诊断",
      "打开后，runtime、决策详情和 replay 校验会持续输出独立双书的预期与已实现对比、退出原因分布和执行策略使用情况。",
    ],
  ];
}

function directionalHedgeOverlayStatus(config = {}, target = {}, decisionScene = "spot") {
  const overlay = target?.hedge_overlay_decision || {};
  const mode = directionalOverlayMode(config, overlay);
  const legLabel = directionalOverlayLegLabel(config, overlay);
  const enabledInMode = directionalOverlayEnabledInMode(config, overlay);
  const modeReady = directionalOverlayModeReady(config, overlay);
  if (config?.hedge_overlay_enabled !== true) return "未启用";
  if ((config?.product_type || decisionScene) !== "derivatives" || config?.hedge_overlay_runtime_supported !== true) {
    return "当前运行域不支持";
  }
  if (!enabledInMode) return "当前模式未单独打开";
  if (config?.hedge_overlay_rollout_allowed === false) return "当前阶段未放开";
  if (!modeReady) return "当前模式未放开";
  if (overlay?.active === true) {
    if (mode === "independent") return `独立双书${readableState(overlay?.state || "active")}`;
    return `${legLabel}${readableState(overlay?.state || "active")}`;
  }
  if (overlay?.blocked_reasons?.length) {
    return mode === "independent" ? "独立双书已启用，但这轮被拦住" : "已启用，但这轮被拦住";
  }
  if (overlay?.state === "inactive") return "已启用，当前未介入";
  return `已启用（${escapeHtml(readableState(config?.hedge_overlay_mode || "protective"))}）`;
}

function directionalHedgeOverlayMeta(config = {}, target = {}, decisionScene = "spot") {
  const overlay = target?.hedge_overlay_decision || {};
  const overlayLabel = directionalOverlayLabel(config, overlay);
  const mode = directionalOverlayMode(config, overlay);
  const enabledInMode = directionalOverlayEnabledInMode(config, overlay);
  const overlayReasonSummary = Array.isArray(overlay?.reason_codes) && overlay.reason_codes.length
    ? summarizeLocalizedList(overlay.reason_codes, { limit: 3, suffix: "等状态说明" })
    : "";
  const expectancySummary = readableBookExpectancySummary(target, "");
  if (config?.hedge_overlay_enabled !== true) {
    return "当前不会在主腿外额外生成 overlay 腿。";
  }
  if ((config?.product_type || decisionScene) !== "derivatives" || config?.hedge_overlay_runtime_supported !== true) {
    return "只有合约 hedge mode 才会真正启用这组 overlay。";
  }
  if (!enabledInMode) {
    return `当前总开关已打开，但 ${overlayLabel} 这一路还没有单独启用。`;
  }
  if (config?.hedge_overlay_rollout_allowed === false) {
    return directionalOverlayRolloutMeta(config);
  }
  if (Array.isArray(overlay?.blocked_reasons) && overlay.blocked_reasons.length) {
    return summarizeLocalizedList(overlay.blocked_reasons, { limit: 3, suffix: "等阻断原因" });
  }
  if (mode === "independent") {
    const closeReasonLabel = overlay?.close_reason ? readableState(overlay.close_reason) : "";
    return `long open ${formatNumber(config?.hedge_independent_long_entry_threshold, 2, "待确认")} / short open ${formatNumber(config?.hedge_independent_short_entry_threshold, 2, "待确认")} / long close ${formatNumber(config?.hedge_independent_long_close_threshold, 2, "待确认")} / short close ${formatNumber(config?.hedge_independent_short_close_threshold, 2, "待确认")} / stale ${formatDuration(config?.hedge_independent_max_thesis_age_seconds, "待确认")} / de-risk ${formatNumber(config?.hedge_independent_de_risk_net_edge_bps, 2, "待确认")} bps / failed ${formatNumber(config?.hedge_independent_failed_thesis_net_edge_bps, 2, "待确认")} bps / passive-first ${config?.hedge_independent_passive_first_enabled ? "true" : "false"}${closeReasonLabel ? ` | ${closeReasonLabel}` : ""}${expectancySummary ? ` | ${expectancySummary}` : ""}${overlayReasonSummary ? ` | ${overlayReasonSummary}` : ""}`;
  }
  if (mode === "opportunistic") {
    return `open ${formatNumber(config?.hedge_opportunistic_open_threshold, 2, "待确认")} / close ${formatNumber(config?.hedge_opportunistic_close_threshold, 2, "待确认")} / safe net ${formatNumber(config?.hedge_opportunistic_min_safe_net_edge_bps, 2, "待确认")} bps / max cost ${formatNumber(config?.hedge_opportunistic_max_acceptable_cost_bps, 2, "待确认")} bps / weak-edge ${escapeHtml(readableState(config?.hedge_opportunistic_weak_edge_execution_mode || "待确认"))} / passive-first ${config?.hedge_opportunistic_passive_first_enabled ? "true" : "false"}${expectancySummary ? ` | ${expectancySummary}` : ""}${overlayReasonSummary ? ` | ${overlayReasonSummary}` : ""}`;
  }
  if (overlayReasonSummary) {
    return overlayReasonSummary;
  }
  return `open ${formatNumber(config?.hedge_open_threshold, 2, "待确认")} / close ${formatNumber(config?.hedge_close_threshold, 2, "待确认")} / max ${formatRatio(config?.hedge_max_ratio)}`;
}

function directionalHedgeOverlayTone(config = {}, target = {}, decisionScene = "spot") {
  const overlay = target?.hedge_overlay_decision || {};
  const enabledInMode = directionalOverlayEnabledInMode(config, overlay);
  const modeReady = directionalOverlayModeReady(config, overlay);
  if (config?.hedge_overlay_enabled !== true) return "info";
  if ((config?.product_type || decisionScene) !== "derivatives" || config?.hedge_overlay_runtime_supported !== true) {
    return "warning";
  }
  if (!enabledInMode) return "warning";
  if (config?.hedge_overlay_rollout_allowed === false) return "warning";
  if (!modeReady) return "warning";
  if (overlay?.active === true) return "warning";
  if (Array.isArray(overlay?.blocked_reasons) && overlay.blocked_reasons.length) return "warning";
  return "info";
}

function directionalHedgeOverlayDetail(overlay = {}, config = {}, decisionScene = "spot", target = {}) {
  const overlayLabel = directionalOverlayLabel(config, overlay);
  const overlayLegLabel = directionalOverlayLegLabel(config, overlay);
  const mode = directionalOverlayMode(config, overlay);
  const enabledInMode = directionalOverlayEnabledInMode(config, overlay);
  if (config?.hedge_overlay_enabled !== true) {
    return "当前没有开启 overlay；方向策略只会按主腿目标执行。";
  }
  if ((config?.product_type || decisionScene) !== "derivatives" || config?.hedge_overlay_runtime_supported !== true) {
    return "当前运行线不是合约 hedge mode，这组 overlay 配置只保留展示，不会真的开额外腿。";
  }
  if (!enabledInMode) {
    return `当前 overlay 总开关已开，但 ${overlayLabel} 这一路还没有单独启用。`;
  }
  if (config?.hedge_overlay_rollout_allowed === false) {
    const rollout = directionalOverlayCurrentRollout(config, overlay);
    return [
      `当前运行线 ${escapeHtml(readableState(config?.hedge_rollout?.runtime_stage || "dry_run"))}`,
      `${overlayLabel} 只放开到 ${escapeHtml(readableState(rollout?.configured_rollout_stage || "replay_only"))}`,
      localizeList(rollout?.blocking_reasons || [], "、") || "当前阶段未放开",
    ].join(" | ");
  }
  if (!overlay || Object.keys(overlay).length === 0) {
    return `当前还没有 ${overlayLabel} 决策快照。`;
  }
  if (mode === "independent") {
    const books = directionalIndependentOverlayBooks(target, overlay);
    const runtimeStateSummary = readableBookRuntimeStateSummary(target, "");
    return [
      `long book ${formatSigned(books.long?.current_position_qty, 4, "0")}/${formatSigned(books.long?.target_position_qty, 4, "0")}`,
      `short book ${formatSigned(books.short?.current_position_qty, 4, "0")}/${formatSigned(books.short?.target_position_qty, 4, "0")}`,
      `状态 ${escapeHtml(readableState(overlay?.state || "disabled"))}`,
      runtimeStateSummary || null,
    ].filter(Boolean).join(" | ");
  }
  return [
    `主腿 ${readableState(overlay?.main_leg_signal || "flat")} ${formatSigned(overlay?.main_leg_current_qty)}/${formatSigned(overlay?.main_leg_target_qty)}`,
    `${overlayLegLabel} ${readableState(overlay?.hedge_leg_signal || "flat")} ${formatSigned(overlay?.hedge_leg_current_qty)}/${formatSigned(overlay?.hedge_leg_target_qty)}`,
    `对冲比例 ${formatRatio(overlay?.hedge_ratio)} / 上限 ${formatRatio(overlay?.max_ratio)}`,
  ].join(" | ");
}

function directionalHedgeOverlayDetailMeta(overlay = {}, config = {}, target = {}) {
  const mode = directionalOverlayMode(config, overlay);
  const scoreLabel = directionalOverlayScoreLabel(config, overlay);
  if (config?.hedge_overlay_enabled === true && config?.hedge_overlay_rollout_allowed === false) {
    const rollout = directionalOverlayCurrentRollout(config, overlay);
    return [
      rollout?.summary || "当前阶段未放开",
      `回滚顺序 ${(config?.hedge_rollout?.rollback_sequence || []).join(" -> ") || "先关各模式开关，再切回 protective"}`,
    ].join(" | ");
  }
  if (!overlay || Object.keys(overlay).length === 0) {
    return "等本轮方向目标进入 hedge mode 路径后，这里会展示 overlay 腿的状态与阻断原因。";
  }
  const timing = [];
  if (Number(overlay?.min_hold_remaining_seconds) > 0) {
    timing.push(`最小持有剩余 ${formatDuration(overlay.min_hold_remaining_seconds)}`);
  }
  if (Number(overlay?.rebalance_cooldown_remaining_seconds) > 0) {
    timing.push(`重平衡冷却剩余 ${formatDuration(overlay.rebalance_cooldown_remaining_seconds)}`);
  }
  const reasons = [];
  if (Array.isArray(overlay?.reason_codes) && overlay.reason_codes.length) {
    reasons.push(`状态 ${localizeList(overlay.reason_codes)}`);
  }
  if (Array.isArray(overlay?.blocked_reasons) && overlay.blocked_reasons.length) {
    reasons.push(`阻断 ${localizeList(overlay.blocked_reasons)}`);
  }
  if (mode === "independent") {
    const books = directionalIndependentOverlayBooks(target, overlay);
    if (Array.isArray(overlay?.long_leg_reason_codes) && overlay.long_leg_reason_codes.length) {
      reasons.push(`long ${overlay.long_leg_reason_codes.map(localizeError).join(" / ")}`);
    }
    if (Array.isArray(overlay?.short_leg_reason_codes) && overlay.short_leg_reason_codes.length) {
      reasons.push(`short ${overlay.short_leg_reason_codes.map(localizeError).join(" / ")}`);
    }
    if (Array.isArray(overlay?.long_leg_blocked_reasons) && overlay.long_leg_blocked_reasons.length) {
      reasons.push(`long 阻断 ${overlay.long_leg_blocked_reasons.map(localizeError).join(" / ")}`);
    }
    if (Array.isArray(overlay?.short_leg_blocked_reasons) && overlay.short_leg_blocked_reasons.length) {
      reasons.push(`short 阻断 ${overlay.short_leg_blocked_reasons.map(localizeError).join(" / ")}`);
    }
    reasons.push(
      `双书分 long ${formatNumber(overlay?.long_leg_score, 2, "0.00")} / short ${formatNumber(overlay?.short_leg_score, 2, "0.00")} | 目标 ${formatSigned(books.long?.target_position_qty, 4, "0")} / ${formatSigned(books.short?.target_position_qty, 4, "0")}`
    );
  } else if (overlay?.pressure_score !== undefined && overlay?.pressure_score !== null) {
    reasons.push(`${scoreLabel} ${formatNumber(overlay.pressure_score, 2, "0.00")} | open ${formatNumber(overlay.open_threshold, 2, "0.00")} / close ${formatNumber(overlay.close_threshold, 2, "0.00")}`);
  }
  const parentSignalSummary = readableOverlayParentSignalSummary(overlay, "");
  if (parentSignalSummary) {
    reasons.push(parentSignalSummary);
  }
  return [...timing, ...reasons].join(" | ") || "当前没有额外的 overlay 状态说明。";
}

function directionalOverlayMode(config = {}, overlay = {}) {
  return overlay?.effective_mode || overlay?.configured_mode || config?.hedge_overlay_mode || "protective";
}

function directionalOverlayEnabledInMode(config = {}, overlay = {}) {
  if (config?.hedge_overlay_enabled_in_mode === true) return true;
  if (config?.hedge_overlay_enabled_in_mode === false) return false;
  return directionalOverlayMode(config, overlay) === "protective";
}

function directionalOverlayModeReady(config = {}, overlay = {}) {
  if (config?.hedge_overlay_mode_ready === true) return true;
  if (config?.hedge_overlay_mode_ready === false) return false;
  return directionalOverlayEnabledInMode(config, overlay);
}

function directionalOverlayLabel(config = {}, overlay = {}) {
  const mode = directionalOverlayMode(config, overlay);
  if (mode === "opportunistic") return "机会型对冲";
  if (mode === "independent") return "独立双书";
  return "保护性对冲";
}

function directionalOverlayLegLabel(config = {}, overlay = {}) {
  const mode = directionalOverlayMode(config, overlay);
  if (mode === "opportunistic") return "机会腿";
  if (mode === "independent") return "双书腿";
  return "保护腿";
}

function directionalOverlayScoreLabel(config = {}, overlay = {}) {
  const mode = directionalOverlayMode(config, overlay);
  if (mode === "opportunistic") return "机会分";
  if (mode === "independent") return "双书分";
  return "压力";
}

function directionalOverlayCurrentRollout(config = {}, overlay = {}) {
  const rollout = config?.hedge_rollout || {};
  const mode = directionalOverlayMode(config, overlay);
  if (mode === "opportunistic") return rollout?.opportunistic || {};
  if (mode === "independent") return rollout?.independent || {};
  return {
    configured_rollout_stage: "live",
    runtime_allowed: true,
    blocking_reasons: [],
    summary: "保护性对冲不受本轮灰度阶段限制。",
  };
}

function directionalOverlayRolloutSummary(config = {}) {
  const rollout = config?.hedge_rollout || {};
  const currentMode = config?.hedge_overlay_mode || "protective";
  const currentAllowed = rollout?.current_mode_allowed !== false;
  return [
    `当前运行线 ${readableState(rollout?.runtime_stage || "dry_run")}`,
    `${directionalOverlayLabel({ hedge_overlay_mode: currentMode }, { effective_mode: currentMode })} ${currentAllowed ? "可用" : "受限"}`,
  ].join(" | ");
}

function directionalOverlayRolloutMeta(config = {}) {
  const rollout = config?.hedge_rollout || {};
  const currentMode = config?.hedge_overlay_mode || "protective";
  const current = directionalOverlayCurrentRollout(config, { effective_mode: currentMode });
  const blockers = Array.isArray(current?.blocking_reasons) ? current.blocking_reasons : [];
  return [
    current?.summary || "当前没有额外的 rollout 说明。",
    blockers.length ? `阻断 ${blockers.map(localizeError).join(" / ")}` : "当前模式没有额外灰度阻断。",
    `回滚顺序 ${(rollout?.rollback_sequence || []).join(" -> ") || "先关各模式开关，再切回 protective"}`,
  ].join(" | ");
}

function directionalIndependentOverlayBooks(target = {}, overlay = {}) {
  const items = Array.isArray(target?.strategy_execution_legs) ? target.strategy_execution_legs : [];
  const filtered = items.filter((item) => {
    const executionMode = String(item?.execution_mode || "");
    return item?.overlay_mode === "independent" || executionMode.startsWith("independent_");
  });
  const long = filtered.find((item) => item?.pos_side === "long") || {
    current_position_qty: overlay?.main_leg_signal === "long" ? overlay?.main_leg_current_qty : 0,
    target_position_qty: overlay?.main_leg_signal === "long" ? overlay?.main_leg_target_qty : 0,
  };
  const short = filtered.find((item) => item?.pos_side === "short") || {
    current_position_qty: overlay?.hedge_leg_signal === "short" ? -Number(overlay?.hedge_leg_current_qty || 0) : 0,
    target_position_qty: overlay?.hedge_leg_signal === "short" ? -Number(overlay?.hedge_leg_target_qty || 0) : 0,
  };
  return { long, short };
}

function renderSmartArbitrageCostCard(summary = {}) {
  const predicted = summary?.predicted || {};
  const realized = summary?.realized || {};
  const calibration = summary?.calibration || {};
  const available = summary?.available === true;
  return surfaceCard({
    title: "智能套利磨损模型",
    kicker: "理论收益与可执行收益",
    copy: available
      ? "这里把本轮智能套利的理论收益、可执行收益、主要磨损来源和预测偏差放在一起，方便判断为什么做或为什么不做。"
      : "当前还没有可展示的智能套利成本摘要；通常是本轮还没有产生智能套利候选。",
    classes: "strategy-compact-card",
    content: available
      ? `
        ${summaryStrip([
          {
            label: "理论净优势",
            value: formatBps(predicted?.ideal_edge_bps),
            meta: `理论总成本 ${formatBps(predicted?.ideal_cost_bps)}`,
            tone: Number(predicted?.ideal_edge_bps) > 0 ? "positive" : "warning",
          },
          {
            label: "可执行净优势",
            value: formatBps(predicted?.executable_edge_bps),
            meta: `可执行总磨损 ${formatBps(predicted?.executable_cost_bps)}`,
            tone: Number(predicted?.executable_edge_bps) > 0 ? "positive" : "danger",
          },
          {
            label: "盈亏平衡基差",
            value: formatBps(predicted?.breakeven_basis_bps),
            meta: `当前基差 ${formatBps(predicted?.basis_bps)}`,
            tone: "info",
          },
          {
            label: "实际总磨损",
            value: formatBps(realized?.realized_total_drag_bps, "待回填"),
            meta: `手续费 ${formatBps(realized?.realized_fee_bps, "待回填")} | 资金费 ${formatBps(realized?.realized_funding_bps, "待回填")}`,
            tone: realized?.realized_total_drag_bps === null || realized?.realized_total_drag_bps === undefined ? "info" : "warning",
          },
        ])}
        ${kvList([
          ["本轮配对", summary?.pair_label || "当前没有智能套利配对摘要", smartArbitragePrimaryDragDriver(predicted)],
          ["主要成本来源", smartArbitrageCostSourceText(predicted?.cost_source_flags), `置信度 ${formatNumber(predicted?.cost_confidence, 2, "0.00")}`],
          ["校准偏差", formatBps(calibration?.predicted_vs_realized_total_drag_error_bps, "待回填"), "正值代表预测磨损高于当前已回填磨损，负值代表预测偏保守不足。"],
        ])}
        ${responsiveTable(
          ["成本项", "预测值", "实际值", "说明"],
          [
            smartArbitrageCostRow("手续费", predicted?.ideal_total_fee_bps, realized?.realized_fee_bps, "双腿开平仓显性费用"),
            smartArbitrageCostRow("spread", predicted?.executable_spread_bps, null, "盘口价差带来的半边磨损"),
            smartArbitrageCostRow("slippage", predicted?.executable_slippage_bps, null, "按经验滑点或行情冲击估算"),
            smartArbitrageCostRow("腿间错配", predicted?.execution_mismatch_bps, null, "两腿不同步成交带来的额外磨损"),
            smartArbitrageCostRow("资金费", predicted?.funding_cost_bps, realized?.realized_funding_bps, "持仓跨 funding 窗口才会显著放大"),
            smartArbitrageCostRow("借币费", predicted?.borrow_cost_bps, realized?.realized_borrow_bps, "目前真实借币费仍按待支持处理"),
            smartArbitrageCostRow("transfer / time", sumBps(predicted?.transfer_cost_bps, predicted?.time_decay_cost_bps), null, "固定转移成本与持有时间磨损"),
          ],
          "当前没有可展示的磨损分解。"
        )}
      `
      : `<p class="meta-copy">当前没有智能套利成本摘要。通常是本轮没有可计算的智能套利候选，或智能套利当前未启用。</p>`,
  });
}

function smartArbitrageConfigPairs(config = {}) {
  return Array.isArray(config?.pair_definitions) ? config.pair_definitions : [];
}

function smartArbitragePairRegistrySourceLabel(config = {}) {
  return readableState(config?.pair_registry_source || "settings_fallback");
}

function smartArbitragePairConfigIssues(pairDefinitions = []) {
  return pairDefinitions.flatMap((item) => {
    const metadata = item?.metadata || {};
    const warningCodes = Array.isArray(metadata?.configuration_warning_codes) ? metadata.configuration_warning_codes : [];
    const errorCodes = Array.isArray(metadata?.configuration_error_codes) ? metadata.configuration_error_codes : [];
    if (!warningCodes.length && !errorCodes.length) return [];
    return [{
      pairId: item?.pair_id || "未命名配对",
      spotSymbol: item?.spot_symbol || "现货腿",
      hedgeSymbol: item?.hedge_symbol || "合约腿",
      warningCodes,
      errorCodes,
      metadata,
    }];
  });
}

function tradeCostCommonConfigRows(config = {}) {
  return [
    tradeCostConfigRow(
      "trade_cost_spot_maker_fee_bps / trade_cost_spot_taker_fee_bps",
      "现货 maker / taker",
      `${formatBps(config?.spot_maker_fee_bps, "待确认")} / ${formatBps(config?.spot_taker_fee_bps, "待确认")}`,
      "现货趋势、定投、现货网格和智能套利现货腿都会读取这里的默认手续费。",
      Number(config?.spot_taker_fee_bps) < Number(config?.spot_maker_fee_bps)
        ? "taker 费率不应低于 maker；请检查配置是否填反。"
        : "现货单腿大多更适合保守按 taker 估算。"
    ),
    tradeCostConfigRow(
      "trade_cost_margin_maker_fee_bps / trade_cost_margin_taker_fee_bps",
      "保证金现货 maker / taker",
      `${formatBps(config?.margin_maker_fee_bps, "待确认")} / ${formatBps(config?.margin_taker_fee_bps, "待确认")}`,
      "主要用于保证金现货腿和 margin-backed 智能套利，不影响纯现金现货策略。",
      Number(config?.margin_taker_fee_bps) < Number(config?.margin_maker_fee_bps)
        ? "taker 费率不应低于 maker；请检查配置是否填反。"
        : "保证金现货腿和现金现货腿分开配置后，反套成本解释会更准确。"
    ),
    tradeCostConfigRow(
      "trade_cost_derivatives_maker_fee_bps / trade_cost_derivatives_taker_fee_bps",
      "合约 maker / taker",
      `${formatBps(config?.derivatives_maker_fee_bps, "待确认")} / ${formatBps(config?.derivatives_taker_fee_bps, "待确认")}`,
      "合约趋势和智能套利对冲腿都会读取这里的默认手续费。",
      Number(config?.derivatives_taker_fee_bps) < Number(config?.derivatives_maker_fee_bps)
        ? "taker 费率不应低于 maker；请检查配置是否填反。"
        : "合约腿更依赖这一组费率，不再走策略私有的磨损配置。"
    ),
    tradeCostConfigRow(
      "trade_cost_spot_spread_bps / trade_cost_spot_slippage_bps",
      "现货 spread / slippage",
      `${formatBps(config?.spot_spread_bps, "待确认")} / ${formatBps(config?.spot_slippage_bps, "待确认")}`,
      "现货趋势、定投、现货网格和智能套利现货腿的执行磨损统一从这里读取。",
      sumBps(config?.spot_spread_bps, config?.spot_slippage_bps) <= 0
        ? "当前现货执行磨损为 0，理论收益和可执行收益会更接近，通常偏乐观。"
        : "spread 与 slippage 已分开计量，更方便回头校准。"
    ),
    tradeCostConfigRow(
      "trade_cost_margin_spread_bps / trade_cost_margin_slippage_bps",
      "保证金现货 spread / slippage",
      `${formatBps(config?.margin_spread_bps, "待确认")} / ${formatBps(config?.margin_slippage_bps, "待确认")}`,
      "主要服务于 margin-backed 反套和未来保证金现货执行链。",
      sumBps(config?.margin_spread_bps, config?.margin_slippage_bps) <= 0
        ? "当前保证金现货执行磨损为 0，负基差反套的可执行成本会被低估。"
        : "保证金现货单独建模后，不再和现金现货共用一组执行磨损。"
    ),
    tradeCostConfigRow(
      "trade_cost_derivatives_spread_bps / trade_cost_derivatives_slippage_bps",
      "合约 spread / slippage",
      `${formatBps(config?.derivatives_spread_bps, "待确认")} / ${formatBps(config?.derivatives_slippage_bps, "待确认")}`,
      "合约趋势和智能套利对冲腿统一从这里读取。",
      sumBps(config?.derivatives_spread_bps, config?.derivatives_slippage_bps) <= 0
        ? "当前合约执行磨损为 0，合约趋势和套利对冲腿的净优势会偏乐观。"
        : "这组参数会同时影响方向合约策略和智能套利对冲腿。"
    ),
  ];
}

function tradeCostAdvancedConfigRows(config = {}) {
  return [
    tradeCostConfigRow(
      "trade_cost_delivery_settlement_fee_bps",
      "交割合约结算费",
      formatBps(config?.delivery_settlement_fee_bps, "待确认"),
      "只在交割合约到期结算时参与成本链路；永续和现货不会读它。",
      Number(config?.delivery_settlement_fee_bps) > 0
        ? "如果后续引入交割合约策略，这个值会直接进入到期结算磨损。"
        : "当前保持 0 代表没有额外结算成本兜底。"
    ),
    tradeCostConfigRow(
      "trade_cost_*",
      "费率口径 / 实盘来源",
      `${String(config?.rate_unit || "bps").toUpperCase()} | ${config?.rate_example || "8 = 0.08%"}`,
      "配置文件里的统一交易成本按 bps 填写，本质上是百分比费率的万分之一；8 代表 0.08%，5 代表 0.05%。",
      config?.live_fee_resolution === "account_schedule_fallback_to_configured"
        ? "实盘优先读取账户费率 schedule，只有拿不到账户费率时才会回退到这组配置。"
        : "当前统一交易成本按配置兜底解释。"
    ),
  ];
}

function smartArbitrageCommonConfigRows(config = {}, tradeCosts = {}) {
  const pairDefinitions = smartArbitrageConfigPairs(config);
  const derivedPairs = pairDefinitions.filter((item) => item?.metadata?.source === "derived_primary_symbol");
  const pairIssues = smartArbitragePairConfigIssues(pairDefinitions);
  const enabled = config?.enabled === true;
  const negativeMode = String(config?.negative_basis_mode || "advisory_only");
  const inventoryEnabled = config?.inventory_reservation_enabled === true;
  const marginEnabled = config?.margin_short_enabled === true;
  const marginReady = config?.margin_short_execution_ready === true;
  return [
    smartArbitrageConfigRow(
      "smart_arbitrage_enabled",
      "策略总开关",
      enabled ? "true" : "false",
      enabled ? "打开后按配对定义持续评估现货/合约基差机会。" : "关闭时其余参数只保留展示，不参与机会评估。",
      enabled ? "启用后会持续生成智能套利候选，需要配合阈值和预算一起看。" : "当前不会生成新的智能套利候选。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_basis_entry_bps",
      "基差入场阈值",
      formatNumber(config?.basis_entry_bps, 1, "待确认"),
      "基差绝对值达到该阈值后，系统才会考虑新开套利对。",
      Number(config?.estimated_cost_bps) >= Number(config?.basis_entry_bps)
        ? "综合成本兜底已经接近或超过入场阈值，自动开新套利对会明显变少。"
        : "阈值越低越容易进场，但也越容易把噪声当机会。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_basis_exit_bps",
      "基差退出阈值",
      formatNumber(config?.basis_exit_bps, 1, "待确认"),
      "已持有套利对后，基差回归到这里附近时会开始退出或收口。",
      Number(config?.basis_exit_bps) >= Number(config?.basis_entry_bps)
        ? "退出阈值不应高于入场阈值，否则容易刚开仓就触发退出。"
        : "退出阈值越低，套利对持有时间通常越长。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_estimated_cost_bps",
      "综合成本兜底",
      formatNumber(config?.estimated_cost_bps, 1, "待确认"),
      "当细分费率模型没有完整给出结果时，用它作为净优势兜底成本。",
      Number(config?.estimated_cost_bps) > 0
        ? "这个值过低会高估机会，过高会错过机会。"
        : "若保持 0，前端会很难解释为什么系统认为净优势不足。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_quote_budget_per_trade",
      "每次套利预算",
      formatQuoteAmount(config?.quote_budget_per_trade),
      "和单组名义上限一起决定每次新开套利对的初始规模。",
      Number(config?.quote_budget_per_trade) > Number(config?.max_pair_notional)
        ? "当前预算高于单组上限，实际会被单组上限裁剪。"
        : "预算越大，单次开仓的资金占用越高。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_max_pair_notional",
      "单组套利硬上限",
      formatQuoteAmount(config?.max_pair_notional),
      `实际初始名义金额取它和每次预算里的较小值：${formatQuoteAmount(smartArbitrageEffectiveBudget(config), "暂未生效")}。`,
      Number(config?.max_pair_notional) < Number(config?.quote_budget_per_trade)
        ? "当前上限比预算更紧，预算不会完全放出来。"
        : "这是单组套利的最后一道名义金额限制。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_pair_definitions",
      "可交易配对定义",
      `${formatNumber(pairDefinitions.length, 0, "0")} 组`,
      `${smartArbitragePairSummaryMeta(pairDefinitions)} | 配对来源 ${smartArbitragePairRegistrySourceLabel(config)}`,
      pairIssues.length
        ? `当前有 ${formatNumber(pairIssues.length, 0, "0")} 组配对带配置告警，建议先处理完再放量。`
        : derivedPairs.length
        ? "有配对来自主标的自动推导；生产环境更建议显式写入 pair_definitions。"
        : "前端和执行链现在都按这组配对来解释和生成双腿。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_negative_basis_mode",
      "负基差模式",
      `${negativeMode} (${smartArbitrageNegativeModeLabel(config)})`,
      smartArbitrageNegativeModeMeta(config),
      negativeMode === "advisory_only" || negativeMode === "disabled"
        ? "当前负基差不会自动生成反向套利执行腿。"
        : "只有相关能力开关也满足时，这个模式才会真的生效。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_inventory_reservation_enabled",
      "库存反套开关",
      inventoryEnabled ? "true" : "false",
      negativeMode === "inventory_backed"
        ? "当前直接参与负基差库存反套能力判断。"
        : "只有 negative_basis_mode=inventory_backed 时它才会生效。",
      negativeMode === "inventory_backed" && !inventoryEnabled
        ? "你已经要求库存反套，但库存预留未启用，负基差会停在建议或阻断态。"
        : "启用后会占用真实现货库存。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_margin_short_enabled",
      "保证金融券能力声明",
      marginEnabled ? "true" : "false",
      negativeMode === "margin_backed"
        ? "当前直接参与保证金反套能力判断。"
        : "只有 negative_basis_mode=margin_backed 时它才会真正参与判断。",
      negativeMode === "margin_backed" && !marginEnabled
        ? "你已经要求保证金反套，但能力声明未启用，系统会直接阻断。"
        : "这是“允许做保证金现货腿”的前置声明，不等于执行链已经可用。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_margin_short_execution_ready",
      "保证金融券执行就绪",
      marginReady ? "true" : "false",
      "必须和保证金融券能力声明同时为 true，负基差才会进入 margin_backed 自动执行。",
      negativeMode === "margin_backed" && (!marginEnabled || !marginReady)
        ? "当前保证金反套配置链条不完整，负基差仍会被阻断。"
        : marginReady && !marginEnabled
          ? "执行就绪标记已开，但能力声明未开，这个标记当前不会生效。"
          : "只有真的跑通现货保证金卖空与恢复链路后，才应该打开它。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_margin_short_spot_margin_mode",
      "保证金现货腿模式",
      String(config?.margin_short_spot_margin_mode || "cross"),
      negativeMode === "margin_backed"
        ? "一旦走保证金反套，这个值决定现货腿使用全仓还是逐仓。"
        : "只有保证金反套真正启用时才会生效。",
      String(config?.margin_short_spot_margin_mode || "cross") === "isolated"
        ? "逐仓模式更严格，但也要求账户和恢复链路按逐仓语义一致。"
        : "首次上线通常先用 cross，更容易和账户总风险口径保持一致。"
    ),
    smartArbitrageConfigRow(
      "trade_costs.*",
      "统一手续费 / spread / slippage",
      tradeCostCompactLabel(tradeCosts),
      "现货腿、保证金现货腿和对冲腿的手续费、spread 和 slippage 已统一从 trade_costs 读取。",
      "修改全局交易成本会同时影响趋势、定投、网格和智能套利，不再是套利专属配置。"
    ),
  ];
}

function smartArbitrageAdvancedConfigRows(config = {}) {
  const negativeMode = String(config?.negative_basis_mode || "advisory_only");
  const costModelEnabled = config?.cost_model_enabled !== false;
  const fundingEnabled = config?.funding_cost_enabled === true;
  const borrowEnabled = config?.borrow_cost_enabled === true;
  const autoRepayEnabled = config?.margin_short_auto_repay_enabled === true;
  const maxConcurrentPairs = Math.max(Number(config?.max_concurrent_pairs) || 1, 1);
  const pairPriorityMode = String(config?.pair_priority_mode || "net_edge");
  const minInventoryRatio = Number(config?.min_inventory_backed_ratio);
  return [
    smartArbitrageConfigRow(
      "smart_arbitrage_cost_model_enabled",
      "细分成本模型开关",
      costModelEnabled ? "true" : "false",
      costModelEnabled
        ? "开启后会优先用 fee / slippage / funding / borrow 细分成本，再回退综合成本兜底。"
        : "关闭后只看综合成本兜底，不再区分费用来源。",
      costModelEnabled
        ? "若细分成本全是 0，系统仍会回退 estimated_cost_bps。"
        : "关闭后虽然更简单，但前端和 operator 很难解释净优势是怎么扣出来的。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_funding_cost_enabled",
      "资金费成本启用",
      fundingEnabled ? "true" : "false",
      "只在细分成本模型开启时有意义，用来决定是否把 funding bps 纳入净优势计算。",
      Number(config?.estimated_funding_bps) > 0 && !fundingEnabled
        ? "你已经填了 funding bps，但资金费成本开关没开，这部分当前不会真正参与计算。"
        : "不开启时，estimated_funding_bps 只会作为展示值存在。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_borrow_cost_enabled",
      "借币成本启用",
      borrowEnabled ? "true" : "false",
      "只在保证金反套或保证金融券路径上有意义，用来决定是否把 borrow bps 纳入净优势计算。",
      Number(config?.estimated_borrow_bps) > 0 && !borrowEnabled
        ? "你已经填了 borrow bps，但借币成本开关没开，这部分当前不会真正参与计算。"
        : "不开启时，保证金反套仍可评估，但借币成本不会被细分计入。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_fee_source_mode / smart_arbitrage_funding_source_mode / smart_arbitrage_borrow_source_mode",
      "成本来源模式",
      `${String(config?.fee_source_mode || "configured")} / ${String(config?.funding_source_mode || "configured")} / ${String(config?.borrow_source_mode || "configured")}`,
      "分别决定手续费、资金费、借币费优先按账户实时数据还是按配置参数建模。",
      "来源模式决定成本解释力，也决定细分成本模型到底有多接近真实账户。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_expected_hold_hours / smart_arbitrage_funding_interval_hours / smart_arbitrage_expected_funding_events",
      "持有与资金费窗口",
      `${formatNumber(config?.expected_hold_hours, 1, "待确认")}h / ${formatNumber(config?.funding_interval_hours, 1, "待确认")}h / ${formatNumber(config?.expected_funding_events, 0, "自动推导")}`,
      "决定 funding 事件数、borrow 窗口和 time-decay 的估算周期。",
      Number(config?.expected_hold_hours) > 24
        ? "持有窗口越长，funding 和 time-decay 越容易成为主要磨损来源。"
        : "短持有窗口更适合先把手续费、spread 和 slippage 估清楚。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_hedge_target_leverage",
      "对冲腿目标杠杆",
      `${formatNumber(config?.hedge_target_leverage, 1, "待确认")}x`,
      "只作用于智能套利合约对冲腿，不再复用 directional 的 default_target_leverage；现货腿仍固定按 1x 解释。",
      Number(config?.hedge_target_leverage) > 5
        ? "对冲腿杠杆较高，虽然不改变套利方向，但会明显放大保证金波动和被动减仓风险。"
        : "独立配置后，智能套利合约腿不会再被方向策略默认杠杆带偏。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_estimated_execution_mismatch_bps / smart_arbitrage_estimated_transfer_cost_bps / smart_arbitrage_time_decay_bps_per_hour",
      "错配 / transfer / 时间磨损",
      `${formatBps(config?.estimated_execution_mismatch_bps, "0 bps")} / ${formatBps(config?.estimated_transfer_cost_bps, "0 bps")} / ${formatBps(config?.time_decay_bps_per_hour, "0 bps/h")}`,
      "分别描述双腿不同步、固定转移成本和持有时长磨损。",
      "这三项往往是“理论上看起来能做，但系统最终不做”的关键原因。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_estimated_borrow_apr / smart_arbitrage_borrow_interest_free_ratio",
      "借币 APR / 免息比例",
      `${formatNumber(config?.estimated_borrow_apr, 2, "0")}% / ${formatNumber(config?.borrow_interest_free_ratio, 2, "0")}`,
      "只有 borrow_source_mode=apr_window_model 且负基差走保证金反套时，它才真正参与离散借币窗口估算。",
      Number(config?.estimated_borrow_apr) > 0 && String(config?.borrow_source_mode || "configured") !== "apr_window_model"
        ? "当前 APR 已配置，但借币来源还不是窗口模型，这个值暂时不会真正生效。"
        : "如果还没准备启用保证金反套，这组参数可以先保守维持。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_max_concurrent_pairs",
      "最多并行套利对",
      formatNumber(maxConcurrentPairs, 0, "1"),
      maxConcurrentPairs > 1
        ? "当前只会并行挑选 symbol scope 不重叠的套利对；共享现货腿或共享合约腿的 pair 不会一起新开。"
        : "当前一次只会新开或主控 1 组套利对。",
      maxConcurrentPairs > 1
        ? "把它调大不等于所有 pair 都会并行；共享标的的组合会被安全边界自动裁掉。"
        : "这是当前最稳妥的生产设置。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_pair_priority_mode",
      "pair 排序方式",
      pairPriorityMode,
      pairPriorityMode === "basis_abs"
        ? "当前优先看基差绝对值，而不是扣完成本后的净优势。"
        : pairPriorityMode === "ideal_edge"
        ? "当前优先看理论净优势，适合诊断，不是最保守的生产排序。"
        : "当前优先看净优势分数，成本越高的机会越容易被压后。",
      pairPriorityMode === "basis_abs"
        ? "只按基差绝对值排序更激进，可能把毛机会排在净优势更好的 pair 前面。"
        : pairPriorityMode === "ideal_edge"
          ? "只按理论净优势排序会弱化执行磨损影响，生产上应谨慎使用。"
        : "这是更偏保守的生产排序方式。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_min_inventory_backed_ratio",
      "库存反套最小覆盖率",
      Number.isFinite(minInventoryRatio) ? formatNumber(minInventoryRatio, 2, "待确认") : "待确认",
      "只有 inventory_backed 可用库存 / 目标 pair 数量达到这个比例，负基差才会真正开库存反套。",
      negativeMode === "inventory_backed" && Number.isFinite(minInventoryRatio) && minInventoryRatio > 1
        ? "比例大于 1 会明显提高库存反套门槛，很多机会会停在 blocked。"
        : "保持 1.0 代表至少要完全覆盖目标数量。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_margin_short_auto_repay_enabled",
      "保证金反套自动还币",
      autoRepayEnabled ? "true" : "false",
      negativeMode === "margin_backed"
        ? "只在保证金反套真正启用时有意义，用来决定平仓后是否自动走还币语义。"
        : "只有 negative_basis_mode=margin_backed 时它才会真正参与执行链。",
      autoRepayEnabled && negativeMode !== "margin_backed"
        ? "自动还币开关当前是闲置状态，因为负基差并没有走保证金反套。"
        : "只有确认账户和交易所适配层的还币语义已经打通后，才应该打开它。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_estimated_funding_bps",
      "细分资金费 bps",
      formatNumber(config?.estimated_funding_bps, 1, "待确认"),
      fundingEnabled
        ? "当前会把 funding bps 计入净优势。"
        : "只有 funding_cost_enabled=true 时它才会真正参与净优势计算。",
      Number(config?.estimated_funding_bps) > 0 && !fundingEnabled
        ? "当前填了资金费成本，但这部分并没有真的参与计算。"
        : "如果策略主要做短持有正基差，这一项通常可以先保守为 0。"
    ),
    smartArbitrageConfigRow(
      "smart_arbitrage_estimated_borrow_bps",
      "细分借币费 bps",
      formatNumber(config?.estimated_borrow_bps, 1, "待确认"),
      borrowEnabled
        ? "当前会在 margin_reverse_carry 上把 borrow bps 计入净优势。"
        : "只有 borrow_cost_enabled=true 时它才会真正参与保证金反套净优势计算。",
      Number(config?.estimated_borrow_bps) > 0 && !borrowEnabled
        ? "当前填了借币成本，但这部分并没有真的参与计算。"
        : "如果还没准备启用保证金反套，这个值可以先保持 0。"
    ),
  ];
}

function smartArbitrageConfigRow(parameter, alias, value, linkage, risk) {
  return [
    renderConfigTableCell(parameter, alias),
    renderConfigTableCell(value),
    renderConfigTableCell(linkage),
    renderConfigTableCell(risk),
  ];
}

function tradeCostConfigRow(parameter, alias, value, linkage, risk) {
  return smartArbitrageConfigRow(parameter, alias, value, linkage, risk);
}

function renderConfigTableCell(primary, meta = "") {
  return `<div><strong>${escapeHtml(primary)}</strong>${meta ? `<div class="table-meta">${escapeHtml(meta)}</div>` : ""}</div>`;
}

function tradeCostCompactLabel(config = {}) {
  return [
    `现货 taker ${formatBps(config?.spot_taker_fee_bps, "待确认")}`,
    `保证金 taker ${formatBps(config?.margin_taker_fee_bps, "待确认")}`,
    `合约 taker ${formatBps(config?.derivatives_taker_fee_bps, "待确认")}`,
  ].join(" | ");
}

function tradeCostPrimaryRisk(config = {}) {
  if (
    sumBps(
      config?.spot_spread_bps,
      config?.spot_slippage_bps,
      config?.margin_spread_bps,
      config?.margin_slippage_bps,
      config?.derivatives_spread_bps,
      config?.derivatives_slippage_bps
    ) <= 0
  ) {
    return "当前三套产品的 spread / slippage 都是 0，理论收益和可执行收益会更接近，整体偏乐观。";
  }
  return "当前统一交易成本链已区分现货、保证金现货和合约，不同策略会自动读取各自产品类型的成本。";
}

function tradeCostSecondaryRisk(config = {}) {
  if (
    Number(config?.spot_taker_fee_bps) < Number(config?.spot_maker_fee_bps)
    || Number(config?.margin_taker_fee_bps) < Number(config?.margin_maker_fee_bps)
    || Number(config?.derivatives_taker_fee_bps) < Number(config?.derivatives_maker_fee_bps)
  ) {
    return "至少有一组 taker 费率低于 maker，看起来像是配置填反了。";
  }
  return "如果以后接入跨平台搬砖或期权，再继续在这条统一成本链上扩展，而不是回退到策略私有费用字段。";
}

function smartArbitrageCostRow(label, predicted, realized, note) {
  return [
    renderConfigTableCell(label),
    renderConfigTableCell(formatBps(predicted, "待确认")),
    renderConfigTableCell(formatBps(realized, "待回填")),
    renderConfigTableCell(note),
  ];
}

function sumBps(...values) {
  let total = 0;
  let hasValue = false;
  values.forEach((value) => {
    const normalized = Number(value);
    if (!Number.isFinite(normalized)) return;
    total += normalized;
    hasValue = true;
  });
  return hasValue ? total : null;
}

function smartArbitrageCostSourceText(flags) {
  const rows = Array.isArray(flags) ? flags : [];
  if (!rows.length) return "当前没有成本来源说明";
  return rows.map((item) => smartArbitrageCostSourceLabel(item)).join(" | ");
}

function smartArbitrageCostSourceLabel(value) {
  switch (String(value || "").trim()) {
    case "fee_account_schedule":
      return "手续费按账户费率";
    case "fee_trade_cost_defaults":
      return "手续费按统一交易成本默认值";
    case "fee_configured_per_leg":
      return "手续费按逐腿配置";
    case "fee_configured_total_fallback":
      return "手续费按总量兜底";
    case "spread_trade_cost_defaults":
      return "spread 按统一交易成本默认值";
    case "spread_configured_per_leg":
      return "spread 按逐腿配置";
    case "slippage_trade_cost_defaults":
      return "slippage 按统一交易成本默认值";
    case "slippage_configured_per_leg":
      return "slippage 按逐腿配置";
    case "slippage_configured_total_fallback":
      return "slippage 按总量兜底";
    case "settlement_fee_trade_cost_service":
      return "交割结算费按统一交易成本";
    case "funding_account_proxy_per_event":
      return "资金费按账户代理和事件数";
    case "funding_account_proxy_total":
      return "资金费按账户代理总量";
    case "funding_configured_per_event":
      return "资金费按配置和事件数";
    case "funding_configured_total":
      return "资金费按配置总量";
    case "borrow_apr_window_model":
      return "借币费按 APR 和计息窗口";
    case "borrow_configured_total":
      return "借币费按配置总量";
    case "execution_mismatch_configured":
      return "腿间错配按配置";
    case "transfer_cost_configured":
      return "transfer 成本按配置";
    case "time_decay_configured":
      return "时间磨损按每小时配置";
    case "legacy_estimated_cost_fallback":
      return "仍回退综合成本兜底";
    case "cost_model_disabled":
      return "细分成本模型关闭";
    default:
      return String(value || "未知来源");
  }
}

function smartArbitragePrimaryDragDriver(predicted = {}) {
  const candidates = [
    ["手续费", Number(predicted?.ideal_total_fee_bps)],
    ["spread", Number(predicted?.executable_spread_bps)],
    ["slippage", Number(predicted?.executable_slippage_bps)],
    ["腿间错配", Number(predicted?.execution_mismatch_bps)],
    ["资金费", Number(predicted?.funding_cost_bps)],
    ["借币费", Number(predicted?.borrow_cost_bps)],
    ["transfer", Number(predicted?.transfer_cost_bps)],
    ["时间磨损", Number(predicted?.time_decay_cost_bps)],
  ].filter((item) => Number.isFinite(item[1]) && item[1] > 0);
  if (!candidates.length) return "当前没有识别到明显的主磨损来源。";
  candidates.sort((a, b) => b[1] - a[1]);
  return `当前主磨损来源：${candidates[0][0]} ${formatBps(candidates[0][1])}`;
}

function smartArbitrageCostCompact(candidate = {}) {
  const metrics = candidate?.metrics || {};
  const idealEdge = metrics?.ideal_edge_bps;
  const executableEdge = metrics?.executable_edge_bps ?? metrics?.net_basis_bps;
  const executableCost = metrics?.executable_cost_bps ?? metrics?.estimated_cost_bps;
  const segments = [];
  if (idealEdge !== undefined && idealEdge !== null) segments.push(`理论净优势 ${formatBps(idealEdge)}`);
  if (executableEdge !== undefined && executableEdge !== null) segments.push(`可执行净优势 ${formatBps(executableEdge)}`);
  if (executableCost !== undefined && executableCost !== null) segments.push(`总磨损 ${formatBps(executableCost)}`);
  return segments.join(" | ");
}

function smartArbitrageConfigRisks(config = {}, tradeCosts = {}, familyStatus = {}) {
  const pairDefinitions = smartArbitrageConfigPairs(config);
  const pairIssues = smartArbitragePairConfigIssues(pairDefinitions);
  const risks = [];
  const maxConcurrentPairs = Math.max(Number(config?.max_concurrent_pairs) || 1, 1);
  if (config?.enabled !== true) {
    risks.push("智能套利当前未启用，这张卡里的其它参数只会作为配置展示。");
  }
  if (familyStatus?.runtime_supported === false) {
    risks.push("当前运行域不是合约运行域，双向配置仍会展示，但自动双腿执行不会在这里生效。");
  }
  if (Number(config?.basis_exit_bps) >= Number(config?.basis_entry_bps)) {
    risks.push("退出阈值已经接近或超过入场阈值，容易在边界位置来回切换。");
  }
  if (Number(config?.estimated_cost_bps) >= Number(config?.basis_entry_bps)) {
    risks.push("综合成本兜底已经接近或超过入场阈值，自动开仓机会会明显减少。");
  }
  if (
    Number(config?.basis_entry_bps) > 0
    && Number(config?.estimated_cost_bps) > 0
    && Number(config?.basis_entry_bps) - Number(config?.estimated_cost_bps) < 4
  ) {
    risks.push("入场阈值和综合成本兜底之间的缓冲很薄，真实 spread / slippage 稍有放大就会把机会吃掉。");
  }
  if (String(config?.negative_basis_mode || "advisory_only") === "inventory_backed" && config?.inventory_reservation_enabled !== true) {
    risks.push("负基差已切到库存反套，但库存预留未启用，当前配置链条不完整。");
  }
  if (String(config?.negative_basis_mode || "advisory_only") === "margin_backed" && config?.margin_short_enabled !== true) {
    risks.push("负基差已切到保证金反套，但保证金融券能力声明未启用。");
  }
  if (String(config?.negative_basis_mode || "advisory_only") === "margin_backed" && config?.margin_short_execution_ready !== true) {
    risks.push("负基差已切到保证金反套，但执行链路尚未标记为就绪。");
  }
  if (config?.margin_short_execution_ready === true && config?.margin_short_enabled !== true) {
    risks.push("保证金融券执行就绪标记已打开，但能力声明仍关闭，这个标记当前不会生效。");
  }
  if (config?.cost_model_enabled === false) {
    risks.push("细分成本模型当前关闭，净优势只看综合成本兜底，解释力会明显下降。");
  }
  if (
    sumBps(
      tradeCosts?.spot_spread_bps,
      tradeCosts?.spot_slippage_bps,
      tradeCosts?.margin_spread_bps,
      tradeCosts?.margin_slippage_bps,
      tradeCosts?.derivatives_spread_bps,
      tradeCosts?.derivatives_slippage_bps
    ) <= 0
  ) {
    risks.push("统一交易成本里的 spread / slippage 仍全部为 0，智能套利的可执行磨损通常会偏乐观。");
  }
  if (Number(config?.estimated_funding_bps) > 0 && config?.funding_cost_enabled !== true) {
    risks.push("当前填了 funding bps，但资金费成本开关没有打开，这部分还没真正参与净优势计算。");
  }
  if (Number(config?.estimated_borrow_bps) > 0 && config?.borrow_cost_enabled !== true) {
    risks.push("当前填了 borrow bps，但借币成本开关没有打开，这部分还没真正参与净优势计算。");
  }
  if (maxConcurrentPairs > 1) {
    risks.push("当前允许多 pair，但系统只会并行挑选 symbol scope 不重叠的组合；共享腿的 pair 不会一起新开。");
  }
  if (!pairDefinitions.length) {
    risks.push("当前没有显式配对定义，系统会退回主标的推导，生产环境不建议长期这样运行。");
  }
  if (pairDefinitions.some((item) => item?.metadata?.source === "derived_primary_symbol")) {
    risks.push("部分配对来自主标的自动推导，建议改成显式 pair_definitions，避免环境切换时配错腿。");
  }
  if (pairIssues.some((item) => item.errorCodes.includes("smart_arbitrage_pair_execution_modes_invalid"))) {
    risks.push("至少有一组配对的 execution_modes 配置非法；系统会按 fail-closed 处理并阻断新开仓。");
  }
  if (pairIssues.some((item) => item.warningCodes.includes("smart_arbitrage_pair_execution_modes_partial_invalid"))) {
    risks.push("至少有一组配对的 execution_modes 只部分合法；系统只会保留合法模式。");
  }
  if (pairIssues.some((item) => item.warningCodes.includes("smart_arbitrage_pair_id_conflict_renamed"))) {
    risks.push("至少有一组配对复用了相同 pair_id；系统已临时重命名冲突 pair，但应尽快修正配置。");
  }
  if (pairIssues.some((item) => item.warningCodes.includes("smart_arbitrage_duplicate_pair_scope_ignored"))) {
    risks.push("至少有一组配对与其它 pair 使用相同现货/合约 scope；后续重复定义当前会被忽略。");
  }
  if (Array.isArray(config?.pair_registry_error_codes) && config.pair_registry_error_codes.length) {
    risks.push(`pair registry 当前还有 ${formatNumber(config.pair_registry_error_codes.length, 0, "0")} 条配置级错误；来源：${smartArbitragePairRegistrySourceLabel(config)}。`);
  }
  return risks;
}

function smartArbitrageThresholdMeta(config = {}) {
  if (Number(config?.basis_exit_bps) >= Number(config?.basis_entry_bps)) {
    return "当前退出阈值不低于入场阈值，容易在阈值边界频繁收口。";
  }
  if (Number(config?.estimated_cost_bps) >= Number(config?.basis_entry_bps)) {
    return "综合成本兜底已经很接近入场阈值，净优势要足够大才会自动开仓。";
  }
  return "入场阈值决定何时新开套利对，退出阈值决定何时把已持仓套利对收回来。";
}

function smartArbitrageThresholdTone(config = {}) {
  if (Number(config?.basis_exit_bps) >= Number(config?.basis_entry_bps)) return "warning";
  if (Number(config?.estimated_cost_bps) >= Number(config?.basis_entry_bps)) return "warning";
  return "info";
}

function smartArbitrageEffectiveBudget(config = {}) {
  const values = [Number(config?.quote_budget_per_trade), Number(config?.max_pair_notional)]
    .filter((item) => Number.isFinite(item) && item > 0);
  if (!values.length) return null;
  return Math.min(...values);
}

function formatQuoteAmount(value, fallback = "待确认") {
  const formatted = formatNumber(value, 2, fallback);
  return formatted === fallback ? fallback : `${formatted} USDT`;
}

function smartArbitrageNegativeModeLabel(config = {}) {
  const labels = {
    disabled: "不处理负基差",
    advisory_only: "只提示，不自动执行",
    inventory_backed: "库存反套",
    margin_backed: "保证金反套",
  };
  return labels[String(config?.negative_basis_mode || "advisory_only")] || "待确认";
}

function smartArbitrageNegativeModeMeta(config = {}) {
  const mode = String(config?.negative_basis_mode || "advisory_only");
  if (mode === "disabled") return "当前完全不处理负基差机会。";
  if (mode === "advisory_only") return "当前负基差只做提示，不会自动生成反向套利双腿。";
  if (mode === "inventory_backed") {
    return config?.inventory_reservation_enabled === true
      ? "只有现货库存足够时，系统才会自动生成库存反套执行腿。"
      : "当前已选择库存反套，但库存预留开关还没打开。";
  }
  if (mode === "margin_backed") {
    return config?.margin_short_enabled === true && config?.margin_short_execution_ready === true
      ? `保证金融券反套链路已声明就绪，现货腿将使用 ${String(config?.margin_short_spot_margin_mode || "cross")}。`
      : "当前已选择保证金反套，但能力声明或执行就绪条件仍未满足。";
  }
  return "当前没有额外模式说明。";
}

function smartArbitrageNegativeModeTone(config = {}) {
  const mode = String(config?.negative_basis_mode || "advisory_only");
  if (mode === "disabled") return "warning";
  if (mode === "advisory_only") return "info";
  if (mode === "inventory_backed") return config?.inventory_reservation_enabled === true ? "positive" : "warning";
  if (mode === "margin_backed") {
    return config?.margin_short_enabled === true && config?.margin_short_execution_ready === true ? "positive" : "warning";
  }
  return "info";
}

function smartArbitrageBidirectionalReady(config = {}) {
  return String(config?.negative_basis_mode || "advisory_only") === "margin_backed"
    && config?.margin_short_enabled === true
    && config?.margin_short_execution_ready === true;
}

function smartArbitrageEffectiveScopeLabel(config = {}, familyStatus = {}) {
  if (config?.enabled !== true) return "当前整族未启用";
  if (familyStatus?.runtime_supported === false) {
    return smartArbitrageBidirectionalReady(config)
      ? "当前配置支持双向套利，但自动执行仍受限于运行域"
      : "当前配置会保留智能套利参数，但自动执行仍受限于运行域";
  }
  const mode = String(config?.negative_basis_mode || "advisory_only");
  if (mode === "disabled") return "当前只做正基差自动执行";
  if (mode === "advisory_only") return "当前正基差自动执行，负基差只提示";
  if (mode === "inventory_backed") {
    return config?.inventory_reservation_enabled === true
      ? "当前正基差自动执行，负基差按库存反套自动执行"
      : "当前正基差自动执行，负基差目标是库存反套但条件未齐";
  }
  if (mode === "margin_backed") {
    return config?.margin_short_enabled === true && config?.margin_short_execution_ready === true
      ? "当前正负基差都可自动执行"
      : "当前正基差自动执行，负基差目标是保证金反套但条件未齐";
  }
  return "当前按默认模式运行";
}

function smartArbitrageEffectiveScopeMeta(config = {}, familyStatus = {}) {
  if (config?.enabled !== true) return "需要先打开策略总开关，前端候选和执行计划才会开始刷新。";
  const effectiveBudget = smartArbitrageEffectiveBudget(config);
  const budgetText = effectiveBudget == null ? "当前预算待确认" : `当前单次开仓按 ${formatQuoteAmount(effectiveBudget)} 作为初始名义金额上限`;
  const parallelText = Math.max(Number(config?.max_concurrent_pairs) || 1, 1) > 1
    ? "当前允许多 pair，但只会并行挑选不共享 symbol scope 的组合"
    : "当前一次只会主控 1 组套利对";
  const runtimeText = familyStatus?.runtime_supported === false
    ? "当前运行域不是合约运行域，因此这里只保留配置可见性，不会自动下发双腿执行计划"
    : null;
  return [budgetText, parallelText, runtimeText, smartArbitrageNegativeModeMeta(config)].filter(Boolean).join("；");
}

function smartArbitragePairSummary(pairDefinitions = []) {
  if (!pairDefinitions.length) return "当前没有显式配对";
  if (pairDefinitions.length === 1) return "当前有效 1 组配对";
  return `当前有效 ${formatNumber(pairDefinitions.length, 0, "0")} 组配对`;
}

function smartArbitragePairSummaryMeta(pairDefinitions = []) {
  if (!pairDefinitions.length) return "系统会退回主标的推导；生产环境更建议显式写入配对。";
  return pairDefinitions
    .slice(0, 3)
    .map((item) => {
      const modes = Array.isArray(item?.execution_modes) && item.execution_modes.length
        ? ` (${item.execution_modes.map((mode) => readableState(mode)).join(" / ")})`
        : "";
      const metadata = item?.metadata || {};
      const issueTag = Array.isArray(metadata?.configuration_error_codes) && metadata.configuration_error_codes.length
        ? " [配置异常]"
        : Array.isArray(metadata?.configuration_warning_codes) && metadata.configuration_warning_codes.length
          ? " [配置告警]"
          : "";
      return `${item.spot_symbol || "现货腿"} <-> ${item.hedge_symbol || "合约腿"}${modes}${issueTag}`;
    })
    .join(" | ");
}

function smartArbitrageLinkageLabel(config = {}) {
  const mode = String(config?.negative_basis_mode || "advisory_only");
  if (mode === "inventory_backed" && config?.inventory_reservation_enabled !== true) {
    return "库存反套链条不完整";
  }
  if (mode === "margin_backed" && (config?.margin_short_enabled !== true || config?.margin_short_execution_ready !== true)) {
    return "保证金反套链条不完整";
  }
  if (config?.margin_short_execution_ready === true && config?.margin_short_enabled !== true) {
    return "执行就绪标记当前不会生效";
  }
  return "当前联动关系清晰";
}

function smartArbitrageLinkageMeta(config = {}) {
  const mode = String(config?.negative_basis_mode || "advisory_only");
  if (mode === "advisory_only" || mode === "disabled") {
    return "库存反套和保证金融券相关开关当前都不会参与自动执行判断。";
  }
  if (mode === "inventory_backed") {
    return config?.inventory_reservation_enabled === true
      ? "负基差会先检查可用现货库存，再决定是否生成库存反套双腿。"
      : "要让负基差进入库存反套，negative_basis_mode 和 inventory_reservation_enabled 必须同时满足。";
  }
  if (mode === "margin_backed") {
    return config?.margin_short_enabled === true && config?.margin_short_execution_ready === true
      ? `负基差当前会按 ${String(config?.margin_short_spot_margin_mode || "cross")} 保证金模式生成现货反套腿。`
      : "要让负基差进入保证金反套，negative_basis_mode、margin_short_enabled、margin_short_execution_ready 必须同时满足。";
  }
  return "当前没有额外联动说明。";
}

function renderExpandableSection(title, body, options = {}) {
  const { meta = "", open = false } = options;
  return `
    <details class="strategy-details"${open ? " open" : ""}>
      <summary>
        <span>${escapeHtml(title)}</span>
        ${meta ? `<span class="strategy-details__meta">${escapeHtml(meta)}</span>` : ""}
      </summary>
      <div class="strategy-details__body">
        ${body}
      </div>
    </details>
  `;
}

function renderPaginationFooter(payload, { singular, loadAction, collapseAction }) {
  const shown = Number(payload?.decisions?.length || 0);
  const total = Number(payload?.total_available || shown);
  const hasMore = Boolean(payload?.has_more);
  const limit = Number(payload?.limit || shown);
  if (!shown) return "";
  return `
    <div class="history-footer">
      <p class="meta-copy">当前显示 ${shown} / ${total} 条${singular}。</p>
      <div class="stack-actions">
        ${hasMore ? actionButton(`加载更多${singular}`, loadAction, "", "secondary") : ""}
        ${limit > 8 ? actionButton("收起到最新 8 条", collapseAction, "", "ghost") : ""}
      </div>
    </div>
  `;
}

function strategyNarrative(detail) {
  if (!detail.decision_id) {
    return "当前暂无新的策略输出，通常是在等待新的市场条件或下一轮决策窗口。";
  }
  const target = detail.position_target || {};
  const outcome = detail.decision_outcome || {};
  const policy = detail.policy_decision || {};
  const risk = detail.risk_decision || {};
  const intentLabel = readableIntent(detail);
  const regimeLabel = readableRegime(detail);
  const strategyFamily = readableState(outcome.selected_strategy_family || target.strategy_family || "directional");
  const currentQty = Number(target.current_position_qty ?? detail.decision_context?.current_position_qty ?? 0);
  const targetQty = Number(target.target_position_qty ?? 0);
  const openOrders = Array.isArray(detail.decision_context?.current_open_orders) ? detail.decision_context.current_open_orders : [];
  const actionSentence =
    intentLabel === "继续观望"
      ? "当前没有持仓和挂单，这轮决策的含义就是继续观察。"
      : currentQty !== 0 && targetQty === currentQty
        ? "这轮决策没有要求增减仓，表示继续维持当前仓位。"
        : openOrders.length > 0
          ? "当前已经有在途委托，这轮主要是在维持既有执行状态。"
          : `这轮决策给出了 ${intentLabel} 的交易结论。`;
  return `当前市场状态为 ${regimeLabel}，本轮由 ${strategyFamily} 接管。${actionSentence}`
    + `${policy.execution_allowed ? "策略层允许执行，" : "策略层仍未允许执行，"}`
    + `${risk.approved ? "风控层当前没有继续阻断。" : `风控仍在拦截：${listText(risk.rejection_reasons, "当前没有额外风控说明")}。`}`;
}

function recentDecisionNarrative(item, scene) {
  const delta = item.delta_position_qty ?? item.target_delta_qty;
  if (delta === null || delta === undefined) {
    return scene === "derivatives" ? "没有新的净仓位调整" : "没有新的持仓调整";
  }
  return `${scene === "derivatives" ? "净仓位变化" : "持仓变化"} ${formatSigned(delta)} | ${item.decision_id || "当前没有编号"}`;
}

function readableRegime(detail) {
  const baseline = detail.baseline_assessment || {};
  return readableState(baseline.regime || baseline.market_regime || detail.decision_context?.market_regime || "unknown");
}

function readableIntent(detail) {
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

function readableRecentIntent(item) {
  const rawIntent = String(item.position_intent || item.target_exposure_side || "hold").toLowerCase();
  const currentQty = Number(item.current_position_qty ?? 0);
  const targetQty = Number(item.target_position_qty ?? 0);
  if (rawIntent === "hold" && currentQty === 0 && targetQty === 0) {
    return "继续观望";
  }
  return readableFamilyExecutionSummary(item, readableState(rawIntent));
}

function inferDecisionScene(latestDecision, recentDecisions) {
  const latestTarget = latestDecision?.position_target || {};
  if (latestTarget.product_type) return inferTradeScene(latestTarget);
  const context = latestDecision?.decision_context || {};
  if (context.product_type) return inferTradeScene(context);
  const firstRecent = recentDecisions.find((item) => item && (item.product_type || item.margin_mode));
  return inferTradeScene(firstRecent || {});
}

function formatRatio(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "待确认";
  return `${formatNumber(number * 100, 1)}%`;
}

function formatBps(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "待确认";
  return `${formatNumber(number, 1)} 个基点`;
}

function formatRawFeeImpact(value, digits = 4, fallback = "待确认") {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return formatSigned(number === 0 ? 0 : -number, digits, fallback);
}

function forwardVerdictLabel(value) {
  const labels = {
    continue: "继续试盘",
    observe: "继续观察",
    shrink: "建议缩容",
    pause: "建议暂停",
    insufficient_data: "样本仍少，先继续观察",
  };
  return labels[String(value || "")] || readableState(value || "unknown");
}

function forwardVerdictTone(value) {
  if (value === "continue") return "positive";
  if (value === "observe" || value === "insufficient_data") return "warning";
  if (value === "shrink") return "warning";
  if (value === "pause") return "danger";
  return "neutral";
}

function renderForwardValidationPeriods(periods) {
  if (!periods.length) {
    return `<p class="meta-copy">当前还没有可展示的前向验证周期。</p>`;
  }
  return responsiveTable(
    ["周期", "状态", "净收益", "费用拖累", "异常比例"],
    periods.map((item) => [
      `<div><strong>${formatMaybeTimestamp(item.period_start)}</strong><div class="table-meta">至 ${formatMaybeTimestamp(item.period_end)}</div></div>`,
      forwardPeriodStatusLabel(item.status),
      formatSigned(item.net_realized_pnl),
      formatRatio(item.fee_to_notional_ratio),
      `${formatRatio(item.high_slippage_ratio)} / ${formatRatio(item.slow_submit_to_fill_ratio)}`,
    ]),
    "当前没有前向验证周期。"
  );
}

function renderStrategyCandidateTable(candidates, smartArbitrageConfig = {}, context = {}) {
  if (!Array.isArray(candidates) || !candidates.length) {
    return `<p class="meta-copy">当前还没有候选策略快照。</p>`;
  }
  return responsiveTable(
    ["策略家族", "当前状态", "如何处理", "本轮目标", "原因说明"],
    candidates.map((candidate) => [
      `<div><strong>${escapeHtml(readableState(candidate.family || "unknown"))}</strong><div class="table-meta">${escapeHtml(strategyCandidateSymbolMeta(candidate))}</div></div>`,
      `<div><strong>${escapeHtml(strategyCandidateStateLabel(candidate))}</strong><div class="table-meta">${escapeHtml(strategyCandidateStateMeta(candidate, smartArbitrageConfig))}</div></div>`,
      `<div><strong>${escapeHtml(strategyCandidateRouteLabel(candidate))}</strong><div class="table-meta">${escapeHtml(strategyCandidateRouteMeta(candidate))}</div></div>`,
      `<div><strong>${escapeHtml(strategyCandidateTargetLabel(candidate))}</strong><div class="table-meta">${escapeHtml(strategyCandidateTargetMeta(candidate, context))}</div></div>`,
      `<div><strong>${escapeHtml(strategyCandidateReason(candidate, smartArbitrageConfig, context))}</strong><div class="table-meta">${escapeHtml(strategyLegSummary(candidate, smartArbitrageConfig))}</div></div>`,
    ]),
    "当前没有候选策略快照。"
  );
}

function renderRecentSleeveIntentTable(items, context = {}) {
  if (!Array.isArray(items) || !items.length) {
    return `<p class="meta-copy">当前还没有新的子策略意图记录。</p>`;
  }
  return responsiveTable(
    ["最近子策略意图", "当前状态", "本轮目标", "自动预算", "原因说明"],
    items.map((item) => [
      `<div><strong>${escapeHtml(item.strategy_sleeve_id || "未归属")}</strong><div class="table-meta">${escapeHtml(readableState(item.family || "unknown"))} | ${escapeHtml(item.symbol || "标的待确认")}</div></div>`,
      `<div><strong>${escapeHtml(readableState(item.state || "unknown"))}</strong><div class="table-meta">${escapeHtml(strategyRouteActionLabel(item.route_action, item.family_action))}</div></div>`,
      `<div><strong>${escapeHtml(strategySleeveIntentTargetLabel(item))}</strong><div class="table-meta">${escapeHtml(strategySleeveIntentTargetMeta(item, context))}</div></div>`,
      `<div><strong>${item.automatic_enabled ? "自动管理" : "人工冻结"}</strong><div class="table-meta">倍率 ${formatNumber(item.budget_multiplier, 2, "0")} | 权重 ${formatNumber(item.allocator_weight, 2, "0")}</div></div>`,
      `<div><strong>${escapeHtml(strategySleeveIntentReason(item, context))}</strong><div class="table-meta">${escapeHtml(reasonListText(item.control_reason_codes?.length ? item.control_reason_codes : item.reason_codes, "当前没有额外原因"))}</div></div>`,
    ]),
    "当前还没有新的 sleeve 意图记录。"
  );
}

function strategyCandidateSymbolMeta(candidate) {
  if (candidate?.family === "smart_arbitrage") {
    if (candidate?.pair_id === "multi_pair" || candidate?.metrics?.aggregate_candidate === true) {
      const pairs = Array.isArray(candidate?.metrics?.selected_pair_summaries) ? candidate.metrics.selected_pair_summaries : [];
      if (pairs.length) {
        const preview = pairs.slice(0, 2).map((item) => `${item.spot_symbol || "现货腿"} <-> ${item.derivatives_symbol || "合约腿"}`).join(" | ");
        return `${formatNumber(pairs.length, 0, "0")} 组套利对 | ${preview}`;
      }
      return "多组套利对聚合候选";
    }
    const spotSymbol = candidate?.metrics?.spot_symbol;
    const derivativesSymbol = candidate?.metrics?.derivatives_symbol;
    if (spotSymbol || derivativesSymbol) {
      return `${spotSymbol || "现货腿"} <-> ${derivativesSymbol || "合约腿"}`;
    }
  }
  return candidate?.recommended_symbol || "当前没有推荐标的";
}

function smartArbitrageBelowEntryThreshold(candidate) {
  if (candidate?.family !== "smart_arbitrage") return false;
  const reasonCodes = Array.isArray(candidate?.reason_codes) ? candidate.reason_codes : [];
  return reasonCodes.includes("smart_arbitrage_basis_below_entry_threshold");
}

function smartArbitragePairLabel(candidate) {
  const spotSymbol = candidate?.metrics?.spot_symbol;
  const derivativesSymbol = candidate?.metrics?.derivatives_symbol;
  if (spotSymbol || derivativesSymbol) {
    return `${spotSymbol || "现货腿"} <-> ${derivativesSymbol || "合约腿"}`;
  }
  return candidate?.recommended_symbol || "套利对待确认";
}

function smartArbitrageInactiveStateMeta(candidate, smartArbitrageConfig = {}) {
  if (smartArbitrageBelowEntryThreshold(candidate)) {
    const basisBps = formatBps(candidate?.metrics?.basis_bps);
    const entryThreshold = formatBps(smartArbitrageEntryThreshold(candidate, smartArbitrageConfig));
    return `${smartArbitragePairLabel(candidate)} | 基差 ${basisBps} | 入场阈值 ${entryThreshold}`;
  }
  return smartArbitrageMarketAvailability(candidate, smartArbitrageConfig);
}

function smartArbitrageEntryThreshold(candidate, smartArbitrageConfig = {}) {
  const candidateThreshold = candidate?.metrics?.entry_threshold_bps;
  if (candidateThreshold !== undefined && candidateThreshold !== null) {
    return candidateThreshold;
  }
  return smartArbitrageConfig?.basis_entry_bps;
}

function strategyCandidateStateLabel(candidate) {
  if (smartArbitrageBelowEntryThreshold(candidate)) {
    return "当前继续观察";
  }
  if (candidate?.family === "smart_arbitrage" && candidate?.state === "inactive") {
    return "当前不参与执行";
  }
  if (candidate?.family === "smart_arbitrage" && candidate?.state === "advisory_only") {
    return "当前只给建议";
  }
  if (candidate?.family === "smart_arbitrage" && candidate?.state === "blocked") {
    return "当前机会被阻断";
  }
  if (candidate?.family === "smart_arbitrage" && candidate?.state === "opening") {
    return "当前准备开仓";
  }
  if (candidate?.family === "smart_arbitrage" && candidate?.state === "recovery") {
    return "当前正在补齐双腿";
  }
  if (candidate?.family === "smart_arbitrage" && candidate?.state === "unwinding") {
    return "当前准备退出套利对";
  }
  return readableState(candidate?.state || "unknown");
}

function strategyCandidateStateMeta(candidate, smartArbitrageConfig = {}) {
  if (candidate?.family === "smart_arbitrage" && candidate?.state === "inactive") {
    return smartArbitrageInactiveStateMeta(candidate, smartArbitrageConfig);
  }
  if (candidate?.family === "smart_arbitrage") {
    return smartArbitrageStateMeta(candidate);
  }
  const confidence = Number(candidate?.confidence);
  if (Number.isFinite(confidence) && confidence > 0) {
    return `置信度 ${formatNumber(confidence, 2, "0")}`;
  }
  return "当前没有额外状态量化信息";
}

function strategyCandidateRouteLabel(candidate) {
  if (smartArbitrageBelowEntryThreshold(candidate)) return "本轮不入场";
  if (candidate?.route_action === "advisory_only") return "仅参考，不直接执行";
  if (candidate?.route_action === "hold_current") return "保持当前仓位";
  if (candidate?.route_action === "override_target") {
    return strategyRouteActionLabel(candidate?.route_action, candidate?.family_action);
  }
  return readableState(candidate?.route_action || "hold_current");
}

function strategyCandidateRouteMeta(candidate) {
  return `优先级 ${escapeFallbackReadableState(candidate?.urgency, "low")}`;
}

function strategyRouteActionLabel(routeAction, familyAction) {
  const normalizedRoute = String(routeAction || "").trim().toLowerCase();
  const normalizedFamilyAction = String(familyAction || "").trim().toLowerCase();
  if (
    normalizedRoute === "override_target" &&
    [
      "close_protection_leg",
      "close_opportunity_leg",
      "de_risk_independent_book",
      "close_failed_thesis_independent_book",
      "close_stale_thesis_independent_book",
    ].includes(normalizedFamilyAction)
  ) {
    return readableState(normalizedFamilyAction);
  }
  return readableState(normalizedRoute || "hold_current");
}

function strategyCandidateTargetLabel(candidate) {
  if (smartArbitrageBelowEntryThreshold(candidate)) {
    return "暂不生成套利双腿";
  }
  if (smartArbitrageNegativeBasisAdvisory(candidate)) {
    return "当前负基差不自动下单";
  }
  if (candidate?.family === "smart_arbitrage" && (candidate?.pair_id === "multi_pair" || candidate?.metrics?.aggregate_candidate === true)) {
    return "按多组套利对分别执行";
  }
  if (candidate?.family === "smart_arbitrage" && candidate?.state === "blocked") {
    return "当前机会被阻断";
  }
  const target = Number(candidate?.target_position_qty ?? 0);
  const delta = Number(candidate?.delta_position_qty ?? 0);
  if (!Number.isFinite(target) || !Number.isFinite(delta)) {
    return "当前没有可用目标";
  }
  if (Math.abs(target) < 1e-12 && Math.abs(delta) < 1e-12) {
    return "当前不生成执行量";
  }
  return formatSigned(candidate?.target_position_qty);
}

function strategyCandidateTargetMeta(candidate, context = {}) {
  if (smartArbitrageBelowEntryThreshold(candidate)) {
    const breakeven = candidate?.metrics?.breakeven_basis_bps;
    return breakeven !== undefined && breakeven !== null
      ? `当前盈亏平衡基差约 ${formatBps(breakeven)}；达到阈值后再计算双腿执行量。`
      : "等基差达到入场阈值后，再计算双腿执行量。";
  }
  if (smartArbitrageNegativeBasisAdvisory(candidate)) {
    return "自动执行当前只支持正基差双腿，现货现金模式不会为负基差生成执行量";
  }
  if (candidate?.family === "smart_arbitrage" && (candidate?.pair_id === "multi_pair" || candidate?.metrics?.aggregate_candidate === true)) {
    const pairCount = Number(candidate?.metrics?.pair_count_selected || candidate?.metrics?.selected_pair_summaries?.length || 0);
    return `当前按 ${formatNumber(pairCount, 0, "0")} 组套利对分别生成双腿；不再用单一目标数量表示。`;
  }
  if (candidate?.family === "smart_arbitrage" && candidate?.state === "blocked") {
    return smartArbitrageBlockingSummary(candidate);
  }
  if (smartArbitrageExitBlockedByKillSwitch(candidate, context)) {
    return "当前已经进入退出阶段，但平仓提交被 kill switch 阻断，交易所里并没有新的退出挂单。";
  }
  if (smartArbitrageWaitingExit(candidate, context)) {
    return "当前双腿已经建好，系统正在等待基差回到退出阈值；这不是挂单未成。";
  }
  const target = Number(candidate?.target_position_qty ?? 0);
  const delta = Number(candidate?.delta_position_qty ?? 0);
  if (!Number.isFinite(target) || !Number.isFinite(delta)) {
    return "当前没有数量信息";
  }
  if (Math.abs(target) < 1e-12 && Math.abs(delta) < 1e-12) {
    return "当前没有需要执行的增减仓";
  }
  return `本轮变化 ${formatSigned(candidate?.delta_position_qty)}`;
}

function renderAllocatorBudgetSnapshotTable(items) {
  if (!Array.isArray(items) || !items.length) {
    return `<p class="meta-copy">当前还没有组合预算快照。</p>`;
  }
  return `
    <div class="section-block">
      <h4>预算快照</h4>
      ${responsiveTable(
        ["子策略", "优先级", "请求名义 / 批准名义", "组合预算变化", "原因"],
        items.map((item) => [
          `<div><strong>${escapeHtml(item.strategy_sleeve_id || "未归属")}</strong><div class="table-meta">${escapeHtml(readableState(item.family || "unknown"))}</div></div>`,
          `<div><strong>${escapeHtml(readableState(item.hedge_priority_class || "standard"))}</strong><div class="table-meta">rank ${escapeHtml(String(item.priority_rank ?? 0))}</div></div>`,
          `<div><strong>${formatSigned(item.requested_notional)} -> ${formatSigned(item.approved_notional)}</strong><div class="table-meta">${formatSigned(item.requested_delta_qty)} -> ${formatSigned(item.approved_delta_qty)}</div></div>`,
          `<div><strong>${formatSigned(item.portfolio_requested_notional)} -> ${formatSigned(item.portfolio_approved_notional)}</strong><div class="table-meta">削减 ${formatSigned(item.portfolio_budget_cut_notional)}</div></div>`,
          `<div><strong>${escapeHtml(item.clamped ? "已裁剪" : "未裁剪")}</strong><div class="table-meta">${escapeHtml(reasonListText(item.reason_codes, "当前没有额外原因"))}</div></div>`,
        ]),
        "当前没有组合预算快照。"
      )}
    </div>
  `;
}

function renderAllocatorConflictResolutionTable(items) {
  if (!Array.isArray(items) || !items.length) {
    return `<p class="meta-copy">当前没有新的冲突解算记录。</p>`;
  }
  return `
    <div class="section-block">
      <h4>冲突解算</h4>
      ${responsiveTable(
        ["类型", "参与子策略", "请求 / 批准 / 阻断", "保护 / 削减", "原因"],
        items.map((item) => [
          `<div><strong>${escapeHtml(readableState(item.conflict_type || "unknown"))}</strong><div class="table-meta">${escapeHtml(readableState(item.resolution_action || "unknown"))}</div></div>`,
          `<div><strong>${escapeHtml((item.input_sleeve_ids || []).join(" | ") || "当前没有输入子策略")}</strong><div class="table-meta">批准 ${escapeHtml((item.approved_sleeve_ids || []).join(" | ") || "无")}</div></div>`,
          `<div><strong>${formatSigned(item.gross_requested_qty)} / ${formatSigned(item.net_approved_qty)}</strong><div class="table-meta">阻断 ${formatSigned(item.blocked_qty)}</div></div>`,
          `<div><strong>${formatSigned(item.protected_notional)}</strong><div class="table-meta">方向削减 ${formatSigned(item.reduced_notional)}</div></div>`,
          `<div><strong>${escapeHtml(reasonListText(item.reason_codes, "当前没有额外原因"))}</strong></div>`,
        ]),
        "当前没有新的冲突解算记录。"
      )}
    </div>
  `;
}

function renderAllocatorNettingDecisionTable(items) {
  if (!Array.isArray(items) || !items.length) {
    return `<p class="meta-copy">当前没有新的净额决策记录。</p>`;
  }
  return `
    <div class="section-block">
      <h4>净额决策</h4>
      ${responsiveTable(
        ["标的", "参与子策略", "总买 / 总卖", "净批准数量", "原因"],
        items.map((item) => [
          `<div><strong>${escapeHtml(item.symbol || "标的待确认")}</strong><div class="table-meta">${escapeHtml(readableState(item.product_type || "unknown"))} | ${escapeHtml(readableState(item.margin_mode || "unknown"))}</div></div>`,
          `<div><strong>${escapeHtml((item.participating_sleeve_ids || []).join(" | ") || "当前没有参与子策略")}</strong></div>`,
          `<div><strong>${formatSigned(item.gross_buy_qty)} / ${formatSigned(item.gross_sell_qty)}</strong></div>`,
          `<div><strong>${formatSigned(item.net_approved_qty)}</strong></div>`,
          `<div><strong>${escapeHtml(reasonListText(item.reason_codes, "当前没有额外原因"))}</strong></div>`,
        ]),
        "当前没有新的净额决策记录。"
      )}
    </div>
  `;
}

function familyEnablementSummary(payload) {
  if (!payload || typeof payload !== "object") return "当前没有多策略运行能力摘要";
  return Object.entries(payload)
    .map(([family, detail]) => {
      const status = detail?.enabled ? "已启用" : "未启用";
      return `${readableState(family)} ${status}`;
    })
    .join(" | ");
}

function strategyCandidateReason(candidate, smartArbitrageConfig = {}, context = {}) {
  const smartArbitrageReason = smartArbitrageReasonText(candidate, smartArbitrageConfig, context);
  if (smartArbitrageReason) return smartArbitrageReason;
  if (candidate?.family === "smart_arbitrage") {
    const summary = smartArbitrageLocalizedReasonSummary(candidate, context);
    if (summary) return summary;
  }
  if (candidate?.headline) return candidate.headline;
  const summary = reasonListText(candidate?.reason_codes, "");
  if (summary) return summary;
  return "当前没有额外说明";
}

function strategySleeveIntentReason(item, context = {}) {
  if (item?.family === "smart_arbitrage") {
    const reason = smartArbitrageReasonText(item, {}, context);
    if (reason) return reason;
    const summary = smartArbitrageLocalizedReasonSummary(item, context);
    if (summary) return summary;
  }
  return item?.control_summary || item?.headline || "当前没有额外说明";
}

function strategySleeveIntentTargetLabel(item) {
  if (smartArbitrageBelowEntryThreshold(item)) {
    return "继续观察";
  }
  if (item?.family === "smart_arbitrage" && item?.pair_id === "multi_pair") {
    return "按多组套利对分别执行";
  }
  return formatSigned(item?.target_position_qty);
}

function strategySleeveIntentTargetMeta(item, context = {}) {
  if (smartArbitrageBelowEntryThreshold(item)) {
    return "当前还没有生成套利双腿。";
  }
  if (item?.family === "smart_arbitrage" && item?.pair_id === "multi_pair") {
    const legCount = Array.isArray(item?.legs) ? item.legs.length : 0;
    return `当前以 ${formatNumber(legCount, 0, "0")} 条执行腿表达，不再展示单一聚合数量。`;
  }
  if (smartArbitrageExitBlockedByKillSwitch(item, context)) {
    return "当前已经进入退出阶段，但平仓提交被 kill switch 阻断，交易所里并没有新的退出挂单。";
  }
  if (smartArbitrageWaitingExit(item, context)) {
    return "当前双腿已经建好，系统正在等待基差回到退出阈值；这不是挂单未成。";
  }
  return `变化 ${formatSigned(item?.delta_position_qty)}`;
}

function strategyLegSummary(candidate, smartArbitrageConfig = {}) {
  const legs = candidate?.legs;
  const expectancySummary = readableBookExpectancySummary(candidate, "");
  if (!Array.isArray(legs) || !legs.length) {
    if (candidate?.family === "smart_arbitrage") {
      if (smartArbitrageBelowEntryThreshold(candidate)) {
        const compact = smartArbitrageCostCompact(candidate);
        return compact ? `当前还没有生成套利双腿。 | ${compact}` : "当前还没有生成套利双腿。";
      }
      const marketAvailability = smartArbitrageMarketAvailability(candidate, smartArbitrageConfig);
      const compact = smartArbitrageCostCompact(candidate);
      return compact ? `${marketAvailability} | ${compact}` : marketAvailability;
    }
    return expectancySummary ? `当前没有附带腿说明。 | ${expectancySummary}` : "当前没有附带腿说明";
  }
  const legSummary = legs
    .map((item) => {
      const mode = item?.execution_mode ? ` (${readableState(item.execution_mode)})` : "";
      return `${readableState(item.product_type)} ${readableState(item.side)} ${item.symbol || "标的待确认"}${mode}`;
    })
    .join(" | ");
  if (candidate?.family === "independent" || candidate?.family === "opportunistic") {
    return expectancySummary ? `${legSummary} | ${expectancySummary}` : legSummary;
  }
  if (candidate?.family !== "smart_arbitrage") return legSummary;
  const compact = smartArbitrageCostCompact(candidate);
  return compact ? `${legSummary} | ${compact}` : legSummary;
}

function normalizedBookExpectancySummary(source = {}) {
  if (!source || typeof source !== "object") return {};
  if (Array.isArray(source.books)) return source;
  const direct = source.book_expectancy_summary || source.bookExpectancySummary;
  if (direct && typeof direct === "object") return direct;
  const familySummary = source.family_execution_summary || source.familyExecutionSummary;
  if (!familySummary || typeof familySummary !== "object") return {};
  const nested = familySummary.book_expectancy_summary || familySummary.bookExpectancySummary;
  return nested && typeof nested === "object" ? nested : {};
}

function resolvedTargetExpectancyMetrics(source = {}) {
  const summary = normalizedBookExpectancySummary(source);
  const books = Array.isArray(summary?.books) ? summary.books : [];
  if (books.length === 1 && books[0] && typeof books[0] === "object") {
    return {
      expected_signal_edge_bps: books[0].expected_signal_edge_bps,
      expected_cost_bps: books[0].expected_cost_bps,
      expected_net_edge_bps: books[0].expected_net_edge_bps,
      required_safe_net_edge_bps: books[0].required_safe_net_edge_bps,
      max_acceptable_cost_bps: books[0].max_acceptable_cost_bps,
      weak_edge_execution_mode: books[0].weak_edge_execution_mode,
      weak_edge_report_only: books[0].weak_edge_report_only,
      passive_first_required: books[0].passive_first_required,
    };
  }
  return {
    expected_signal_edge_bps: source?.expected_signal_edge_bps,
    expected_cost_bps: source?.expected_cost_bps,
    expected_net_edge_bps: source?.expected_net_edge_bps,
    required_safe_net_edge_bps: null,
    max_acceptable_cost_bps: null,
    weak_edge_execution_mode: null,
    weak_edge_report_only: null,
    passive_first_required: null,
  };
}

function targetExpectancySummary(targetExpectancy = {}) {
  const parts = [
    `预期净优势 ${formatBps(targetExpectancy.expected_net_edge_bps)}`,
    `信号 ${formatBps(targetExpectancy.expected_signal_edge_bps)}`,
    `成本 ${formatBps(targetExpectancy.expected_cost_bps)}`,
  ];
  if (targetExpectancy.required_safe_net_edge_bps !== undefined && targetExpectancy.required_safe_net_edge_bps !== null) {
    parts.push(`安全净边际 ${formatBps(targetExpectancy.required_safe_net_edge_bps)}`);
  }
  if (
    targetExpectancy.max_acceptable_cost_bps !== undefined
    && targetExpectancy.max_acceptable_cost_bps !== null
    && Number(targetExpectancy.max_acceptable_cost_bps) > 0
  ) {
    parts.push(`成本上限 ${formatBps(targetExpectancy.max_acceptable_cost_bps)}`);
  }
  if (targetExpectancy.weak_edge_execution_mode) {
    parts.push(`弱边际 ${readableState(targetExpectancy.weak_edge_execution_mode, targetExpectancy.weak_edge_execution_mode)}`);
  }
  if (targetExpectancy.weak_edge_report_only === true) {
    parts.push("本轮只保留报告");
  }
  if (targetExpectancy.passive_first_required === true) {
    parts.push("要求被动优先");
  }
  return parts.join(" | ");
}

function familyExpectancySuffix(source = {}) {
  const summary = readableBookExpectancySummary(source, "");
  return summary ? ` | ${summary}` : "";
}

function smartArbitrageMarketAvailability(candidate, smartArbitrageConfig = {}) {
  const metrics = candidate?.metrics || {};
  const spotSymbol = metrics.spot_symbol || candidate?.recommended_symbol || "现货腿";
  const derivativesSymbol = metrics.derivatives_symbol || "合约腿";
  const reasonCodes = smartArbitrageContextCodes(candidate);
  if (smartArbitrageBelowEntryThreshold(candidate)) {
    const basisBps = formatBps(metrics.basis_bps);
    const entryThreshold = formatBps(smartArbitrageEntryThreshold(candidate, smartArbitrageConfig));
    const breakeven = metrics?.breakeven_basis_bps;
    return breakeven !== undefined && breakeven !== null
      ? `当前基差 ${basisBps}，还没有达到入场阈值 ${entryThreshold}；按当前磨损估算，盈亏平衡约需要 ${formatBps(breakeven)}。`
      : `当前基差 ${basisBps}，还没有达到入场阈值 ${entryThreshold}，系统继续观察。`;
  }
  if (reasonCodes.includes("smart_arbitrage_market_pair_incomplete")) {
    return `当前缺少 ${spotSymbol} 或 ${derivativesSymbol} 的配对行情，暂时不能进入双腿执行。`;
  }
  if (reasonCodes.includes("smart_arbitrage_symbol_pair_missing")) {
    return "当前没有识别到可用的现货/合约配对标的。";
  }
  if (smartArbitrageNegativeBasisAdvisory(candidate) || reasonCodes.includes("smart_arbitrage_negative_basis_advisory_only")) {
    return "当前是负基差提示单，系统不会下发套利双腿执行计划。";
  }
  if (reasonCodes.includes("smart_arbitrage_inventory_backed_spot_balance_unavailable")) {
    return "当前识别到负基差，但账户里没有可用于反套的现货余额。";
  }
  if (reasonCodes.includes("smart_arbitrage_inventory_backed_insufficient")) {
    return "当前识别到负基差，但可用于反套的现货库存不足。";
  }
  if (reasonCodes.includes("smart_arbitrage_margin_short_disabled")) {
    return "当前识别到负基差，但保证金融券反套模式当前未启用。";
  }
  if (reasonCodes.includes("smart_arbitrage_margin_short_execution_not_ready")) {
    return "当前识别到负基差，但保证金融券执行链路尚未接通。";
  }
  if (reasonCodes.includes("smart_arbitrage_spot_carry_not_allowed")) {
    return `当前配对 ${spotSymbol} <-> ${derivativesSymbol} 没有开放正向现货套利模式。`;
  }
  if (reasonCodes.includes("smart_arbitrage_inventory_reverse_carry_not_allowed")) {
    return `当前配对 ${spotSymbol} <-> ${derivativesSymbol} 没有开放库存反向套利模式。`;
  }
  if (reasonCodes.includes("smart_arbitrage_margin_reverse_carry_not_allowed")) {
    return `当前配对 ${spotSymbol} <-> ${derivativesSymbol} 没有开放保证金反向套利模式。`;
  }
  return "当前没有附带套利双腿执行信息。";
}

function smartArbitrageNegativeBasisAdvisory(candidate) {
  if (candidate?.family !== "smart_arbitrage") return false;
  const reasonCodes = smartArbitrageContextCodes(candidate);
  return (
    reasonCodes.includes("smart_arbitrage_negative_basis") &&
    reasonCodes.includes("smart_arbitrage_spot_short_not_supported")
  );
}

function smartArbitrageContextCodes(candidate = {}, context = {}) {
  const candidateCodes = [
    ...(Array.isArray(candidate?.reason_codes) ? candidate.reason_codes : []),
    ...(Array.isArray(candidate?.blocking_reasons) ? candidate.blocking_reasons : []),
    ...(Array.isArray(candidate?.control_reason_codes) ? candidate.control_reason_codes : []),
  ];
  const policyCodes = Array.isArray(context?.policy?.blocker_reasons) ? context.policy.blocker_reasons : [];
  const riskCodes = Array.isArray(context?.risk?.rejection_reasons) ? context.risk.rejection_reasons : [];
  return Array.from(new Set([...candidateCodes, ...policyCodes, ...riskCodes]));
}

function smartArbitrageLocalizedReasonSummary(candidate = {}, context = {}) {
  return reasonListText(smartArbitrageContextCodes(candidate, context), "");
}

function smartArbitrageExitBlockedByKillSwitch(candidate = {}, context = {}) {
  if (candidate?.family !== "smart_arbitrage") return false;
  const reasonCodes = smartArbitrageContextCodes(candidate, context);
  const state = String(candidate?.state || "");
  const statePhase = String(candidate?.state_phase || candidate?.metrics?.state_phase || "");
  const opportunityKind = String(candidate?.opportunity_kind || "").trim();
  const wantsExit = (
    reasonCodes.includes("smart_arbitrage_exit_ready")
    || state === "unwinding"
    || statePhase === "unwinding"
    || opportunityKind === "pair_exit"
  );
  return wantsExit && reasonCodes.includes("kill_switch_active");
}

function smartArbitrageWaitingExit(candidate = {}, context = {}) {
  if (candidate?.family !== "smart_arbitrage") return false;
  const reasonCodes = smartArbitrageContextCodes(candidate, context);
  return (
    reasonCodes.includes("smart_arbitrage_pair_active_waiting_exit")
    && !smartArbitrageExitBlockedByKillSwitch(candidate, context)
  );
}

function smartArbitrageReasonText(candidate, smartArbitrageConfig = {}, context = {}) {
  if (smartArbitrageBelowEntryThreshold(candidate)) {
    return smartArbitrageMarketAvailability(candidate, smartArbitrageConfig);
  }
  const reasonCodes = smartArbitrageContextCodes(candidate, context);
  if (reasonCodes.includes("smart_arbitrage_market_pair_incomplete")) {
    return smartArbitrageMarketAvailability(candidate, smartArbitrageConfig);
  }
  if (reasonCodes.includes("smart_arbitrage_symbol_pair_missing")) {
    return smartArbitrageMarketAvailability(candidate, smartArbitrageConfig);
  }
  if (smartArbitrageNegativeBasisAdvisory(candidate) || reasonCodes.includes("smart_arbitrage_negative_basis_advisory_only")) {
    return "当前是负基差，但自动执行只支持正基差双腿；现货现金模式不能自动做空。";
  }
  if (reasonCodes.includes("smart_arbitrage_inventory_backed_ready")) {
    return "当前是负基差，且账户现货库存足够，系统会按库存反套模式生成双腿计划。";
  }
  if (reasonCodes.includes("smart_arbitrage_margin_short_ready")) {
    return "当前是负基差，且保证金融券反套链路已就绪，系统会按借币卖出现货并买入合约的模式生成双腿计划。";
  }
  if (reasonCodes.includes("smart_arbitrage_inventory_backed_spot_balance_unavailable")) {
    return "当前识别到负基差，但账户里没有可用于反套的现货余额，不能自动生成库存反套执行计划。";
  }
  if (reasonCodes.includes("smart_arbitrage_inventory_backed_insufficient")) {
    return "当前识别到负基差，但现货库存不足，不能自动生成库存反套执行计划。";
  }
  if (reasonCodes.includes("smart_arbitrage_margin_short_disabled")) {
    return "当前识别到负基差，配置要求走保证金融券反套，但这条执行模式当前未启用。";
  }
  if (reasonCodes.includes("smart_arbitrage_margin_short_execution_not_ready")) {
    return "当前识别到负基差，配置要求走保证金融券反套，但执行链路尚未接通。";
  }
  if (reasonCodes.includes("smart_arbitrage_margin_short_margin_mode_mismatch")) {
    return "当前识别到负基差，但保证金融券反套的现货保证金模式与运行配置不一致，系统暂不执行。";
  }
  if (reasonCodes.includes("smart_arbitrage_drag_exceeds_basis")) {
    return `当前有基差，但扣掉可执行磨损后净优势已经不够。${smartArbitragePrimaryDragDriver(candidate?.metrics || {})}`;
  }
  if (reasonCodes.includes("smart_arbitrage_executable_edge_negative")) {
    return `当前理论上有价差，但按可执行磨损估算已经不值得做。${smartArbitragePrimaryDragDriver(candidate?.metrics || {})}`;
  }
  if (reasonCodes.includes("smart_arbitrage_funding_window_unfavorable")) {
    return "当前主要被资金费窗口拖累，可执行净优势已经不足，系统暂不进场。";
  }
  if (reasonCodes.includes("smart_arbitrage_borrow_window_unfavorable")) {
    return "当前主要被借币计息窗口拖累，可执行净优势已经不足，系统暂不进场。";
  }
  if (reasonCodes.includes("smart_arbitrage_spot_carry_not_allowed")) {
    return "当前是正基差，但这组配对没有开放正向现货套利模式，系统暂不执行。";
  }
  if (reasonCodes.includes("smart_arbitrage_inventory_reverse_carry_not_allowed")) {
    return "当前识别到负基差，但这组配对没有开放库存反向套利模式。";
  }
  if (reasonCodes.includes("smart_arbitrage_margin_reverse_carry_not_allowed")) {
    return "当前识别到负基差，但这组配对没有开放保证金反向套利模式。";
  }
  if (reasonCodes.includes("smart_arbitrage_existing_pair_mode_not_allowed_by_config")) {
    return "当前这组套利对来自旧配置，虽然新开模式已不再允许，但系统仍会继续恢复或退出现有双腿。";
  }
  if (smartArbitrageExitBlockedByKillSwitch(candidate, context)) {
    return "当前套利对已经进入退出阶段，但平仓提交被 kill switch 阻断，双腿暂时只能原地保持。";
  }
  if (reasonCodes.includes("smart_arbitrage_exit_ready")) {
    return "当前套利对已经达到退出条件，系统会优先收口双腿。";
  }
  if (smartArbitrageWaitingExit(candidate, context)) {
    return "当前套利对仍在持有区间，系统继续保持现有双腿；这不是挂单未成。";
  }
  if (reasonCodes.includes("smart_arbitrage_partial_fill_recovery")) {
    return "当前套利双腿不平衡，系统会优先恢复缺失腿。";
  }
  if (reasonCodes.includes("smart_arbitrage_protective_directional_exit_retained")) {
    return "当前更像方向策略的保护性退出场景，智能套利本轮不会接管。";
  }
  if (reasonCodes.includes("smart_arbitrage_existing_unpaired_exposure")) {
    return "当前账户里还有未配对的相关暴露，智能套利暂不接管。";
  }
  if (reasonCodes.includes("smart_arbitrage_mixed_pair_direction_detected")) {
    return "当前套利对存在方向混杂或脏仓位，系统暂不接管，避免在污染状态上继续叠加双腿。";
  }
  if (reasonCodes.includes("smart_arbitrage_mixed_reverse_execution_modes_detected")) {
    return "当前套利对同时混入库存反套和保证金反套，系统暂不自动接管。";
  }
  return "";
}

function smartArbitrageStateMeta(candidate) {
  const parts = [];
  const pairId = candidate?.pair_id || candidate?.metrics?.pair_id;
  const statePhase = candidate?.state_phase || candidate?.metrics?.state_phase;
  const executionMode = candidate?.execution_mode || candidate?.metrics?.execution_mode;
  const confidence = Number(candidate?.confidence);
  if (pairId) parts.push(`pair ${pairId}`);
  if (statePhase) parts.push(`阶段 ${readableState(statePhase)}`);
  if (executionMode) parts.push(`模式 ${readableState(executionMode)}`);
  if (candidate?.metrics?.executable_edge_bps !== undefined && candidate?.metrics?.executable_edge_bps !== null) {
    parts.push(`可执行净优势 ${formatBps(candidate.metrics.executable_edge_bps)}`);
  }
  if (Number.isFinite(confidence) && confidence > 0) parts.push(`置信度 ${formatNumber(confidence, 2, "0")}`);
  return parts.join(" | ") || "当前没有额外状态量化信息";
}

function smartArbitrageBlockingSummary(candidate) {
  const blockingReasons = Array.isArray(candidate?.blocking_reasons)
    ? candidate.blocking_reasons
    : Array.isArray(candidate?.metrics?.blocking_reasons)
      ? candidate.metrics.blocking_reasons
      : [];
  const summary = reasonListText(blockingReasons, "");
  if (summary) return summary;
  return "当前存在阻断条件，因此暂不生成自动执行量。";
}

function escapeFallbackReadableState(value, fallback) {
  return readableState(value || fallback || "unknown");
}

function renderTrialVerdictActions(workbenchActions, fallback) {
  if (Array.isArray(workbenchActions) && workbenchActions.length) {
    return `<div class="stack-actions table-actions--compact">${workbenchActions.map(renderWorkbenchActionButton).join("")}</div>`;
  }
  const { trialGuardStatus, trialGuardHardStopActive, trialVerdict } = fallback;
  const actions = [
    actionButton("查看风险与恢复", "navigate-view", "risk", "ghost"),
  ];
  if (trialGuardHardStopActive || trialGuardStatus === "breached") {
    actions.push(
      actionButton("查看委托与成交", "navigate-view", "execution", "ghost"),
      actionButton("记录本次复盘", "record-trial-review", "", "secondary"),
      actionButton("刷新当前状态", "refresh-dashboard", "", "warning"),
    );
    return `<div class="stack-actions table-actions--compact">${actions.join("")}</div>`;
  }
  if (trialVerdict === "approve_scale_up") {
    actions.push(actionButton("提交放量评审", "record-scaling-review", "approve_scale_up", "warning"));
  } else if (trialVerdict === "continue_small_capital") {
    actions.push(actionButton("记为继续小资金试盘", "record-scaling-review", "continue_small_capital", "secondary"));
  } else if (trialVerdict === "shrink_trial") {
    actions.push(actionButton("记为缩小试盘规模", "record-scaling-review", "shrink_trial", "warning"));
  } else if (trialVerdict === "pause_trial") {
    actions.push(actionButton("记为暂停试盘并复盘", "record-scaling-review", "pause_trial", "warning"));
  }
  actions.push(actionButton("记录本次复盘", "record-trial-review", "", "ghost"));
  return `<div class="stack-actions table-actions--compact">${actions.join("")}</div>`;
}

function renderWorkbenchActionButton(item) {
  return actionButton(
    item?.label || "执行动作",
    item?.client_action || "refresh-dashboard",
    item?.value || "",
    item?.tone || "ghost"
  );
}

function renderTrialReviewHistory(items) {
  if (!Array.isArray(items) || !items.length) {
    return `
      <div class="section-block">
        <h4>最近处理记录</h4>
        <p class="meta-copy">当前还没有新的试盘处理记录。</p>
      </div>
    `;
  }
  return `
    <div class="section-block">
      <h4>最近处理记录</h4>
      ${responsiveTable(
        ["时间", "处理动作", "操作人", "说明"],
        items.map((item) => [
          `<div><strong>${formatRelativeAge(item.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.created_at)}</div></div>`,
          `<div><strong>${escapeHtml(item.label || readableState(item.selected_action || item.action || "unknown"))}</strong><div class="table-meta">${escapeHtml(readableState(item.status || "unknown"))}</div></div>`,
          `<div><strong>${escapeHtml(item.actor_identity || item.actor_role || "操作人待确认")}</strong><div class="table-meta">${escapeHtml(readableState(item.auth_source || "unknown"))}</div></div>`,
          `<div><strong>${escapeHtml(item.reason || "当前没有额外原因")}</strong></div>`,
        ]),
        "当前还没有新的试盘处理记录。"
      )}
    </div>
  `;
}

function scalingVerdictLabel(value) {
  const labels = {
    approve_scale_up: "允许进入下一档资金评审",
    continue_small_capital: "继续小资金试盘",
    shrink_trial: "建议缩容试盘",
    pause_trial: "建议暂停试盘",
  };
  return labels[String(value || "")] || readableState(value || "unknown");
}

function scalingVerdictTone(value) {
  if (value === "approve_scale_up") return "positive";
  if (value === "continue_small_capital") return "info";
  if (value === "shrink_trial") return "warning";
  if (value === "pause_trial") return "danger";
  return "neutral";
}

function trialGuardHardStopLabel(hardStop, status) {
  if (hardStop?.active) return "已触发";
  if (status === "recovered") return "已恢复";
  if (status === "warming_up") return "预热中";
  if (status === "disabled" || status === "not_configured") return "未启用";
  if (status === "monitoring") return "监控中";
  return readableState(status || "unknown");
}

function hardStopRequirementText(hardStop) {
  const items = hardStop?.recovery_requirements?.items;
  if (!Array.isArray(items) || !items.length) {
    return hardStop?.summary || "当前没有额外的试盘守护说明。";
  }
  return items
    .map((item) => String(item?.requirement || "").trim())
    .filter(Boolean)
    .join("；");
}

function formatSegmentLabel(segment) {
  if (!segment || typeof segment !== "object") return "当前没有可用分层切片";
  const parts = [
    segment.symbol,
    segment.market_regime,
    segment.side,
    segment.execution_action,
  ].filter(Boolean);
  return parts.length ? parts.map((item) => readableState(item)).join(" | ") : "当前没有可用分层切片";
}

function forwardPeriodStatusLabel(value) {
  const labels = {
    healthy: "稳定通过",
    caution: "收益转弱，建议谨慎",
    failing: "已触发警戒",
    insufficient_data: "样本仍少，先继续观察",
  };
  return labels[String(value || "")] || readableState(value || "unknown");
}

function reasonListText(value, fallback) {
  const rows = splitStrategyReasons(value).map((item) => strategyReasonText(item)).filter(Boolean);
  return rows.length ? rows.join("；") : fallback;
}

function splitStrategyReasons(value) {
  const items = Array.isArray(value) ? value : [value];
  return items.map((item) => String(item ?? "").trim()).filter(Boolean);
}

function plainListText(value, fallback) {
  const rows = splitStrategyReasons(value);
  return rows.length ? rows.join("；") : fallback;
}

function mergeReasonLists(...values) {
  return values.flatMap((value) => splitStrategyReasons(value));
}

function chooseTrialVerdict(...values) {
  for (const value of values) {
    if (value) return value;
  }
  return "unknown";
}

function strategyReasonText(value) {
  const map = {
    no_forward_validation_rows: "当前还没有足够的已完成成交，暂时无法做前向验证。",
    insufficient_forward_validation_sample: "最近一个观察周期的样本还不够，先继续观察，不适合下强结论。",
    negative_net_realized_pnl: "最近一个观察周期净收益转负，说明试盘边际开始变弱。",
    execution_quality_or_fee_threshold_breached: "最近一个观察周期的执行质量或手续费拖累已经超过允许范围。",
    forward_validation_loss_limit_breached: "最近一个观察周期已经触碰试盘亏损上限。",
    trial_guard_not_enabled: "试盘守护当前未启用，这份试盘建议只能作为参考，不适合直接拿来做放量判断。",
    trial_profile_not_active: "当前不在试盘档位，先不要做放量判断。",
    trial_observation_flow_inactive: "当前运行线不在试盘观察流程里，这份试盘建议只能作为参考，不能直接拿来决定放量或恢复。",
    trial_guard_hard_stop_active: "试盘守护当前处于硬停机状态，先处理停机原因，再谈恢复或放量。",
    runtime_halted: "系统当前处于暂停状态，先处理暂停原因。",
    recovery_not_safe_to_trade: "当前恢复状态还不允许继续自动交易。",
    manual_review_required: "当前仍有人工复核要求，先处理复核再谈放量。",
    active_execution_blockers_present: "当前仍有执行阻断项没有清掉。",
    trial_guard_breached: "试盘守护已经触发警戒，当前不适合继续放量。",
    forward_validation_pause: "最近周期已经达到建议暂停的条件。",
    forward_validation_shrink: "最近周期更适合先缩容观察。",
    forward_validation_still_observing: "最近周期还在观察期，样本还不够稳。",
    forward_validation_stable: "最近周期暂时稳定，可以继续积累样本。",
    healthy_period_requirement_not_met: "连续健康周期还没达标，现在谈放量还太早。",
    scale_up_requirements_met: "样本、恢复状态和执行质量都达到进入下一档资金评审的条件。",
  };
  return map[String(value || "").trim()] || readableState(value || "unknown");
}

function trialObservationLabel(requirements) {
  if (!requirements?.trial_guard_enabled) return "试盘守护未启用";
  if (!requirements?.trial_observation_flow_active) return "当前不在试盘观察流程";
  return "试盘守护正在本运行线生效";
}

function cooldownSummary(cooldowns) {
  if (!cooldowns || typeof cooldowns !== "object") return "当前没有冷却限制";
  const parts = Object.entries(cooldowns)
    .filter(([, value]) => Number(value) > 0)
    .map(([key, value]) => `${humanCooldownLabel(key)}：${formatDuration(value)}`);
  return parts.length ? parts.join(" | ") : "当前没有冷却限制";
}

function humanCooldownLabel(key) {
  const normalized = String(key || "").replaceAll("_remaining_seconds", "");
  const labels = {
    reentry: "再次开仓冷却",
    reversal: "反手冷却",
    reduce: "减仓冷却",
    close: "平仓冷却",
  };
  return labels[normalized] || normalized;
}

function listText(value, fallback) {
  return localizeList(value, fallback);
}

function optionalState(value, fallback) {
  return value ? readableState(value) : fallback;
}

function formattedOrText(value, fallback) {
  return formatNumber(value, 4, fallback);
}

function numberMeta(label, value, fallback) {
  const text = formatNumber(value, 4, fallback);
  return text === fallback ? fallback : `${label} ${text}`;
}

function isHoldIntent(target = {}) {
  if (hasFamilyExecutionSummary(target)) {
    return false;
  }
  return String(target.position_intent || target.route_action || "").trim().toLowerCase() === "hold";
}

function targetDeltaValue(target = {}, decisionScene) {
  if (target.delta_position_qty === null || target.delta_position_qty === undefined || target.delta_position_qty === "") {
    return isHoldIntent(target)
      ? decisionScene === "derivatives"
        ? "保持净仓位"
        : "保持持仓"
      : "暂无目标变化";
  }
  return formatSigned(target.delta_position_qty);
}

function currentPositionValue(target = {}, decisionScene) {
  if (target.current_position_qty === null || target.current_position_qty === undefined || target.current_position_qty === "") {
    return decisionScene === "derivatives" ? "暂无净仓位" : "暂无持仓";
  }
  return formatSigned(target.current_position_qty);
}

function targetDirectionLabel(target = {}, decisionScene) {
  if (isHoldIntent(target)) {
    return decisionScene === "derivatives" ? "保持当前净仓位" : "保持当前持仓";
  }
  return readableFamilyExecutionDirection(
    target,
    decisionScene === "derivatives" ? "当前没有新的方向调整" : "当前没有新的持仓方向调整",
  );
}

function targetPositionValue(target = {}, decisionScene) {
  if (target.target_position_qty === null || target.target_position_qty === undefined || target.target_position_qty === "") {
    return isHoldIntent(target)
      ? decisionScene === "derivatives"
        ? "保持当前净仓位"
        : "保持当前持仓"
      : "暂无目标仓位";
  }
  return formatSigned(target.target_position_qty);
}

function targetPlanMeta(target = {}, decisionScene) {
  return `${readableFamilyExecutionSummary(target, "保持当前仓位")} | ${
    decisionScene === "derivatives"
      ? numberMeta("目标杠杆", target.target_leverage, "当前没有目标杠杆")
      : readableFamilyExecutionMeta(target, "当前没有方向补充说明")
  }`;
}

function latestOrderStatusLabel(order = null) {
  if (!order) return "暂无委托";
  return readableState(order.status || "unknown");
}
