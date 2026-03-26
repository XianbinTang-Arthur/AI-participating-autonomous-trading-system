import { actionButton, callout, kvList, pill, responsiveTable, statGrid, summaryStrip, surfaceCard } from "../components.js";
import { localizeList, summarizeLocalizedList } from "../copy.js";
import { escapeHtml, formatDuration, formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, middleEllipsis } from "../formatters.js";
import { readableState } from "../terms.js";
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
  const strategyAttribution = data.strategyAttribution || {};
  const attributionSummary = strategyAttribution.summary || {};
  const sleeveProfitability = strategyAttribution.profitability_by_strategy_sleeve || [];
  const sleeveInventorySummary = strategyAttribution.sleeve_inventory_summary || [];
  const strategyFamilyEnablement = strategyRuntime.family_enablement || {};
  const baseline = latestDecision.baseline_assessment || {};
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
          ["执行约束", listText(target.guardrail_flags, "当前没有额外执行限制"), `预期净优势 ${formatBps(target.expected_net_edge_bps)} | 信号 ${formatBps(target.expected_signal_edge_bps)} | 成本 ${formatBps(target.expected_cost_bps)}`],
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
            label: "最近执行 Bundle",
            value: readableState(strategyRuntimeSummary.latest_bundle_status || "unknown"),
            meta: latestBundle.bundle_id ? middleEllipsis(latestBundle.bundle_id, 10, 8, "当前没有策略执行 bundle") : "当前没有策略执行 bundle",
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
          ["当前路由", readableState(strategyRuntimeSummary.latest_selected_route_action || "override_target"), strategyRuntimeSummary.protective_fallback_active ? "当前保留了保护性减仓/退出路径。" : "当前没有触发保护性回退。"],
          ["Allocator 结论", latestAllocationDecision.operator_summary || "当前还没有 allocator 决策。", reasonListText(latestAllocationDecision.reason_codes, "当前没有 allocator 级原因说明")],
          [
            "Hedge 保护 / 方向削减",
            `${formatSigned(strategyRuntimeSummary.latest_hedge_protected_notional)} / ${formatSigned(strategyRuntimeSummary.latest_directional_reduced_notional)}`,
            "前者表示为保护 hedge 结构而保留的名义金额，后者表示 allocator 主动削减的方向暴露。",
          ],
          [
            "最近 Bundle / 已应用目标",
            `${readableState(latestBundle.status || strategyRuntimeSummary.latest_bundle_status || "unknown")} / ${formatSigned(strategyAppliedTarget.target_position_qty)}`,
            `${formatNumber(recentBundles[0]?.legs?.length ?? latestBundle.legs?.length ?? 0, 0, "0")} 条腿 | ${readableState(strategyAppliedTarget.position_intent || "hold")}`,
          ],
        ])}
        ${renderStrategyCandidateTable(displayedStrategyCandidates)}
        ${renderRecentSleeveIntentTable(recentSleeveIntents.slice(0, 5))}
        ${renderExpandableSection("预算快照", renderAllocatorBudgetSnapshotTable(recentBudgetSnapshots), {
          meta: `${formatNumber(strategyRuntimeSummary.latest_budget_snapshot_count, 0, "0")} 条`,
        })}
        ${renderExpandableSection("冲突解算", renderAllocatorConflictResolutionTable(recentConflictResolutions), {
          meta: `${formatNumber(strategyRuntimeSummary.latest_conflict_resolution_count, 0, "0")} 条`,
        })}
        ${renderExpandableSection("净额决策", renderAllocatorNettingDecisionTable(recentNettingDecisions), {
          meta: `${formatNumber(strategyRuntimeSummary.latest_netting_decision_count, 0, "0")} 条`,
        })}
      `,
    }),
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
            label: "活跃 Sleeve",
            value: formatNumber(strategyRuntimeSummary.automation_active_count, 0, "0"),
            meta: "当前在自动预算内正常运行",
            tone: "positive",
          },
          {
            label: "收缩中的 Sleeve",
            value: formatNumber(strategyRuntimeSummary.automation_contracted_count, 0, "0"),
            meta: "预算已收缩或只保留保护性管理",
            tone: strategyRuntimeSummary.automation_contracted_count ? "warning" : "info",
          },
          {
            label: "暂停中的 Sleeve",
            value: formatNumber(strategyRuntimeSummary.automation_paused_count, 0, "0"),
            meta: "当前已被系统自动暂停",
            tone: strategyRuntimeSummary.automation_paused_count ? "danger" : "info",
          },
        ])}
        ${kvList([
          [
            "最新预算权重",
            Object.keys(strategyRuntimeSummary.latest_approved_sleeve_weights || {}).length ? "allocator 已生成权重" : "当前没有新的预算权重",
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
          ["Sleeve", "自动状态", "预算倍率", "权重", "最近净收益"],
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
            label: "库存最大 Sleeve",
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
            "主要 Sleeve 收益",
            sleeveProfitability[0]?.strategy_sleeve_id || "当前没有 sleeve 级收益记录",
            sleeveProfitability[0] ? `净收益 ${formatSigned(sleeveProfitability[0].combined_net_realized_pnl)} | 记录 ${formatNumber(sleeveProfitability[0].record_count, 0, "0")} 条` : "等第一批 fill / funding fee 归因记录落地后，这里会自动出现。",
          ],
          [
            "库存归属概览",
            sleeveInventorySummary[0]?.strategy_sleeve_id || "当前没有 open lot",
            sleeveInventorySummary[0] ? `库存名义金额 ${formatSigned(sleeveInventorySummary[0].inventory_notional)} | ${formatNumber(sleeveInventorySummary[0].open_lot_count, 0, "0")} 个 lot` : "当前没有需要追踪的 sleeve 库存。",
          ],
          [
            "Bundle 归因",
            formatNumber((strategyAttribution.profitability_by_strategy_bundle || []).length, 0, "0"),
            "按 allocation / bundle 的收益归因已经进入组合报表，可用于排查多腿执行后的收益归属。",
          ],
        ])}
        ${responsiveTable(
          ["Sleeve", "净收益", "资金费", "库存变化", "库存名义金额"],
          displayedSleeveProfitability.map((item) => {
            const inventory = sleeveInventorySummary.find((row) => row.strategy_sleeve_id === item.strategy_sleeve_id) || {};
            return [
              `<div><strong>${escapeHtml(item.strategy_sleeve_id || "未归属")}</strong><div class="table-meta">${escapeHtml((item.families || []).join(" / ") || "当前没有家族标签")}</div></div>`,
              `<div><strong>${formatSigned(item.combined_net_realized_pnl)}</strong><div class="table-meta">实现 ${formatSigned(item.realized_pnl)}</div></div>`,
              `<div><strong>${formatSigned(item.funding_fee_amount)}</strong><div class="table-meta">费用 ${formatSigned(item.fee_amount)}</div></div>`,
              `<div><strong>${formatSigned(item.inventory_move_qty)}</strong><div class="table-meta">${formatNumber(item.record_count, 0, "0")} 条记录</div></div>`,
              `<div><strong>${formatSigned(inventory.inventory_notional)}</strong><div class="table-meta">${formatNumber(inventory.open_lot_count, 0, "0")} 个 open lot</div></div>`,
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
        })}
        ${renderExpandableSection("最近处理记录", renderTrialReviewHistory(displayedTrialReviewActions), {
          meta: `${formatNumber(displayedTrialReviewActions.length, 0, "0")} 条`,
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
  return `
    <div class="panel-grid strategy-page-grid">
      <div class="span-12">${sections.strategyHero}</div>
      <div class="span-12">${sections.strategyDecisionWorkbench}</div>
      <div class="span-12">${sections.strategyTrialVerdict}</div>
      <div class="span-12">${sections.strategyCoordinator}</div>
      <div class="span-12">${sections.strategyAutomation}</div>
      <div class="span-12">${sections.strategyAttribution}</div>
      <div class="span-12">${sections.strategyHistory}</div>
    </div>
  `;
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
  return readableState(rawIntent);
}

function readableRecentIntent(item) {
  const rawIntent = String(item.position_intent || item.target_exposure_side || "hold").toLowerCase();
  const currentQty = Number(item.current_position_qty ?? 0);
  const targetQty = Number(item.target_position_qty ?? 0);
  if (rawIntent === "hold" && currentQty === 0 && targetQty === 0) {
    return "继续观望";
  }
  return readableState(rawIntent);
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

function renderStrategyCandidateTable(candidates) {
  if (!Array.isArray(candidates) || !candidates.length) {
    return `<p class="meta-copy">当前还没有候选策略快照。</p>`;
  }
  return responsiveTable(
    ["策略家族", "当前状态", "如何处理", "本轮目标", "原因说明"],
    candidates.map((candidate) => [
      `<div><strong>${escapeHtml(readableState(candidate.family || "unknown"))}</strong><div class="table-meta">${escapeHtml(candidate.recommended_symbol || "当前没有推荐标的")}</div></div>`,
      `<div><strong>${escapeHtml(strategyCandidateStateLabel(candidate))}</strong><div class="table-meta">${escapeHtml(strategyCandidateStateMeta(candidate))}</div></div>`,
      `<div><strong>${escapeHtml(strategyCandidateRouteLabel(candidate))}</strong><div class="table-meta">${escapeHtml(strategyCandidateRouteMeta(candidate))}</div></div>`,
      `<div><strong>${escapeHtml(strategyCandidateTargetLabel(candidate))}</strong><div class="table-meta">${escapeHtml(strategyCandidateTargetMeta(candidate))}</div></div>`,
      `<div><strong>${escapeHtml(strategyCandidateReason(candidate))}</strong><div class="table-meta">${escapeHtml(strategyLegSummary(candidate))}</div></div>`,
    ]),
    "当前没有候选策略快照。"
  );
}

function renderRecentSleeveIntentTable(items) {
  if (!Array.isArray(items) || !items.length) {
    return `<p class="meta-copy">当前还没有新的 sleeve 意图记录。</p>`;
  }
  return responsiveTable(
    ["最近 Sleeve 意图", "当前状态", "本轮目标", "自动预算", "原因说明"],
    items.map((item) => [
      `<div><strong>${escapeHtml(item.strategy_sleeve_id || "未归属")}</strong><div class="table-meta">${escapeHtml(readableState(item.family || "unknown"))} | ${escapeHtml(item.symbol || "标的待确认")}</div></div>`,
      `<div><strong>${escapeHtml(readableState(item.state || "unknown"))}</strong><div class="table-meta">${escapeHtml(readableState(item.route_action || "hold_current"))}</div></div>`,
      `<div><strong>${formatSigned(item.target_position_qty)}</strong><div class="table-meta">变化 ${formatSigned(item.delta_position_qty)}</div></div>`,
      `<div><strong>${item.automatic_enabled ? "自动管理" : "人工冻结"}</strong><div class="table-meta">倍率 ${formatNumber(item.budget_multiplier, 2, "0")} | 权重 ${formatNumber(item.allocator_weight, 2, "0")}</div></div>`,
      `<div><strong>${escapeHtml(item.control_summary || item.headline || "当前没有额外说明")}</strong><div class="table-meta">${escapeHtml(reasonListText(item.control_reason_codes?.length ? item.control_reason_codes : item.reason_codes, "当前没有额外原因"))}</div></div>`,
    ]),
    "当前还没有新的 sleeve 意图记录。"
  );
}

function strategyCandidateStateLabel(candidate) {
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

function strategyCandidateStateMeta(candidate) {
  if (candidate?.family === "smart_arbitrage" && candidate?.state === "inactive") {
    return smartArbitrageMarketAvailability(candidate);
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
  if (candidate?.route_action === "advisory_only") return "仅参考，不直接执行";
  if (candidate?.route_action === "hold_current") return "保持当前仓位";
  if (candidate?.route_action === "override_target") return "接管本轮目标";
  return readableState(candidate?.route_action || "hold_current");
}

function strategyCandidateRouteMeta(candidate) {
  return `优先级 ${escapeFallbackReadableState(candidate?.urgency, "low")}`;
}

function strategyCandidateTargetLabel(candidate) {
  if (smartArbitrageNegativeBasisAdvisory(candidate)) {
    return "当前负基差不自动下单";
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

function strategyCandidateTargetMeta(candidate) {
  if (smartArbitrageNegativeBasisAdvisory(candidate)) {
    return "自动执行当前只支持正基差双腿，现货现金模式不会为负基差生成执行量";
  }
  if (candidate?.family === "smart_arbitrage" && candidate?.state === "blocked") {
    return smartArbitrageBlockingSummary(candidate);
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
    return `<p class="meta-copy">当前还没有 allocator 预算快照。</p>`;
  }
  return `
    <div class="section-block">
      <h4>预算快照</h4>
      ${responsiveTable(
        ["Sleeve", "优先级", "请求名义 / 批准名义", "组合预算变化", "原因"],
        items.map((item) => [
          `<div><strong>${escapeHtml(item.strategy_sleeve_id || "未归属")}</strong><div class="table-meta">${escapeHtml(readableState(item.family || "unknown"))}</div></div>`,
          `<div><strong>${escapeHtml(readableState(item.hedge_priority_class || "standard"))}</strong><div class="table-meta">rank ${escapeHtml(String(item.priority_rank ?? 0))}</div></div>`,
          `<div><strong>${formatSigned(item.requested_notional)} -> ${formatSigned(item.approved_notional)}</strong><div class="table-meta">${formatSigned(item.requested_delta_qty)} -> ${formatSigned(item.approved_delta_qty)}</div></div>`,
          `<div><strong>${formatSigned(item.portfolio_requested_notional)} -> ${formatSigned(item.portfolio_approved_notional)}</strong><div class="table-meta">削减 ${formatSigned(item.portfolio_budget_cut_notional)}</div></div>`,
          `<div><strong>${escapeHtml(item.clamped ? "已裁剪" : "未裁剪")}</strong><div class="table-meta">${escapeHtml(reasonListText(item.reason_codes, "当前没有额外原因"))}</div></div>`,
        ]),
        "当前没有 allocator 预算快照。"
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
        ["类型", "参与 Sleeve", "请求 / 批准 / 阻断", "保护 / 削减", "原因"],
        items.map((item) => [
          `<div><strong>${escapeHtml(readableState(item.conflict_type || "unknown"))}</strong><div class="table-meta">${escapeHtml(readableState(item.resolution_action || "unknown"))}</div></div>`,
          `<div><strong>${escapeHtml((item.input_sleeve_ids || []).join(" | ") || "当前没有输入 sleeve")}</strong><div class="table-meta">批准 ${escapeHtml((item.approved_sleeve_ids || []).join(" | ") || "无")}</div></div>`,
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
        ["标的", "参与 Sleeve", "总买 / 总卖", "净批准数量", "原因"],
        items.map((item) => [
          `<div><strong>${escapeHtml(item.symbol || "标的待确认")}</strong><div class="table-meta">${escapeHtml(readableState(item.product_type || "unknown"))} | ${escapeHtml(readableState(item.margin_mode || "unknown"))}</div></div>`,
          `<div><strong>${escapeHtml((item.participating_sleeve_ids || []).join(" | ") || "当前没有参与 sleeve")}</strong></div>`,
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

function strategyCandidateReason(candidate) {
  const smartArbitrageReason = smartArbitrageReasonText(candidate);
  if (smartArbitrageReason) return smartArbitrageReason;
  if (candidate?.headline) return candidate.headline;
  const summary = reasonListText(candidate?.reason_codes, "");
  if (summary) return summary;
  return "当前没有额外说明";
}

function strategyLegSummary(candidate) {
  const legs = candidate?.legs;
  if (!Array.isArray(legs) || !legs.length) {
    if (candidate?.family === "smart_arbitrage") {
      return smartArbitrageMarketAvailability(candidate);
    }
    return "当前没有附带腿说明";
  }
  return legs
    .map((item) => {
      const mode = item?.execution_mode ? ` (${readableState(item.execution_mode)})` : "";
      return `${readableState(item.product_type)} ${readableState(item.side)} ${item.symbol || "标的待确认"}${mode}`;
    })
    .join(" | ");
}

function smartArbitrageMarketAvailability(candidate) {
  const metrics = candidate?.metrics || {};
  const spotSymbol = metrics.spot_symbol || candidate?.recommended_symbol || "现货腿";
  const derivativesSymbol = metrics.derivatives_symbol || "合约腿";
  const reasonCodes = Array.isArray(candidate?.reason_codes) ? candidate.reason_codes : [];
  if (reasonCodes.includes("smart_arbitrage_market_pair_incomplete")) {
    return `当前缺少 ${spotSymbol} 或 ${derivativesSymbol} 的配对行情，暂时不能进入双腿执行。`;
  }
  if (reasonCodes.includes("smart_arbitrage_symbol_pair_missing")) {
    return "当前没有识别到可用的现货/合约配对标的。";
  }
  if (smartArbitrageNegativeBasisAdvisory(candidate)) {
    return "当前是负基差提示单，系统不会下发套利双腿执行计划。";
  }
  if (reasonCodes.includes("smart_arbitrage_inventory_backed_insufficient")) {
    return "当前识别到负基差，但可用于反套的现货库存不足。";
  }
  if (reasonCodes.includes("smart_arbitrage_margin_short_execution_not_ready")) {
    return "当前识别到负基差，但保证金融券执行链路尚未接通。";
  }
  return "当前没有附带套利双腿执行信息。";
}

function smartArbitrageNegativeBasisAdvisory(candidate) {
  if (candidate?.family !== "smart_arbitrage") return false;
  const reasonCodes = Array.isArray(candidate?.reason_codes) ? candidate.reason_codes : [];
  return (
    reasonCodes.includes("smart_arbitrage_negative_basis") &&
    reasonCodes.includes("smart_arbitrage_spot_short_not_supported")
  );
}

function smartArbitrageReasonText(candidate) {
  if (smartArbitrageNegativeBasisAdvisory(candidate)) {
    return "当前是负基差，但自动执行只支持正基差双腿；现货现金模式不能自动做空。";
  }
  const reasonCodes = Array.isArray(candidate?.reason_codes) ? candidate.reason_codes : [];
  if (reasonCodes.includes("smart_arbitrage_inventory_backed_ready")) {
    return "当前是负基差，且账户现货库存足够，系统会按库存反套模式生成双腿计划。";
  }
  if (reasonCodes.includes("smart_arbitrage_margin_short_ready")) {
    return "当前是负基差，且保证金融券反套链路已就绪，系统会按借币卖出现货并买入合约的模式生成双腿计划。";
  }
  if (reasonCodes.includes("smart_arbitrage_inventory_backed_insufficient")) {
    return "当前识别到负基差，但现货库存不足，不能自动生成库存反套执行计划。";
  }
  if (reasonCodes.includes("smart_arbitrage_margin_short_execution_not_ready")) {
    return "当前识别到负基差，配置要求走保证金融券反套，但执行链路尚未接通。";
  }
  if (reasonCodes.includes("smart_arbitrage_margin_short_margin_mode_mismatch")) {
    return "当前识别到负基差，但保证金融券反套的现货保证金模式与运行配置不一致，系统暂不执行。";
  }
  if (reasonCodes.includes("smart_arbitrage_mixed_pair_direction_detected")) {
    return "当前套利对存在方向混杂或脏仓位，系统暂不接管，避免在污染状态上继续叠加双腿。";
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
  return readableState(
    target.target_exposure_side || target.position_intent,
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
  return `${readableState(target.position_intent || "hold")} | ${
    decisionScene === "derivatives"
      ? numberMeta("目标杠杆", target.target_leverage, "当前没有目标杠杆")
      : optionalState(target.target_exposure_side, "当前没有方向补充说明")
  }`;
}

function latestOrderStatusLabel(order = null) {
  if (!order) return "暂无委托";
  return readableState(order.status || "unknown");
}
