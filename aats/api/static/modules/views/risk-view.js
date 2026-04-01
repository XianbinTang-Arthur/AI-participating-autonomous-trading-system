import { actionButton, pill, primaryStatusPanel, responsiveTable, summaryStrip, surfaceCard } from "../components.js";
import { kvList } from "../components.js";
import { localizeList, textOrFallback } from "../copy.js";
import { booleanWord, escapeHtml, formatMaybeTimestamp, formatNumber, formatRelativeAge, middleEllipsis } from "../formatters.js";
import {
  localizeError,
  operationalStatusCopy,
  operationalStatusHeadline,
  readableOverlayParentLegQuantitySummary,
  readableOverlayParentPostmortemMeta,
  readableOverlayParentSignalSummary,
  readableState,
  recoveryStatusLabel,
  reviewStatusLabel,
  toneForReconciliationSeverity,
  toneForRuntimeState,
  tradingStatusLabel,
} from "../terms.js";

export function renderRiskSections(data) {
  const account = data.accountState || {};
  const portfolio = data.portfolio?.portfolio || {};
  const blockerControl = data.blockerControl || {};
  const blockers = blockerControl.blockers || data.blockers?.blockers || [];
  const primaryBlocker = blockerControl.primary_blocker || blockers[0] || null;
  const secondaryBlockers = blockerControl.secondary_blockers || [];
  const primaryTask = blockerControl.primary_task || null;
  const reconciliation = data.reconciliationLatest?.reconciliation || null;
  const mismatchSummary = data.reconciliationLatest?.mismatch_summary || {};
  const legMismatchSummary = mismatchSummary.leg_mismatch_summary || {};
  const billsSummary = data.reconciliationLatest?.exchange_bills_summary || {};
  const positionsView = data.positions || {};
  const localInstrumentPositions = Array.isArray(positionsView.local_instrument_positions)
    ? positionsView.local_instrument_positions
    : [];
  const recovery = data.systemRecovery?.recovery || {};
  const replay = data.replayStatus || {};
  const metrics = data.metrics || {};
  const health = data.health || {};
  const uiHints = data.uiHints || {};
  const phase1Shadow = data.phase1Shadow || metrics.phase1_shadow || {};
  const trialGuard = data.trialGuard || data.runtime?.trial_guard || {};
  const marginBuffer = account.margin_buffer_overview || data.runtime?.margin_buffer_overview || {};
  const guardedLivePreflight = data.guardedLivePreflight || data.runtime?.guarded_live_preflight || {};
  const guardedLiveRunPacket = data.guardedLiveRunPacket || data.runtime?.guarded_live_run_packet_summary || {};
  const positionModeContract = account.position_mode_contract || {};
  const derivativesLiveGuard = account.derivatives_live_guard || {};
  const currentDerivativesExposure = derivativesLiveGuard.current_derivatives_exposure || {};
  const replayParentPostmortem = replay.last_validation?.overlay_parent_exposure_summary || null;
  const replayRecentValidations = Array.isArray(replay.recent_validations) ? replay.recent_validations : [];

  return {
    riskHero: primaryStatusPanel({
      eyebrow: "风险与恢复",
      headline: riskHeadline({ primaryBlocker, blockers, reconciliation, recovery }),
      summary: operationalStatusCopy({
        health,
        recovery,
        blockers: primaryBlocker ? [primaryBlocker] : blockers,
        reconciliation,
        recoveryReasonText: primaryTask?.summary || blockerControl.next_step_summary || uiHints.recoveryReasonsText,
        readyCopy: "当前没有硬阻断，可继续关注账户、对账和恢复状态。",
      }),
      tone: riskTone({ primaryBlocker, blockers, reconciliation, recovery, health }),
      actions: shouldShowInspectReconciliation({ reconciliation, recovery })
        ? actionButton("查看最新对账", "inspect-reconciliation", reconciliation.reconciliation_id, "ghost")
        : "",
      pills: [
        pill(`运行状态 ${readableState(health.runtime_state || health.overall_status || "unknown")}`, toneForRuntimeState(health.runtime_state || health.overall_status)),
        pill(`自动交易 ${tradingStatusLabel(recovery)}`, recovery.safe_to_trade ? "positive" : recovery.resume_eligible ? "warning" : "danger"),
        pill(`人工复核 ${reviewStatusLabel(recovery.review_required)}`, recovery.review_required ? "warning" : "outline"),
      ],
      metrics: [
        {
          label: "当前阻断数",
          value: formatNumber(blockers.length, 0),
          meta: primaryTask
            ? textOrFallback(primaryTask.title, "当前没有额外主任务")
            : noPrimaryBlockerSummary({ recovery, reconciliation }).meta,
          tone: blockers.length > 0 ? "danger" : "positive",
        },
        {
          label: "最新对账",
          value: readableState(reconciliation?.severity || "unknown"),
          meta: middleEllipsis(reconciliation?.reconciliation_id, 10, 6, "当前暂无最新对账编号"),
          tone: reconciliation?.halt_required ? "danger" : toneForReconciliationSeverity(reconciliation?.severity),
        },
        {
          label: "恢复状态",
          value: recoveryStatusLabel(recovery),
          meta: recovery.halted && recovery.resume_eligible
            ? "系统已手动暂停，确认无误后可直接恢复自动运行。"
            : primaryTask?.summary || blockerControl.next_step_summary || uiHints.recoveryReasonsText || listText(recovery.resume_blocked_reasons, "当前没有额外恢复说明"),
          tone: recovery.safe_to_trade ? "positive" : recovery.resume_eligible ? "warning" : recovery.review_required ? "warning" : "danger",
        },
        {
          label: "账户快照",
          value: booleanWord(account.fresh),
          meta: formatMaybeTimestamp(account.last_refresh_timestamp),
          tone: account.fresh ? "positive" : "warning",
        },
      ],
    }),
    riskActions: surfaceCard({
      title: "你现在先做什么",
      kicker: "当前主任务",
      copy: primaryTask?.summary || blockerControl.next_step_summary || reconciliationActionCopy({ reconciliation, recovery }),
      content: renderPrimaryTaskPanel({ primaryTask, recovery, reconciliation, uiHints }),
    }),
    riskEvidence: surfaceCard({
      title: "状态依据",
      kicker: "判断依据",
      copy: "把最影响自动交易资格的三条证据放在同一处，减少来回跳读。",
      content: summaryStrip([
        {
          label: "当前主任务",
          value: primaryTask ? textOrFallback(primaryTask.title, "当前没有额外主任务") : "当前没有额外主任务",
          meta: primaryTask ? textOrFallback(primaryTask.summary, "当前没有额外处理建议") : "当前暂无需要立刻处理的动作",
          tone: primaryTask && primaryTask.kind === "resolve_blocker" ? "danger" : primaryTask ? "warning" : "positive",
        },
        {
          label: "最新对账",
          value: readableState(reconciliation?.severity || "unknown"),
          meta: reconciliation?.halt_required ? "最新对账已要求暂停交易" : middleEllipsis(reconciliation?.reconciliation_id, 10, 6, "当前没有对账结论"),
          tone: reconciliation?.halt_required ? "danger" : toneForReconciliationSeverity(reconciliation?.severity),
        },
        {
          label: "恢复资格",
          value: booleanWord(recovery.resume_eligible),
          meta: uiHints.recoveryReasonsText || listText(recovery.resume_blocked_reasons, "当前没有额外恢复限制说明"),
          tone: recovery.resume_eligible ? "positive" : "warning",
        },
      ]),
    }),
    riskAccount: surfaceCard({
      title: "账户概览",
      kicker: "资金状态",
      copy: "这里主要看账户是否可信，不把账户状态和阻断原因混在一起。",
      content: summaryStrip([
        { label: "总权益", value: formatNumber(portfolio.total_equity), meta: "账户当前总价值", tone: "info" },
        { label: "已实现收益", value: formatNumber(portfolio.realized_pnl), meta: "已经确认的盈亏", tone: Number(portfolio.realized_pnl || 0) >= 0 ? "positive" : "warning" },
        { label: "未实现收益", value: formatNumber(portfolio.unrealized_pnl), meta: "持仓浮动盈亏", tone: Number(portfolio.unrealized_pnl || 0) >= 0 ? "positive" : "warning" },
        { label: "保证金占用", value: formatNumber(portfolio.margin_usage), meta: `总敞口 ${formatNumber(portfolio.gross_exposure)}`, tone: Number(portfolio.margin_usage || 0) > 0 ? "warning" : "neutral" },
      ]),
    }),
    riskRecovery: surfaceCard({
      title: "恢复概览",
      kicker: "恢复状态",
      copy: "恢复和回放共同决定系统在异常后还能不能继续被信任。",
      content: summaryStrip([
        { label: "恢复状态", value: recoveryStatusLabel(recovery), meta: recovery.safe_to_trade ? "当前允许继续自动运行" : recovery.halted && recovery.resume_eligible ? "当前处于手动暂停，可在确认后恢复自动运行" : "当前不允许继续自动运行", tone: recovery.safe_to_trade ? "positive" : recovery.resume_eligible ? "warning" : recovery.review_required ? "warning" : "danger" },
        { label: "人工复核", value: reviewStatusLabel(recovery.review_required), meta: recovery.rebaseline_available ? "允许重新确认基线" : "当前不允许重建基线", tone: recovery.review_required ? "warning" : "positive" },
        { label: "回放健康度", value: booleanWord(replay.healthy), meta: textOrFallback(replay.last_validation?.decision_id, "最近没有回放验证"), tone: replay.healthy ? "positive" : "warning" },
        { label: "最近回放时间", value: formatMaybeTimestamp(replay.last_validation?.validated_at), meta: formatRelativeAge(replay.last_validation?.validated_at), tone: replay.last_validation?.validated_at ? "info" : "neutral" },
      ]),
    }),
    riskReplayPostmortem: replayParentPostmortem
      ? surfaceCard({
          title: "回放父腿复盘",
          kicker: "Replay Postmortem",
          copy: "把最近一次回放里的父腿暴露阶段单独收口，便于核对 residual inventory、target-only 和 mixed source。",
          content: kvList(replayOverlayParentPostmortemRows(replayParentPostmortem)),
        })
      : "",
    riskReplayHistory: replayRecentValidations.length
      ? surfaceCard({
          title: "回放父腿历史",
          kicker: "Replay History",
          copy: "把最近几次 replay 里的父腿暴露阶段并排展开，方便比较 residual inventory、mixed source 和目标切换场景。",
          actions: `<div class="stack-actions table-actions--compact">${actionButton("查看 Replay 工作区", "navigate-view", "replay", "ghost")}</div>`,
          content: renderReplayOverlayParentHistory(replayRecentValidations),
        })
      : "",
    riskMarginBuffer: surfaceCard({
      title: "保证金缓冲",
      kicker: "强平风险",
      copy: marginBuffer.summary || "这里同时展示当前真实保证金缓冲和下一笔投影风险。",
      content: summaryStrip([
        {
          label: "当前状态",
          value: readableState(marginBuffer.status || "unknown"),
          meta: marginBuffer.summary || "当前没有额外保证金风险说明",
          tone: marginBufferTone(marginBuffer.status),
        },
        {
          label: "当前保证金占用",
          value: trialRatioText(marginBuffer.current?.initial_margin_usage_fraction),
          meta: `距离 only-reduce ${trialRatioText(marginBuffer.current?.buffer_to_only_reduce)}，距离硬上限 ${trialRatioText(marginBuffer.current?.buffer_to_hard_limit)}`,
          tone: marginBufferTone(marginBuffer.status),
        },
        {
          label: "下一笔投影占用",
          value: trialRatioText(marginBuffer.projected?.projected_margin_usage),
          meta: `投影后距离 only-reduce ${trialRatioText(marginBuffer.projected?.buffer_to_only_reduce)}，距离硬上限 ${trialRatioText(marginBuffer.projected?.buffer_to_hard_limit)}`,
          tone: marginBufferTone(marginBuffer.status),
        },
        {
          label: "最近强平距离",
          value: trialRatioText(marginBuffer.liquidation?.nearest_liquidation_gap_ratio),
          meta: marginBuffer.liquidation?.closest_position
            ? `${textOrFallback(marginBuffer.liquidation.closest_position.symbol, "未知合约")} / ${textOrFallback(marginBuffer.liquidation.closest_position.pos_side, "未知方向")}，强平价 ${formatNumber(marginBuffer.liquidation.closest_position.liquidation_price)}`
            : "当前没有可计算强平距离的仓位",
          tone: marginBufferTone(marginBuffer.status),
        },
      ]),
    }),
    riskPositionMode: surfaceCard({
      title: "持仓模式契约",
      kicker: "对冲模式",
      copy: "这里明确说明本地合约运行线要求的仓位模式，以及交易所当前真实返回的 posMode。",
      content: summaryStrip([
        {
          label: "本地要求",
          value: derivativesPositionModeLabel(positionModeContract.configured_derivatives_position_mode),
          meta: requiredExchangeModeMeta(positionModeContract),
          tone: positionModeContract.exchange_position_mode_matches_configured === false ? "danger" : "info",
        },
        {
          label: "交易所当前模式",
          value: exchangePositionModeLabel(positionModeContract.exchange_position_mode),
          meta: positionModeContract.position_mode_match_required
            ? `强匹配 ${booleanWord(positionModeContract.exchange_position_mode_matches_configured)}`
            : "当前没有强制要求和交易所模式一致",
          tone: positionModeContract.exchange_position_mode_matches_configured === false ? "danger" : "positive",
        },
        {
          label: "本地双腿持仓",
          value: Number(localInstrumentPositions.filter((item) => item.dual_legged).length || 0) > 0
            ? `${formatNumber(localInstrumentPositions.filter((item) => item.dual_legged).length || 0, 0)} 个标的`
            : "当前没有双腿并存标的",
          meta: localInstrumentLegMeta(localInstrumentPositions),
          tone: Number(localInstrumentPositions.filter((item) => item.dual_legged).length || 0) > 0 ? "info" : "neutral",
        },
        {
          label: "恢复上下文",
          value: recovery.safe_to_trade ? "当前没有模式阻断" : "恢复资格仍受限",
          meta: Number(legMismatchSummary.total_count || 0) > 0 ? "恢复判断会继续结合腿级异常一起评估。" : "当前没有额外腿级异常进入恢复判断。",
          tone: recovery.safe_to_trade ? "positive" : "warning",
        },
      ]),
    }),
    riskExposure: surfaceCard({
      title: "合约敞口",
      kicker: "long / short / gross / net",
      copy: "对冲模式下不能只看净敞口，这里同时展开 long、short、gross、net 四口径。",
      content: summaryStrip([
        {
          label: "多头名义价值",
          value: formatNumber(currentDerivativesExposure.long_notional),
          meta: `多头杠杆 ${formatNumber(currentDerivativesExposure.long_leverage, 2)}`,
          tone: Number(currentDerivativesExposure.long_notional || 0) > 0 ? "info" : "neutral",
        },
        {
          label: "空头名义价值",
          value: formatNumber(currentDerivativesExposure.short_notional),
          meta: `空头杠杆 ${formatNumber(currentDerivativesExposure.short_leverage, 2)}`,
          tone: Number(currentDerivativesExposure.short_notional || 0) > 0 ? "info" : "neutral",
        },
        {
          label: "毛敞口",
          value: formatNumber(currentDerivativesExposure.gross_notional),
          meta: `毛杠杆 ${formatNumber(currentDerivativesExposure.gross_leverage, 2)}`,
          tone: Number(currentDerivativesExposure.gross_notional || 0) > 0 ? "warning" : "neutral",
        },
        {
          label: "净敞口",
          value: formatNumber(currentDerivativesExposure.net_notional),
          meta: `净杠杆 ${formatNumber(currentDerivativesExposure.net_leverage, 2)}`,
          tone: Number(currentDerivativesExposure.net_notional || 0) === 0 ? "neutral" : "info",
        },
      ]),
    }),
    riskPreflight: surfaceCard({
      title: "启盘前自检",
      kicker: "guarded_live 预检",
      copy: guardedLivePreflight.summary || "这里收口合约 guarded_live 启盘前必须人工确认的结构化检查项。",
      content: summaryStrip([
        {
          label: "当前结论",
          value: readableState(guardedLivePreflight.status || "unknown"),
          meta: guardedLivePreflight.summary || "当前没有额外预检说明",
          tone: preflightTone(guardedLivePreflight.status),
        },
        {
          label: "通过 / 告警 / 失败",
          value: `${formatNumber(guardedLivePreflight.counts?.pass || 0, 0)} / ${formatNumber(guardedLivePreflight.counts?.warn || 0, 0)} / ${formatNumber(guardedLivePreflight.counts?.fail || 0, 0)}`,
          meta: `启盘资格 ${booleanWord(guardedLivePreflight.launch_ready)}`,
          tone: preflightTone(guardedLivePreflight.status),
        },
        {
          label: "真实资金报单路径",
          value: textOrFallback(guardedLivePreflight.checks?.find((item) => item.check_id === "real_money_route_ready")?.status, "未知"),
          meta: textOrFallback(guardedLivePreflight.checks?.find((item) => item.check_id === "real_money_route_ready")?.detail, "当前没有额外线路说明"),
          tone: preflightTone(guardedLivePreflight.checks?.find((item) => item.check_id === "real_money_route_ready")?.status),
        },
        {
          label: "下一步",
          value: textOrFallback((guardedLivePreflight.operator_actions || [])[0], "当前没有额外预检动作"),
          meta: textOrFallback((guardedLivePreflight.operator_actions || [])[1], "请继续保持小资金和人工盯盘。"),
          tone: guardedLivePreflight.launch_ready ? "positive" : "warning",
        },
      ]),
    }),
    riskRunPacket: surfaceCard({
      title: "小资金运行包",
      kicker: "运行摘要",
      copy: guardedLiveRunPacket.summary || "这里把试盘守护、保证金风险、恢复状态和当前敞口收成一张运行包。",
      content: summaryStrip([
        {
          label: "当前状态",
          value: readableState(guardedLiveRunPacket.status || "unknown"),
          meta: guardedLiveRunPacket.summary || "当前没有额外运行包说明",
          tone: packetTone(guardedLiveRunPacket.status),
        },
        {
          label: "综合净收益",
          value: formatNumber(guardedLiveRunPacket.summary_metrics?.combined_net_realized_pnl),
          meta: `资金费 ${formatNumber(guardedLiveRunPacket.summary_metrics?.funding_fee_net_pnl)}，活动阻断 ${formatNumber(guardedLiveRunPacket.summary_metrics?.execution_blocker_count || 0, 0)} 个`,
          tone: Number(guardedLiveRunPacket.summary_metrics?.combined_net_realized_pnl || 0) >= 0 ? "positive" : "warning",
        },
        {
          label: "保证金 / 强平距离",
          value: trialRatioText(guardedLiveRunPacket.summary_metrics?.current_initial_margin_usage_fraction),
          meta: `最近强平距离 ${trialRatioText(guardedLiveRunPacket.summary_metrics?.nearest_liquidation_gap_ratio)}，持仓 ${formatNumber(guardedLiveRunPacket.summary_metrics?.open_position_count || 0, 0)} 条`,
          tone: packetTone(guardedLiveRunPacket.status),
        },
        {
          label: "人工动作",
          value: textOrFallback((guardedLiveRunPacket.operator_actions || [])[0], "当前没有额外运行包动作"),
          meta: textOrFallback((guardedLiveRunPacket.operator_actions || [])[1], "继续保持受控运行并关注风险页。"),
          tone: packetTone(guardedLiveRunPacket.status),
        },
      ]),
    }),
    riskShadow: surfaceCard({
      title: "影子兼容层",
      kicker: "Phase 1 兼容状态",
      copy: "这里单独看新旧执行链的兼容层是否追平，避免把恢复判断建立在半同步的数据上。",
      actions: actionButton("查看影子详情", "inspect-shadow", "", "ghost"),
      content: summaryStrip([
        {
          label: "当前状态",
          value: phase1ShadowLabel(phase1Shadow.status),
          meta: phase1Shadow.summary || "当前没有额外兼容层说明",
          tone: phase1ShadowTone(phase1Shadow.status),
        },
        {
          label: "订单 / 成交积压",
          value: `${backlogText(phase1Shadow.lag?.order_backlog)} / ${backlogText(phase1Shadow.lag?.fill_backlog)}`,
          meta: `保留金积压 ${backlogText(phase1Shadow.lag?.obligation_backlog)}`,
          tone: phase1ShadowHasBacklog(phase1Shadow) ? "warning" : "positive",
        },
        {
          label: "最近人工核查",
          value: phase1Shadow.latest_review_action ? "已记录" : "尚未记录",
          meta: phase1ShadowReviewMeta(phase1Shadow.latest_review_action),
          tone: phase1Shadow.latest_review_action ? "info" : "neutral",
        },
        {
          label: "最近兼容层错误",
          value: phase1ShadowLastError(phase1Shadow),
          meta: formatMaybeTimestamp(
            phase1Shadow.execution_shadow?.last_failure_ts
            || phase1Shadow.ledger_shadow?.last_failure_ts
          ),
          tone: phase1Shadow.status === "degraded" ? "danger" : "neutral",
        },
      ]),
    }),
    riskTrialGuard: surfaceCard({
      title: "试盘守护",
      kicker: "小资金前向验证",
      copy: "这里专门看小资金试盘的自动停机阈值，避免在还没证明策略有效前先放大亏损。",
      content: summaryStrip([
        {
          label: "当前状态",
          value: trialGuardStatusLabel(trialGuard.status),
          meta: trialGuard.summary || "当前没有额外试盘守护说明",
          tone: trialGuardTone(trialGuard.status),
        },
        {
          label: "样本量",
          value: formatNumber(trialGuard.fill_count, 0),
          meta: `最少需要 ${formatNumber(trialGuard.min_closed_fills, 0)} 笔已完成成交后才会触发自动停机判断`,
          tone: Number(trialGuard.fill_count || 0) >= Number(trialGuard.min_closed_fills || 0) ? "positive" : "warning",
        },
        {
          label: "最近 24 小时综合净收益",
          value: formatNumber(trialGuard.daily_combined_net_realized ?? trialGuard.daily_net_realized),
          meta: `交易净收益 ${formatNumber(trialGuard.daily_trading_net_realized)}，资金费 ${formatNumber(trialGuard.daily_funding_fee_net)}，连续亏损 ${formatNumber(trialGuard.consecutive_losses, 0)} 笔`,
          tone: Number((trialGuard.daily_combined_net_realized ?? trialGuard.daily_net_realized) || 0) >= 0 ? "positive" : "warning",
        },
        {
          label: "费用 / 成交额",
          value: trialRatioText(trialGuard.fee_to_notional_ratio),
          meta: `高滑点比例 ${trialRatioText(trialGuard.high_slippage_ratio)}，慢成交比例 ${trialRatioText(trialGuard.slow_submit_to_fill_ratio)}`,
          tone: (trialGuard.breaches || []).length ? "danger" : "info",
        },
      ]),
    }),
    riskBlockers: surfaceCard({
      title: "当前阻断明细",
      kicker: "阻断状态",
      copy: blockers.length ? "下面这些阻断项正在直接影响交易资格。" : "当前没有阻断项，说明系统没有被明确拦停。",
      content: renderBlockerControlList({ blockers, primaryBlocker, uiHints }),
    }),
    riskMetrics: surfaceCard({
      title: "风险指标",
      kicker: "核心指标",
      copy: "这些指标用来判断系统是不是开始偏离可被信任的状态。",
      classes: "is-muted",
      content: summaryStrip([
        { label: "拒单数", value: formatNumber(metrics.rejection_count, 0), meta: "偏高时要检查门禁和执行条件", tone: Number(metrics.rejection_count || 0) > 0 ? "warning" : "positive" },
        { label: "活动委托数", value: formatNumber(metrics.current_open_order_count, 0), meta: "需要和阻断项、保留额度一起看", tone: Number(metrics.current_open_order_count || 0) > 0 ? "warning" : "positive" },
        { label: "累计异常对账", value: formatNumber(metrics.reconciliation_mismatch_count, 0), meta: "累计出现过的非一致对账次数，不等于当前待处理数量", tone: Number(metrics.reconciliation_mismatch_count || 0) > 0 ? "warning" : "positive" },
        { label: "账户快照", value: booleanWord(account.ready), meta: listText(account.blockers, "当前没有额外账户阻断说明"), tone: account.ready ? "positive" : "warning" },
      ]),
    }),
    riskReconciliation: surfaceCard({
      title: "对账概览",
      kicker: "对账上下文",
      copy: "这里保留最近一次对账的关键上下文，不再和首屏裁决抢主次。",
      classes: "is-muted",
      content: summaryStrip([
        { label: "对账级别", value: readableState(reconciliation?.severity || "unknown"), meta: middleEllipsis(reconciliation?.reconciliation_id, 10, 6, "当前暂无对账编号"), tone: reconciliation?.halt_required ? "danger" : toneForReconciliationSeverity(reconciliation?.severity) },
        { label: "是否要求停机", value: booleanWord(reconciliation?.halt_required), meta: reconciliation?.exchange_comparison_enabled ? "已比对交易所" : "仅校验本地记录", tone: reconciliation?.halt_required ? "danger" : "positive" },
        {
          label: "差异分层",
          value: mismatchSummary.finding_summary
            ? `${formatNumber(mismatchSummary.finding_summary.structural_count || 0, 0)} / ${formatNumber(mismatchSummary.finding_summary.financial_count || 0, 0)} / ${formatNumber(mismatchSummary.finding_summary.observational_count || 0, 0)}`
            : "当前没有差异条目",
          meta: mismatchSummary.observational_only
            ? "当前只有动态观察值漂移，不需要立即人工确认。"
            : "顺序为：结构性 / 财务 / 观察值。",
          tone: mismatchSummary.observational_only ? "info" : mismatchSummary.mismatch_reasons?.length ? "warning" : "positive",
        },
        {
          label: "持仓腿异常",
          value: Number(legMismatchSummary.total_count || 0) > 0 ? `${formatNumber(legMismatchSummary.total_count || 0, 0)} 条` : "当前没有腿级异常",
          meta: legMismatchSummaryMeta(legMismatchSummary),
          tone: legMismatchTone(legMismatchSummary),
        },
        { label: "建议动作", value: mismatchSummary.recommended_operator_action ? localizeError(mismatchSummary.recommended_operator_action) : "当前没有额外建议动作", meta: listText(mismatchSummary.safety_impacts, "当前没有额外安全影响说明"), tone: mismatchSummary.recommended_operator_action ? "info" : "neutral" },
      ]),
    }),
    riskBills: surfaceCard({
      title: "账单概览",
      kicker: "账单证据",
      copy: billsSummary.available ? "最近账单已缓存，可作为交易所侧余额与对账辅助证据。" : "当前暂无最新账单摘要缓存。",
      classes: "is-muted",
      content: summaryStrip([
        { label: "账单数量", value: formatNumber(billsSummary.count || 0, 0), meta: textOrFallback(billsSummary.latest_bill_id, "当前暂无最新账单编号"), tone: Number(billsSummary.count || 0) > 0 ? "info" : "neutral" },
        { label: "最新账单时间", value: formatMaybeTimestamp(billsSummary.latest_bill_ts), meta: formatRelativeAge(billsSummary.latest_bill_ts), tone: billsSummary.latest_bill_ts ? "info" : "neutral" },
        { label: "涉及币种", value: listText(billsSummary.currencies, "当前没有账单币种摘要"), meta: "最近交易所侧账务变动范围", tone: (billsSummary.currencies || []).length ? "warning" : "neutral" },
        { label: "高频账单类别", value: renderBillCategories(billsSummary.top_categories), meta: billsSummary.last_error || "已按类型、子类型和币种聚合", tone: billsSummary.last_error ? "warning" : "positive" },
      ]),
    }),
  };
}

export function renderRiskView(data) {
  const sections = renderRiskSections(data);
  return `
    <div class="panel-grid">
      <div class="span-12">${sections.riskHero}</div>
      <div class="span-12">${sections.riskActions}</div>
      <div class="span-12">${sections.riskEvidence}</div>
      <div class="span-3">${sections.riskAccount}</div>
      <div class="span-3">${sections.riskPositionMode}</div>
      <div class="span-3">${sections.riskMarginBuffer}</div>
      <div class="span-3">${sections.riskReconciliation}</div>
      <div class="span-6">${sections.riskRecovery}</div>
      ${sections.riskReplayPostmortem ? `<div class="span-6">${sections.riskReplayPostmortem}</div>` : ""}
      ${sections.riskReplayHistory ? `<div class="span-12">${sections.riskReplayHistory}</div>` : ""}
      <div class="span-6">${sections.riskExposure}</div>
      <div class="span-6">${sections.riskPreflight}</div>
      <div class="span-6">${sections.riskRunPacket}</div>
      <div class="span-12">${sections.riskShadow}</div>
      <div class="span-12">${sections.riskTrialGuard}</div>
      <div class="span-12">${sections.riskBills}</div>
      <div class="span-6">${sections.riskBlockers}</div>
      <div class="span-6">${sections.riskMetrics}</div>
    </div>
  `;
}

function replayOverlayParentPostmortemRows(summary = {}) {
  return [
    [
      "父腿阶段",
      readableOverlayParentSignalSummary(summary, "当前没有额外父腿阶段说明"),
      readableOverlayParentPostmortemMeta(summary, "当前没有额外父腿契约说明"),
    ],
    [
      "双腿数量拆解",
      readableOverlayParentLegQuantitySummary(summary, "当前没有父腿多空数量拆解"),
      `来源 ${textOrFallback(localizeError(summary.signal_source), "当前没有额外来源说明")}`,
    ],
  ];
}

function renderReplayOverlayParentHistory(validations = []) {
  const headers = ["回放时间 / 决策", "父腿阶段", "契约口径", "双腿数量拆解"];
  const rows = validations.map((validation) => {
    const summary = validation?.overlay_parent_exposure_summary || {};
    return [
      `<strong>${escapeHtml(formatMaybeTimestamp(validation?.validated_at))}</strong><div class="table-meta">${escapeHtml(textOrFallback(validation?.decision_id, "当前没有决策编号"))}</div>`,
      escapeHtml(readableOverlayParentSignalSummary(summary, "当前没有父腿阶段说明")),
      escapeHtml(readableOverlayParentPostmortemMeta(summary, "当前没有额外父腿契约说明")),
      escapeHtml(readableOverlayParentLegQuantitySummary(summary, "当前没有父腿多空数量拆解")),
    ];
  });
  const cards = validations.map((validation) => {
    const summary = validation?.overlay_parent_exposure_summary || {};
    return {
      kicker: formatMaybeTimestamp(validation?.validated_at),
      title: textOrFallback(validation?.decision_id, "当前没有决策编号"),
      meta: readableOverlayParentPostmortemMeta(summary, "当前没有额外父腿契约说明"),
      fields: [
        {
          label: "父腿阶段",
          value: readableOverlayParentSignalSummary(summary, "当前没有父腿阶段说明"),
        },
        {
          label: "双腿数量拆解",
          value: readableOverlayParentLegQuantitySummary(summary, "当前没有父腿多空数量拆解"),
        },
      ],
      details: [
        {
          label: "健康度",
          value: booleanWord(validation?.healthy),
          meta: `偏差 ${formatNumber(validation?.divergence_count ?? 0, 0)} / 链路分数 ${formatNumber(validation?.chain_health_score ?? 0, 3)}`,
        },
      ],
    };
  });
  return responsiveTable(headers, rows, "当前没有回放父腿历史。", cards);
}

function trialGuardStatusLabel(status) {
  if (status === "disabled" || status === "not_configured") return "未启用";
  if (status === "inactive_for_runtime") return "当前运行线未启用";
  if (status === "warming_up") return "预热中";
  if (status === "breached") return "已触发暂停";
  if (status === "monitoring") return "监控中";
  if (status === "recovered") return "已恢复";
  return textOrFallback(status, "未知状态");
}

function trialGuardTone(status) {
  if (status === "breached") return "danger";
  if (status === "inactive_for_runtime") return "neutral";
  if (status === "warming_up") return "warning";
  if (status === "monitoring") return "positive";
  if (status === "recovered") return "positive";
  return "neutral";
}

function marginBufferTone(status) {
  if (status === "critical") return "danger";
  if (status === "warning") return "warning";
  if (status === "healthy") return "positive";
  return "neutral";
}

function preflightTone(status) {
  if (status === "pass") return "positive";
  if (status === "fail") return "danger";
  if (status === "warning") return "warning";
  if (status === "ready") return "positive";
  return "neutral";
}

function packetTone(status) {
  if (status === "critical") return "danger";
  if (status === "warning") return "warning";
  if (status === "ready") return "positive";
  return "neutral";
}

function trialRatioText(value) {
  if (value === null || value === undefined || value === "") return "暂无";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "暂无";
  return `${formatNumber(numeric * 100, 2)}%`;
}

export function renderReconciliationControls({
  reconciliation = null,
  recovery = {},
  uiHints = {},
  includeInspect = false,
  compact = false,
} = {}) {
  const permissionMessage = textOrFallback(uiHints.controlPermissionMessage, "");
  const canWrite = !permissionMessage;
  const buttons = [];
  if (includeInspect && shouldShowInspectReconciliation({ reconciliation, recovery })) {
    buttons.push(actionButton("查看对账", "inspect-reconciliation", reconciliation.reconciliation_id, "ghost"));
  }
  if (shouldShowValidateAction({ reconciliation, recovery })) {
    buttons.push(
      actionButton("重新对账（刷新交易所状态）", "trigger-reconciliation-validate", "", "secondary", {
        disabled: !canWrite,
        title: permissionMessage,
      })
    );
  }
  if (shouldShowRebaselineAction({ reconciliation, recovery })) {
    buttons.push(
      actionButton("接受当前状态为新基线", "trigger-rebaseline", "", "warning", {
        disabled: !canWrite,
        title: permissionMessage,
      })
    );
  }
  if (shouldShowResumeAction({ recovery })) {
    buttons.push(
      actionButton("恢复自动运行", "trigger-resume", "", "warning", {
        disabled: !canWrite || !recovery.resume_eligible,
        title: !canWrite ? permissionMessage : resumeActionHint({ recovery, uiHints }),
      })
    );
  }
  if (!buttons.length) return `<p class="meta-copy">${reconciliationActionCopy({ reconciliation, recovery })}</p>`;
  return `<div class="stack-actions ${compact ? "table-actions--compact" : ""}">${buttons.join("")}</div>`;
}

export function reconciliationActionCopy({ reconciliation = null, recovery = {}, isHistorical = false } = {}) {
  if (isHistorical) {
    return "这是历史对账记录。下面的操作会作用于当前运行态，请先确认最新对账结论是否仍然一致。";
  }
  if (reconciliation?.halt_required) {
    return "当前需先完成对账。请先核对差异原因；确认交易所当前状态才是正确状态后，再接受为新基线。";
  }
  if (reconciliation?.observational_only && !recovery.review_required) {
    return "当前只有轻度动态漂移，例如保证金或浮盈随行情波动。系统可继续运行，建议持续观察，不需要立即重设基线。";
  }
  if (reconciliation?.review_required || shouldShowRebaselineAction({ reconciliation, recovery })) {
    return "当前处于待人工确认状态。请先重新对账或核对交易所账单，确认状态符合预期后再接受为新基线。";
  }
  if (recovery.halted && recovery.resume_eligible) {
    return operationalStatusCopy({ recovery });
  }
  if (!recovery.safe_to_trade) {
    return operationalStatusCopy({ recovery });
  }
  return "当前状态稳定。如果想再次确认状态，可以手动重新对账（刷新交易所状态）。";
}

function renderPrimaryTaskPanel({ primaryTask = null, recovery = {}, reconciliation = null, uiHints = {} } = {}) {
  if (!primaryTask) {
    const summary = noPrimaryBlockerSummary({ recovery, reconciliation });
    return `
      ${summaryStrip([
        {
          label: "当前状态",
          value: summary.value,
          meta: summary.meta,
          tone: summary.tone,
        },
      ])}
      <p class="meta-copy">${escapeHtml(summary.copy)}</p>
      ${renderReconciliationControls({ reconciliation, recovery, uiHints, includeInspect: true })}
    `;
  }
  return `
    ${summaryStrip([
      {
        label: "当前主任务",
        value: textOrFallback(primaryTask.title, "当前没有额外主任务"),
        meta: primaryTask.kind === "resolve_blocker" ? "请先完成这一项，再看是否还剩其他阻断。" : "按这一步处理后，系统会重新评估恢复资格。",
        tone: primaryTask.kind === "resolve_blocker" ? "danger" : primaryTask.kind === "observe" || primaryTask.kind === "healthy" ? "info" : "warning",
      },
      {
        label: "为什么先做这一步",
        value: textOrFallback(primaryTask.reason, "当前没有额外原因说明。"),
        meta: primaryTask.kind === "observe" ? "当前以观察为主，不需要立即做高风险人工操作。" : "这一步是当前最直接影响系统状态的处理动作。",
        tone: primaryTask.kind === "observe" || primaryTask.kind === "healthy" ? "info" : "warning",
      },
      {
        label: "做完后会怎样",
        value: textOrFallback(primaryTask.completion_outcome, "处理完成后系统会重新评估当前状态。"),
        meta: primaryTask.secondary_blocker_count > 0 ? `后面还剩 ${formatNumber(primaryTask.secondary_blocker_count, 0)} 条次级阻断。` : "处理完成后即可重新评估是否恢复自动运行。",
        tone: "info",
      },
    ])}
    ${kvList([
      ["先做什么", textOrFallback(primaryTask.summary, "当前没有额外处理建议。"), primaryTask.kind === "observe" ? "这一栏说的是“现在最推荐做的动作”，不是系统内部状态描述。" : "优先按这一步处理，不要先去点无关按钮。"] ,
      ["来源", primaryTask.source_blocker ? localizeError(primaryTask.source_blocker) : "当前没有单独的上游阻断代码", primaryTask.source_blocker ? `阻断代码：${primaryTask.source_blocker}` : "这是系统按当前恢复状态综合给出的主任务。"] ,
    ])}
    ${renderBlockerActions(primaryTask.actions || [], primaryTask.source_blocker || "", uiHints)}
  `;
}

function renderBlockerControlList({ blockers = [], primaryBlocker = null, uiHints = {} } = {}) {
  if (!blockers.length) {
    return `<p class="meta-copy">当前暂无阻断项。</p>`;
  }
  return `
    <div class="alert-list">
      ${blockers.map((item) => `
        <article class="timeline-item">
          <div class="panel-head">
            <div>
              <strong>${escapeHtml(textOrFallback(item.title, localizeError(item.blocker)))}</strong>
              <p class="meta-copy">来源：${escapeHtml(readableState(item.subsystem || "system"))}</p>
            </div>
            <div class="inline-pills">
              ${item.root_cause ? pill("当前先处理", "danger") : ""}
              ${item.submit_only ? pill("仅影响报单", "warning") : pill("影响自动运行", "danger")}
            </div>
          </div>
          <p>${escapeHtml(textOrFallback(item.description, localizeError(item.blocker)))}</p>
          <p class="meta-copy">${escapeHtml(textOrFallback(item.recommended_next_step, "当前没有额外处理建议。"))}</p>
          ${renderBlockerActions(item.actions || [], item.blocker, uiHints)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderBlockerActions(actions = [], blocker = "", uiHints = {}) {
  if (!actions.length) return "";
  const permissionMessage = textOrFallback(uiHints.controlPermissionMessage, "");
  const rendered = actions.flatMap((action) => {
    if (action.kind === "client" && action.client_action === "navigate-view" && action.value === "risk") {
      return [];
    }
    const isApi = action.kind !== "client";
    const disabledReason = isApi ? permissionMessage || action.disabled_reason : action.disabled_reason;
    const disabled = Boolean((isApi && permissionMessage) || action.enabled === false);
    if (action.kind === "client") {
      return [actionButton(
        action.label,
        textOrFallback(action.client_action, "refresh-dashboard"),
        textOrFallback(action.value, ""),
        action.tone || "ghost",
        {
          disabled,
          title: disabledReason || action.expected_effect || "",
        },
      )];
    }
    return [actionButton(
      action.label,
      "trigger-blocker-action",
      `${action.action_id}::${blocker}`,
      action.tone || "secondary",
      {
        disabled,
        title: disabledReason || action.expected_effect || "",
      },
    )];
  });
  if (!rendered.length) return "";
  return `<div class="stack-actions">${rendered.join("")}</div>`;
}

function renderBillCategories(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return "当前没有账单分类";
  return rows
    .slice(0, 3)
    .map((item) => `${item.type}/${item.sub_type}/${item.currency} x${formatNumber(item.count, 0)}`)
    .join(" | ");
}

function noPrimaryBlockerSummary({ recovery = {}, reconciliation = null } = {}) {
  if (recovery.safe_to_trade && reconciliation?.observational_only) {
    return {
      value: "轻度差异，建议观察",
      meta: "当前没有新的主阻断。最新对账只有保证金、浮盈或仓位观察值的动态漂移。",
      tone: "info",
      copy: "当前没有新的第一优先级阻断。最新对账只有轻度动态漂移，系统可继续运行，建议持续观察，不需要立即重设基线。",
    };
  }
  if (recovery.review_required) {
    return {
      value: "仍需人工确认",
      meta: "当前没有新的主阻断，但恢复状态仍要求人工确认。这通常表示最近有未完全收敛的对账或恢复事件。",
      tone: "warning",
      copy: "当前没有新的第一优先级阻断，但系统仍处于人工确认流程。请优先查看最新对账、恢复状态和交易所账单，确认是否还有未收敛的复核条件。",
    };
  }
  if (recovery.halted && recovery.resume_eligible) {
    return {
      value: "手动暂停，待恢复",
      meta: "当前没有新的主阻断。系统处于手动暂停状态，确认无误后可以恢复自动运行。",
      tone: "warning",
      copy: "当前没有新的第一优先级阻断。系统处于手动暂停状态，确认最新对账和账户快照无误后即可恢复自动运行。",
    };
  }
  if (!recovery.safe_to_trade) {
    return {
      value: "仍未满足恢复条件",
      meta: "当前没有新的主阻断，但系统仍未恢复到可自动运行状态。请继续查看恢复状态和恢复受限原因。",
      tone: "warning",
      copy: "当前没有新的第一优先级阻断，但系统仍未满足恢复条件。请先查看恢复状态中的限制原因，再判断是否可以继续运行。",
    };
  }
  return {
      value: "当前可继续自动运行",
      meta: "系统当前没有硬阻断。",
      tone: "positive",
      copy: "当前没有新的第一优先级阻断。若仍需再次确认状态，可手动重新对账（刷新交易所状态）。",
    };
  }

function riskHeadline({ primaryBlocker, blockers, reconciliation, recovery }) {
  return operationalStatusHeadline({
    recovery,
    blockers: primaryBlocker ? [primaryBlocker] : blockers,
    reconciliation,
    readyLabel: "持续观察",
  });
}

function riskTone({ primaryBlocker, blockers, reconciliation, recovery, health }) {
  const activeBlockers = primaryBlocker ? [primaryBlocker] : blockers;
  if (isPausedAwaitingResume({ blockers, recovery })) return "warning";
  if (health?.halted || activeBlockers.length > 0 || reconciliation?.halt_required) return "danger";
  if (reconciliation?.observational_only && recovery.safe_to_trade) return "info";
  if (!recovery.safe_to_trade || recovery.review_required) return "warning";
  return "positive";
}

function shouldShowValidateAction({ reconciliation, recovery }) {
  return Boolean(
    reconciliationNeedsAttention(reconciliation)
    || reconciliation?.observational_only
    || recovery.review_required
  );
}

function shouldShowRebaselineAction({ reconciliation, recovery }) {
  return Boolean(
    recovery.rebaseline_available
    || reconciliation?.review_required
    || actionSuggestsRebaseline(reconciliation?.recommended_operator_action)
  );
}

function shouldShowResumeAction({ recovery }) {
  return Boolean(recovery.halted || recovery.resume_eligible);
}

function shouldShowInspectReconciliation({ reconciliation, recovery }) {
  return Boolean(
    reconciliation?.reconciliation_id
    && (reconciliationNeedsAttention(reconciliation) || recovery.review_required)
  );
}

function legMismatchTone(summary = {}) {
  if (Number(summary.missing_execution_chain_count || 0) > 0) return "danger";
  if (Number(summary.total_count || 0) > 0) return "warning";
  return "neutral";
}

function legMismatchSummaryMeta(summary = {}) {
  const items = Array.isArray(summary.items) ? summary.items : [];
  if (!items.length) {
    return "当前没有 long / short 两条腿之间的额外异常。";
  }
  const prefix = Number(summary.missing_execution_chain_count || 0) > 0
    ? `其中有 ${formatNumber(summary.missing_execution_chain_count || 0, 0)} 条腿在交易所存在，但本地没有对应执行链。`
    : "当前看到的是腿级数量差异，不等于整账户净仓异常。";
  const details = items
    .slice(0, 2)
    .map((item) => {
      const side = item.leg_side === "long" ? "多头腿" : item.leg_side === "short" ? "空头腿" : "净仓腿";
      return `${textOrFallback(item.symbol, "未知合约")} ${side}：本地 ${formatNumber(item.stored_qty)}，交易所 ${formatNumber(item.exchange_qty)}`;
    })
    .join("；");
  return `${prefix}${details ? ` ${details}` : ""}`;
}

function derivativesPositionModeLabel(value) {
  if (value === "hedge") return "对冲模式";
  if (value === "net") return "净仓模式";
  return textOrFallback(value, "待确认");
}

function exchangePositionModeLabel(value) {
  if (value === "long_short_mode") return "交易所对冲模式";
  if (value === "net_mode") return "交易所净仓模式";
  return textOrFallback(value, "交易所未返回");
}

function requiredExchangeModeMeta(positionModeContract = {}) {
  const required = exchangePositionModeLabel(positionModeContract.required_exchange_position_mode);
  return positionModeContract.position_mode_match_required
    ? `要求交易所返回 ${required}`
    : `当前没有强制交易所模式要求，期望值 ${required}`;
}

function localInstrumentLegMeta(rows = []) {
  if (!rows.length) return "当前没有持仓。";
  const dualLegged = rows.filter((item) => item.dual_legged);
  if (!dualLegged.length) return "当前持仓都是单腿净仓，没有 long / short 并存。";
  return dualLegged
    .slice(0, 2)
    .map((item) => `${textOrFallback(item.symbol, "未知合约")}：多头 ${formatNumber(item.long_position_qty)} / 空头 ${formatNumber(item.short_position_qty)}`)
    .join("；");
}

function actionSuggestsRebaseline(value) {
  return String(value || "").toLowerCase().includes("rebaseline");
}

function reconciliationNeedsAttention(reconciliation) {
  const severity = String(reconciliation?.severity || "").toUpperCase();
  return Boolean(
    reconciliation?.halt_required
    || reconciliation?.review_required
    || (severity && severity !== "CLEAN")
  );
}

function resumeActionHint({ recovery, uiHints }) {
  if (recovery.resume_eligible) {
    return recovery.halted ? operationalStatusCopy({ recovery }) : "";
  }
  return operationalStatusCopy({
    recovery,
    recoveryReasonText: textOrFallback(
      uiHints.recoveryReasonsText,
      listText(recovery.resume_blocked_reasons, "当前没有额外恢复说明")
    ),
  });
}

function isPausedAwaitingResume({ blockers = [], recovery = {} } = {}) {
  return Boolean(
    recovery.halted
    && recovery.resume_eligible
    && !recovery.safe_to_trade
    && (!blockers.length || blockers.every((item) => item?.blocker === "kill_switch_active"))
  );
}

function listText(value, fallback = "当前没有额外说明") {
  return Array.isArray(value) ? localizeList(value, fallback) : textOrFallback(value, fallback);
}

function phase1ShadowLabel(status) {
  const map = {
    not_configured: "未配置",
    idle: "空闲",
    healthy: "已追平",
    lagging: "仍有积压",
    degraded: "最近失败",
  };
  return map[String(status || "")] || readableState(status || "unknown");
}

function phase1ShadowTone(status) {
  if (status === "healthy") return "positive";
  if (status === "lagging") return "warning";
  if (status === "degraded") return "danger";
  return "neutral";
}

function backlogText(value) {
  if (value === null || value === undefined) return "待确认";
  return formatNumber(value, 0);
}

function phase1ShadowHasBacklog(phase1Shadow) {
  const lag = phase1Shadow.lag || {};
  return Number(lag.order_backlog || 0) > 0 || Number(lag.fill_backlog || 0) > 0 || Number(lag.obligation_backlog || 0) > 0;
}

function phase1ShadowReviewMeta(action) {
  if (!action) return "当前还没有人工核查记录";
  const actor = action.actor_identity || action.actor_role || "未知操作人";
  const reviewedAt = action.details?.reviewed_at || action.details?.snapshot_generated_at;
  return `${actor} 于 ${formatMaybeTimestamp(reviewedAt)}`;
}

function phase1ShadowLastError(phase1Shadow) {
  return (
    phase1Shadow.execution_shadow?.last_error
    || phase1Shadow.ledger_shadow?.last_error
    || "当前没有兼容层错误"
  );
}
