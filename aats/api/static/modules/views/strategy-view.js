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
  const forwardValidation = trialReviewSections.forward_validation || data.forwardValidation || {};
  const forwardSummary = forwardValidation.summary || {};
  const forwardPeriods = forwardValidation.periods || [];
  const scalingReadiness = trialReviewSections.scaling_readiness || data.scalingReadiness || {};
  const scalingRequirements = scalingReadiness.requirements || {};
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
            value: formatSigned(target.delta_position_qty),
            meta: readableState(target.target_exposure_side || target.position_intent, "方向待确认"),
          },
          {
            label: decisionScene === "derivatives" ? "当前净仓位" : "当前持仓",
            value: formatSigned(target.current_position_qty),
            meta: decisionScene === "derivatives" ? "按净仓位口径展示" : "按现货持仓口径展示",
          },
          {
            label: "最新决策时间",
            value: formatMaybeTimestamp(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts),
            meta: formatRelativeAge(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts),
          },
          {
            label: "最近执行结果",
            value: readableState(executionLatest.latest_order?.status || "unknown"),
            meta: middleEllipsis(executionLatest.latest_order?.client_order_id, 10, 6, "暂未生成委托"),
          },
        ])}
      `,
    }),
    strategyCoordinator: surfaceCard({
      title: "多策略调度",
      kicker: "并行策略",
      copy: "这里展示当前配置的主策略家族、最近一次调度结果，以及其他候选策略为什么没有接管执行。",
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
            label: "当前路由动作",
            value: readableState(strategyRuntimeSummary.latest_selected_route_action || "override_target"),
            meta: strategyRuntimeSummary.protective_fallback_active ? "当前保留了方向策略的保护性减仓/退出" : "当前没有触发保护性回退",
            tone: strategyRuntimeSummary.protective_fallback_active ? "warning" : "positive",
          },
          {
            label: "最近已应用目标",
            value: formatSigned(strategyAppliedTarget.target_position_qty),
            meta: `${readableState(strategyAppliedTarget.strategy_family || "directional")} | ${readableState(strategyAppliedTarget.strategy_route_action || "override_target")}`,
            tone: "info",
          },
          {
            label: "最近执行 Bundle",
            value: readableState(strategyRuntimeSummary.latest_bundle_status || "unknown"),
            meta: latestBundle.bundle_id ? middleEllipsis(latestBundle.bundle_id, 10, 8, "当前没有策略执行 bundle") : "当前没有策略执行 bundle",
            tone: strategyRuntimeSummary.latest_bundle_status === "blocked" ? "warning" : "info",
          },
          {
            label: "最近批准家族",
            value: formatNumber((strategyRuntimeSummary.latest_approved_families || []).length, 0, "0"),
            meta: localizeList(strategyRuntimeSummary.latest_approved_families || [], { fallback: "当前没有被批准的策略家族" }),
            tone: "info",
          },
        ])}
        ${kvList([
          ["调度结论", strategyRuntimeSummary.operator_summary || "当前还没有多策略调度快照。", reasonListText(strategyRuntimeSummary.latest_selection_reason_codes, "当前没有额外调度原因说明")],
          ["运行模板", strategyRuntimeSummary.env_template_profile || "当前未记录模板来源", strategyRuntimeSummary.automatic_selection_enabled ? "策略家族当前按系统自动选择运行。" : "策略家族当前不在自动选择模式。"],
          ["Allocator 结论", latestAllocationDecision.operator_summary || "当前还没有 allocator 决策。", reasonListText(latestAllocationDecision.reason_codes, "当前没有 allocator 级原因说明")],
          [
            "组合预算",
            `${formatSigned(strategyRuntimeSummary.latest_portfolio_requested_notional)} -> ${formatSigned(strategyRuntimeSummary.latest_portfolio_approved_notional)}`,
            `预算削减 ${formatSigned(strategyRuntimeSummary.latest_portfolio_budget_cut_notional)}`,
          ],
          [
            "预算与净额",
            readableState(strategyRuntimeSummary.latest_portfolio_risk_budget_state || "unknown"),
            [
              `预算快照 ${formatNumber(strategyRuntimeSummary.latest_budget_snapshot_count, 0, "0")} 条`,
              `冲突解算 ${formatNumber(strategyRuntimeSummary.latest_conflict_resolution_count, 0, "0")} 条`,
              `净额决策 ${formatNumber(strategyRuntimeSummary.latest_netting_decision_count, 0, "0")} 条`,
            ].join(" | "),
          ],
          [
            "Hedge 保护 / 方向削减",
            `${formatSigned(strategyRuntimeSummary.latest_hedge_protected_notional)} / ${formatSigned(strategyRuntimeSummary.latest_directional_reduced_notional)}`,
            "前者表示为保护 hedge 结构而保留的名义金额，后者表示 allocator 主动削减的方向暴露。",
          ],
          [
            "预期净优势 / 成本",
            `${formatSigned(strategyRuntimeSummary.latest_expected_edge_bps)} / ${formatSigned(strategyRuntimeSummary.latest_expected_cost_bps)}`,
            "按本轮批准后的 sleeve 权重聚合，用于解释 allocator 为什么这样分配预算。",
          ],
          ["最近 Bundle", readableState(latestBundle.status || "unknown"), reasonListText(latestBundle.reason_codes, "当前没有 bundle 级原因说明")],
          [
            "最近 Bundle 类型",
            `${readableState(strategyRuntimeSummary.latest_bundle_type || "unknown")} / ${readableState(strategyRuntimeSummary.latest_bundle_priority || "standard")}`,
            `${formatSigned(strategyRuntimeSummary.latest_bundle_gross_requested_exposure)} -> ${formatSigned(strategyRuntimeSummary.latest_bundle_net_approved_exposure)}`,
          ],
          ["最近已应用动作", readableState(strategyAppliedTarget.position_intent || "hold"), reasonListText(strategyAppliedTarget.strategy_reason_codes, "当前没有额外已应用目标说明")],
          ["最近 Bundle 腿数", formatNumber(recentBundles[0]?.legs?.length ?? latestBundle.legs?.length ?? 0, 0, "0"), latestBundle.operator_summary || "当前没有 bundle 级执行摘要"],
          ["最近 Sleeve Intent", formatNumber(recentSleeveIntents.length, 0, "0"), recentSleeveIntents[0]?.headline || "当前没有最新 sleeve intent 摘要"],
        ])}
        ${renderStrategyCandidateTable(strategyCandidates)}
        ${renderAllocatorBudgetSnapshotTable(recentBudgetSnapshots)}
        ${renderAllocatorConflictResolutionTable(recentConflictResolutions)}
        ${renderAllocatorNettingDecisionTable(recentNettingDecisions)}
      `,
    }),
    strategyAutomation: surfaceCard({
      title: "自动预算与启停",
      kicker: "全自动并行运行",
      copy: "系统会按最近归因、恢复状态和波动环境自动决定 sleeve 是否继续运行、给多少预算、是否只保留保护性管理。",
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
              `最小预算倍率 ${formatNumber(strategyRuntime.configured_parameters?.strategy_sleeve_auto_min_budget_multiplier, 2, "待确认")}`,
              `软亏损 ${formatSigned(strategyRuntime.configured_parameters?.strategy_sleeve_auto_soft_loss_usdt)}`,
              `硬亏损 ${formatSigned(strategyRuntime.configured_parameters?.strategy_sleeve_auto_hard_loss_usdt)}`,
            ].join(" | "),
          ],
        ])}
        ${responsiveTable(
          ["Sleeve", "自动状态", "预算倍率", "权重", "最近净收益"],
          automationDecisions.map((item) => [
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
      copy: "这里把收益、资金费和库存都按 sleeve 收口，便于分清谁在赚钱、谁还占着仓位。",
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
          sleeveProfitability.slice(0, 8).map((item) => {
            const inventory = sleeveInventorySummary.find((row) => row.strategy_sleeve_id === item.strategy_sleeve_id) || {};
            return [
              `<div><strong>${escapeHtml(item.strategy_sleeve_id || "未归属")}</strong><div class="table-meta">${escapeHtml((item.families || []).join(" / ") || "家族待确认")}</div></div>`,
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
    strategySignal: surfaceCard({
      title: "信号说明",
      kicker: "结论依据",
      copy: "先看四个核心摘要，再看门禁和拦截原因。",
      classes: "strategy-signal-card",
      content: `
        ${renderSignalGrid([
          {
            label: "交易场景",
            value: decisionScene === "derivatives" ? "合约" : "现货",
            meta: decisionScene === "derivatives" ? optionalState(target.margin_mode, "保证金模式待确认") : "现金买卖",
            tone: "info",
          },
          {
            label: "基础信号强度",
            value: formattedOrText(baseline.confidence, "待确认"),
            meta: numberMeta("综合强度", baseline.composite_alpha_score, "本轮还没有综合强度结果"),
            tone: "info",
          },
          {
            label: "AI 参考",
            value: optionalState(ai.summary || ai.direction_bias, "暂无 AI 参考"),
            meta: numberMeta("置信度", ai.confidence, "当前没有 AI 置信度"),
            tone: "info",
          },
          {
            label: decisionScene === "derivatives" ? "目标净仓位" : "目标持仓",
            value: formatSigned(target.target_position_qty),
            meta: decisionScene === "derivatives" ? numberMeta("目标杠杆", target.target_leverage, "目标杠杆待确认") : optionalState(target.target_exposure_side, "目标方向待确认"),
            tone: "info",
          },
        ])}
        ${kvList([
          ["基础信号说明", summarizeLocalizedList(baseline.reasons, { fallback: "本轮没有额外信号说明", limit: 4 }), numberMeta("微观结构强度", baseline.microstructure_alpha, "当前没有微观结构强度")],
          ["策略门禁", policy.execution_allowed ? "本轮允许进入执行" : "策略层未放行", policy.execution_allowed ? listText(policy.allow_reasons, "当前没有额外门禁说明") : listText(policy.blocker_reasons, "当前没有给出具体拦截原因")],
          ["风控结论", risk.approved ? "风控允许执行" : "风控仍在拦截", risk.approved ? listText(risk.approval_reasons, "当前没有额外放行说明") : listText(risk.rejection_reasons, "当前没有额外拦截说明")],
        ])}
      `,
    }),
    strategyHealth: surfaceCard({
      title: "执行约束",
      kicker: "质量约束",
      copy: "把最近成交质量、冷却状态和保护规则直接摆出来，便于一眼判断当前是不是在无效来回交易。",
      content: `
        ${statGrid([
          {
            label: "最近平仓样本",
            value: formatNumber(strategyHealth.recent_closed_trade_count, 0),
            meta: strategyHealth.latest_fill_timestamp ? `最近成交 ${formatRelativeAge(strategyHealth.latest_fill_timestamp)}` : "当前暂无平仓样本",
          },
          {
            label: "胜率",
            value: formatRatio(strategyHealth.recent_win_rate),
            meta: "按最近已闭合交易统计",
          },
          {
            label: "手续费拖累",
            value: formatRatio(strategyHealth.recent_fee_drag_ratio),
            meta: formatSigned(strategyHealth.recent_fee_total),
          },
          {
            label: "来回交易占比",
            value: formatRatio(strategyHealth.recent_churn_ratio),
            meta: `连续低净优势 ${formatNumber(strategyHealth.recent_low_edge_trade_streak, 0)} 笔`,
          },
        ])}
        ${kvList([
          ["当前保护规则", listText(strategyHealth.guardrail_flags, "当前没有额外保护规则"), cooldownSummary(strategyHealth.cooldowns)],
          ["最早持仓开始", formatMaybeTimestamp(latestDecision.decision_context?.current_position_opened_at || strategyHealth.current_position_opened_at), holdAge(latestDecision.decision_context?.current_position_opened_at || strategyHealth.current_position_opened_at)],
          ["最近平仓时间", formatMaybeTimestamp(latestDecision.decision_context?.last_position_closed_at || strategyHealth.last_position_closed_at), formatRelativeAge(latestDecision.decision_context?.last_position_closed_at || strategyHealth.last_position_closed_at)],
          ["预期净优势", formatBps(target.expected_net_edge_bps), `信号优势 ${formatBps(target.expected_signal_edge_bps)} / 成本 ${formatBps(target.expected_cost_bps)}`],
          ["本轮执行限制", listText(target.guardrail_flags, "当前没有额外执行限制"), `目标动作 ${readableState(target.position_intent || "hold")}`],
        ])}
      `,
    }),
    strategyTrialVerdict: surfaceCard({
      title: "系统自动试盘结论",
      kicker: "试盘审查",
      copy: "这里汇总系统自动给出的试盘建议、最近观察周期表现，以及当前是否满足继续试盘或进入下一步评估的前置条件。",
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
            label: "运行前置条件",
            value: scalingRequirements.safe_to_trade ? "当前可继续观察" : "当前先不要推进",
            meta: `${scalingRequirements.review_required ? "仍需人工复核" : "当前无需人工复核"} | 阻断 ${formatNumber(scalingRequirements.active_blocker_count, 0, "0")} 项`,
            tone: scalingRequirements.safe_to_trade && !scalingRequirements.review_required && Number(scalingRequirements.active_blocker_count || 0) === 0 ? "positive" : "danger",
          },
        ])}
        ${kvList([
          [
            "为什么系统给出这个结论",
            trialHeadline,
            reasonListText(trialReasons, "当前没有额外原因说明"),
          ],
          [
            "最近观察周期",
            forwardVerdictLabel(forwardSummary.verdict),
            reasonListText(forwardSummary.reasons, "当前还没有形成额外的周期说明"),
          ],
          [
            "当前运行前置条件",
            scalingRequirements.trial_guard_profile_active ? "试盘守护已启用" : "试盘守护未启用",
            [
              scalingRequirements.trial_guard_status === "monitoring" ? "试盘守护正在监控" : "当前不在试盘档位",
              scalingRequirements.safe_to_trade ? "恢复状态允许继续运行" : "恢复状态暂不允许继续自动运行",
              Number(scalingRequirements.active_blocker_count || 0) > 0 ? `仍有 ${formatNumber(scalingRequirements.active_blocker_count, 0, "0")} 项阻断未清除` : "当前没有新的执行阻断",
            ].join("；"),
          ],
          [
            "最近一周复盘参考",
            plainListText(trialReviewRecommendation.action_items, "当前没有新的周度动作建议"),
            trialReviewSummary.headline || reasonListText(trialReviewRecommendation.reasons, "当前没有额外复盘说明"),
          ],
          ["最强分层切片", formatSegmentLabel(trialReviewSections.strategy_segments?.strongest_segment?.segment), formatSigned(trialReviewSections.strategy_segments?.strongest_segment?.net_realized_pnl)],
          ["最弱分层切片", formatSegmentLabel(trialReviewSections.strategy_segments?.weakest_segment?.segment), formatSigned(trialReviewSections.strategy_segments?.weakest_segment?.net_realized_pnl)],
        ])}
        ${renderForwardValidationPeriods(forwardPeriods)}
      `,
    }),
    strategyHistory: surfaceCard({
      title: "决策记录",
      kicker: "历史记录",
      copy: "桌面端保留表格，窄屏自动切成卡片，方便值班时在手机上快速扫读。",
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
    <div class="panel-grid">
      <div class="span-6">${sections.strategyHero}</div>
      <div class="span-6">${sections.strategyCoordinator}</div>
      <div class="span-12">${sections.strategyAutomation}</div>
      <div class="span-12">${sections.strategyAttribution}</div>
      <div class="span-12">${sections.strategySignal}</div>
      <div class="span-12">${sections.strategyHistory}</div>
      <div class="span-12">${sections.strategyHealth}</div>
      <div class="span-12">${sections.strategyTrialVerdict}</div>
    </div>
  `;
}

function renderSignalGrid(items) {
  if (!items.length) return "";
  return `
    <div class="summary-strip summary-strip--quad">
      ${items
        .map(
          (item) => `
            <article class="summary-tile tone-${escapeHtml(item.tone || "neutral")}">
              <span class="summary-tile__label">${escapeHtml(item.label)}</span>
              <strong class="summary-tile__value">${escapeHtml(item.value)}</strong>
              ${item.meta ? `<span class="summary-tile__meta">${escapeHtml(item.meta)}</span>` : ""}
            </article>
          `
        )
        .join("")}
    </div>
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
    ["策略家族", "状态", "路由", "目标", "说明"],
    candidates.map((candidate) => [
      `<div><strong>${escapeHtml(readableState(candidate.family || "unknown"))}</strong><div class="table-meta">${escapeHtml(candidate.recommended_symbol || "当前没有推荐标的")}</div></div>`,
      `<div><strong>${escapeHtml(readableState(candidate.state || "unknown"))}</strong><div class="table-meta">${escapeHtml(formatNumber(candidate.confidence, 2, "待确认"))}</div></div>`,
      `<div><strong>${escapeHtml(readableState(candidate.route_action || "hold_current"))}</strong><div class="table-meta">${escapeHtml(readableState(candidate.urgency || "low"))}</div></div>`,
      `<div><strong>${escapeHtml(formatSigned(candidate.target_position_qty))}</strong><div class="table-meta">${escapeHtml(formatSigned(candidate.delta_position_qty))}</div></div>`,
      `<div><strong>${escapeHtml(strategyCandidateReason(candidate))}</strong><div class="table-meta">${escapeHtml(strategyLegSummary(candidate.legs))}</div></div>`,
    ]),
    "当前没有候选策略快照。"
  );
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
  const summary = reasonListText(candidate?.reason_codes, "");
  if (summary) return summary;
  if (candidate?.headline) return candidate.headline;
  return "当前没有额外说明";
}

function strategyLegSummary(legs) {
  if (!Array.isArray(legs) || !legs.length) return "当前没有附带腿说明";
  return legs
    .map((item) => `${readableState(item.product_type)} ${readableState(item.side)} ${item.symbol || "标的待确认"}`)
    .join(" | ");
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
    trial_guard_not_enabled: "试盘守护还没启用，所以当前不适合直接加资金。",
    trial_profile_not_active: "当前不在试盘档位，先不要做放量判断。",
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

function cooldownSummary(cooldowns) {
  if (!cooldowns || typeof cooldowns !== "object") return "当前没有冷却限制";
  const parts = Object.entries(cooldowns)
    .filter(([, value]) => Number(value) > 0)
    .map(([key, value]) => `${humanCooldownLabel(key)}：${formatDuration(value)}`);
  return parts.length ? parts.join(" | ") : "当前没有冷却限制";
}

function holdAge(value) {
  if (!value) return "持仓时长待确认";
  const date = new Date(String(value).replace("Z", "+00:00"));
  if (Number.isNaN(date.getTime())) return "持仓时长待确认";
  return formatDuration(Math.max((Date.now() - date.getTime()) / 1000, 0));
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
