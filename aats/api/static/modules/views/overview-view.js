import { actionButton, pill, primaryStatusPanel, responsiveTable, summaryStrip, surfaceCard, timeline } from "../components.js";
import { localizeList } from "../copy.js";
import { booleanWord, escapeHtml, formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, middleEllipsis } from "../formatters.js";
import {
  hasFamilyExecutionSummary,
  readableFamilyExecutionDirection,
  readableFamilyExecutionSummary,
  readableOverlayParentSignalSummary,
  localizeError,
  operationalStatusCopy,
  operationalStatusHeadline,
  readableState,
  reconciliationStatusLabel,
  statusHeadline,
  toneForOrderStatus,
  toneForReconciliationSeverity,
  toneForRuntimeState,
  tradingStatusLabel,
} from "../terms.js";

export function renderOverviewView(data) {
  const health = data.health || {};
  const mode = data.mode || {};
  const runtime = data.runtime || {};
  const recovery = data.systemRecovery?.recovery || {};
  const blockers = data.blockers?.blockers || [];
  const portfolio = data.portfolio?.portfolio || {};
  const latestDecision = data.latestDecision || {};
  const latestOrder = data.executionLatest?.latest_order || null;
  const latestFill = data.executionLatest?.latest_fill || null;
  const terminalNoFill = data.executionLatest?.terminal_no_fill_explanation || null;
  const reconciliation = data.reconciliationLatest?.reconciliation || null;
  const positionsView = data.positions || {};
  const metrics = data.metrics || {};
  const aiRuntime = data.aiRuntime || {};
  const strategyRuntime = data.strategyRuntime || {};
  const strategyRuntimeSummary = strategyRuntime.summary || {};
  const entryExecutionGuard = strategyRuntime.entry_execution_guard || strategyRuntimeSummary.entry_execution_guard || {};
  const uiHints = data.uiHints || {};
  const currentPosition = trackedPosition(portfolio, runtime.symbols?.[0] || mode.default_symbol);
  const operatorTruthCockpit = buildOperatorTruthCockpit({
    aiRuntime,
    strategyRuntime,
    strategyRuntimeSummary,
    entryExecutionGuard,
    latestDecision,
    latestOrder,
    latestFill,
    terminalNoFill,
    metrics,
    blockers,
    recovery,
    reconciliation,
  });

  return `
    <div class="panel-grid">
      <div class="span-12">
        ${surfaceCard({
          title: "运行真相驾驶舱",
          kicker: "交易显微镜",
          panelKey: ["aiRuntime", "strategyRuntime", "latestDecision", "executionLatest", "blockers", "metrics"],
          copy: operatorTruthCockpit.copy,
          actions: `
            <div class="stack-actions table-actions--compact">
              ${actionButton("策略", "navigate-view", "strategy", "ghost")}
              ${actionButton("执行", "navigate-view", "execution", "ghost")}
              ${actionButton("风控", "navigate-view", "risk", "ghost")}
              ${actionButton("AI分析", "navigate-view", "aiAnalysis", "ghost")}
            </div>
          `,
          content: `
            ${summaryStrip(operatorTruthCockpit.summary)}
            ${timeline(operatorTruthCockpit.items, "当前暂无可展示的运行真相。")}
          `,
        })}
      </div>

      <div class="span-12">
        ${primaryStatusPanel({
          eyebrow: "交易总览",
          panelKey: ["latestDecision", "executionLatest"],
          headline: overviewHeadline({ latestDecision, latestOrder, recovery }),
          summary: overviewSummary({ latestDecision, latestOrder, latestFill, blockers, reconciliation, recovery }),
          tone: overviewTone({ health, recovery, blockers, latestOrder }),
          actions: latestDecision.decision_id ? actionButton("查看决策链", "inspect-decision", latestDecision.decision_id) : "",
          pills: [
            pill(`运行状态 ${readableState(health.runtime_state || health.overall_status)}`, toneForRuntimeState(health.runtime_state || health.overall_status)),
            pill(`自动交易 ${tradingStatusLabel(recovery)}`, recovery.safe_to_trade ? "positive" : recovery.halted && recovery.resume_eligible ? "warning" : "danger"),
            pill(`最新委托 ${readableState(latestOrder?.status || "unknown")}`, toneForOrderStatus(latestOrder?.status)),
          ],
          metrics: [
            { label: "最新决策时间", value: formatMaybeTimestamp(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts), meta: formatRelativeAge(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts), tone: latestDecision.decision_id ? "info" : "neutral" },
            { label: "目标仓位变化", value: formatSigned(latestDecision.position_target?.delta_position_qty), meta: readableFamilyExecutionDirection(latestDecision.position_target || {}, "方向待确认"), tone: latestDecision.decision_id ? "info" : "neutral" },
            { label: "策略门禁", value: booleanWord(latestDecision.policy_decision?.execution_allowed), meta: localizeList(latestDecision.policy_decision?.blocker_reasons, "当前没有额外门禁说明"), tone: latestDecision.policy_decision?.execution_allowed ? "positive" : "warning" },
            { label: "风控结论", value: booleanWord(latestDecision.risk_decision?.approved), meta: localizeList(latestDecision.risk_decision?.rejection_reasons, "当前没有额外风控说明"), tone: latestDecision.risk_decision?.approved ? "positive" : "danger" },
          ],
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "资产概览",
          kicker: "资产状态",
          copy: "值班视角下只保留账户总览信息，持仓细节单独放到实时持仓表格。",
          content: summaryStrip([
            { label: "总权益", value: formatNumber(portfolio.total_equity), meta: `已实现 ${formatSigned(portfolio.realized_pnl)}`, tone: "info" },
            { label: "未实现收益", value: formatSigned(portfolio.unrealized_pnl), meta: `总敞口 ${formatNumber(portfolio.gross_exposure)}`, tone: Number(portfolio.unrealized_pnl || 0) >= 0 ? "positive" : "warning" },
            { label: "持仓数量", value: formatNumber((portfolio.positions || []).length, 0), meta: currentPosition ? "详情请看下方当前持仓表" : "当前没有持仓", tone: "info" },
            { label: "净敞口", value: formatSigned(portfolio.net_exposure), meta: portfolio.snapshot_ts ? `快照 ${formatMaybeTimestamp(portfolio.snapshot_ts)}` : "快照时间待同步", tone: portfolio.net_exposure === null || portfolio.net_exposure === undefined ? "neutral" : Number(portfolio.net_exposure) === 0 ? "positive" : "info" },
          ]),
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "执行概览",
          kicker: "执行状态",
          panelKey: ["executionLatest", "reconciliationLatest"],
          copy: "用一组摘要判断当前动作是否已经真正进入执行链路。",
          content: summaryStrip([
            { label: "最新委托", value: readableState(latestOrder?.status || "unknown"), meta: middleEllipsis(latestOrder?.client_order_id, 10, 6, "暂未生成委托"), tone: toneForOrderStatus(latestOrder?.status) },
            { label: "最新成交", value: latestFill ? `${formatNumber(latestFill.fill_qty)} @ ${formatNumber(latestFill.fill_price)}` : "暂未成交", meta: middleEllipsis(latestFill?.fill_id, 10, 6, "当前暂无成交编号"), tone: latestFill ? "positive" : "neutral" },
            { label: "对账结果", value: readableState(reconciliation?.severity || "unknown"), meta: middleEllipsis(reconciliation?.reconciliation_id, 10, 6, "暂时没有最新对账"), tone: reconciliation?.halt_required ? "danger" : toneForReconciliationSeverity(reconciliation?.severity) },
            { label: "活动委托", value: formatNumber(metrics.current_open_order_count, 0), meta: activityOrderMeta({ currentOpenOrderCount: metrics.current_open_order_count, positionCount: (portfolio.positions || []).length }), tone: metrics.current_open_order_count > 0 ? "warning" : "positive" },
          ]),
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "关注事项",
          kicker: "风险提示",
          panelKey: "reconciliationLatest",
          copy: blockers.length ? "这里专门提醒当前最需要关注的风险和限制。" : "当前暂无新的硬阻断，但仍保留恢复和对账上下文。",
          classes: blockers.length || !recovery.safe_to_trade ? "" : "is-muted",
          content: timeline(overviewFocusItems({ blockers, recovery, reconciliation, uiHints }), "当前暂无新的高优先级关注项。"),
        })}
      </div>

      <div class="span-12">
        ${surfaceCard({
          title: "当前持仓",
          kicker: "实时持仓",
          copy: "这里按表格展示最新组合快照中的全部持仓，多条持仓时也能直接横向比较。",
          content: renderCurrentPositionsTable({
            portfolio,
            positionsView,
            fallbackSymbol: trackedSymbol(runtime, mode),
          }),
        })}
      </div>

      <div class="span-7">
        ${surfaceCard({
          title: "运行时间线",
          kicker: "关键链路",
          panelKey: ["latestDecision", "executionLatest", "reconciliationLatest"],
          copy: "按时间把最近一次关键节点串起来，方便快速定位问题卡在哪一段链路。",
          content: timeline(buildTimeline({ latestDecision, latestOrder, latestFill, terminalNoFill, reconciliation }), "当前暂无新的运行活动。"),
        })}
      </div>

      <div class="span-5">
        ${surfaceCard({
          title: "运行指标",
          kicker: "核心指标",
          copy: "这些数字用于观察整体节奏和异常堆积，不抢首屏主判断。",
          classes: "is-muted",
          content: summaryStrip([
            { label: "策略轮次", value: formatNumber(metrics.decision_cycle_count, 0), meta: "累计完成的策略判断次数", tone: "info" },
            { label: "拟下单次数", value: formatNumber(metrics.order_intent_count, 0), meta: "真正进入执行规划的次数", tone: "info" },
            { label: "累计成交笔数", value: formatNumber(metrics.fill_count, 0), meta: "已确认落库的成交笔数", tone: Number(metrics.fill_count || 0) > 0 ? "positive" : "neutral" },
            { label: "累计异常对账", value: formatNumber(metrics.reconciliation_mismatch_count, 0), meta: "累计出现过的非一致对账次数，不等于当前待处理数量", tone: Number(metrics.reconciliation_mismatch_count || 0) > 0 ? "warning" : "positive" },
          ]),
        })}
      </div>
    </div>
  `;
}

function overviewHeadline({ latestDecision, latestOrder, recovery }) {
  if (!latestDecision.decision_id) {
    return operationalStatusHeadline({
      recovery,
      readyLabel: "持续观察",
    });
  }
  if (latestOrder?.status && ["created", "submitting", "partially_filled", "cancel_pending"].includes(String(latestOrder.status).toLowerCase())) {
    return statusHeadline("执行中");
  }
  return overviewIntentLabel(latestDecision);
}

function overviewSummary({ latestDecision, latestOrder, latestFill, blockers, reconciliation, recovery }) {
  if (!latestDecision.decision_id) {
    return recovery.safe_to_trade
      ? operationalStatusCopy({ recovery, readyCopy: "当前暂无新的决策输出，系统当前保持观察状态，暂未进入新的开平仓动作。" })
      : operationalStatusCopy({
          recovery,
          recoveryReasonText: localizeList(recovery.resume_blocked_reasons, "当前没有给出额外恢复说明"),
        });
  }
  return `${formatRelativeAge(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts)}，系统形成了 ${overviewIntentLabel(latestDecision)} 的策略判断。`
    + `${latestOrder ? ` 最近一笔委托状态为 ${readableState(latestOrder.status)}。` : " 本轮暂未生成新委托。"}`
    + `${latestFill ? ` 最近一笔成交已落库，数量 ${formatNumber(latestFill.fill_qty)}。` : " 当前暂无新的成交落库。"}`
    + `${blockers.length ? ` 当前主要限制来自 ${localizeError(blockers[0].blocker)}。` : reconciliation ? ` 最近对账结论为 ${readableState(reconciliation.severity)}。` : ""}`;
}

function overviewTone({ health, recovery, blockers, latestOrder }) {
  if (health.halted || blockers.length > 0 || !recovery.safe_to_trade) return "danger";
  if (latestOrder && ["submitting", "partially_filled", "cancel_pending"].includes(String(latestOrder.status || "").toLowerCase())) return "info";
  return "positive";
}

function buildOperatorTruthCockpit({
  aiRuntime,
  strategyRuntime,
  strategyRuntimeSummary,
  entryExecutionGuard,
  latestDecision,
  latestOrder,
  latestFill,
  terminalNoFill,
  metrics,
  blockers,
  recovery,
  reconciliation,
}) {
  const configuredMode = textOrFallback(aiRuntime.configured_operating_mode || aiRuntime.legacy_modes?.configured_operating_mode);
  const effectiveMode = textOrFallback(aiRuntime.effective_operating_mode || aiRuntime.legacy_modes?.effective_operating_mode);
  const activeFamily = textOrFallback(
    strategyRuntimeSummary.latest_selected_family
      || strategyRuntimeSummary.configured_active_family
      || strategyRuntime.configured_parameters?.strategy_family_active,
    "directional",
  );
  const latestBundleStatus = strategyRuntimeSummary.latest_bundle_status || strategyRuntime.latest_bundle?.status || "unknown";
  const executionControlSummary = strategyRuntimeSummary.execution_control_summary || {};
  const providerState = aiRuntime.provider_state || aiRuntime.provider || "unknown";
  const providerConfigured = aiRuntime.configured === true || aiRuntime.provider_ready === true;
  const profileAutoEffective = aiRuntime.strategy_profile_auto_control_effective === true;
  const guardActive = entryExecutionGuard.active === true;
  const decisionFreshness = latestDecision.decision_time || latestDecision.decision_context?.as_of_ts;

  return {
    copy: `OKX BTC-USDT-SWAP；当前实盘载体 ${readableState(activeFamily)}；影子基准：未验证。`,
    summary: [
      {
        label: "有效运行态",
        value: readableState(effectiveMode),
        meta: `目标 ${readableState(configuredMode)} / ${aiRuntime.manual_override_active ? "手动覆盖生效" : "无手动覆盖"}`,
        tone: aiRuntimeTone(effectiveMode, aiRuntime),
      },
      {
        label: "AI 服务",
        value: providerConfigured ? "已配置" : readableState(providerState),
        meta: `服务 ${readableState(providerState)} / 影子 ${aiRuntime.shadow_mode_enabled ? "开启" : "关闭"}`,
        tone: providerConfigured && !aiRuntime.provider_degraded ? "positive" : aiRuntime.provider_degraded ? "warning" : "neutral",
      },
      {
        label: "策略载体",
        value: readableState(activeFamily),
        meta: `最新执行包 ${readableState(latestBundleStatus)} / 自动档 ${profileAutoEffective ? "生效" : "手动"}`,
        tone: activeFamily === "directional" ? "info" : "warning",
      },
      {
        label: "准入门禁",
        value: guardActive ? "仅参考" : "允许自动执行",
        meta: entryExecutionGuard.operator_summary || entryExecutionGuard.summary || readableState(executionControlSummary.primary_mode || "unknown"),
        tone: guardActive ? "warning" : "positive",
      },
      {
        label: "决策到执行",
        value: latestDecision.decision_id ? overviewIntentLabel(latestDecision) : "暂无决策",
        meta: latestOrder ? `委托 ${readableState(latestOrder.status)}` : "本轮暂未生成委托",
        tone: decisionExecutionTone(latestDecision, latestOrder),
      },
      {
        label: "无成交终局",
        value: hasTerminalNoFill(terminalNoFill) ? readableTerminalNoFillReason(terminalNoFill.reason) : "无",
        meta: hasTerminalNoFill(terminalNoFill) ? terminalNoFillMeta(terminalNoFill) : "当前没有终端无成交解释",
        tone: hasTerminalNoFill(terminalNoFill) ? "warning" : "neutral",
      },
      {
        label: "成交证据",
        value: formatNumber(metrics.fill_count, 0),
        meta: latestFill ? `${formatNumber(latestFill.fill_qty)} @ ${formatNumber(latestFill.fill_price)}` : "当前暂无最新成交",
        tone: Number(metrics.fill_count || 0) > 0 ? "positive" : "neutral",
      },
      {
        label: "阻断队列",
        value: blockers.length ? `${formatNumber(blockers.length, 0)} 条` : recovery.safe_to_trade ? "清空" : "恢复受限",
        meta: blockers[0] ? localizeError(blockers[0].blocker) : reconciliation ? `对账 ${readableState(reconciliation.severity)}` : "当前没有硬阻断",
        tone: blockers.length ? "danger" : recovery.safe_to_trade ? "positive" : "warning",
      },
    ],
    items: [
      {
        title: "AI 有效路径",
        subtitle: `配置 ${readableState(configuredMode)} / 生效 ${readableState(effectiveMode)}`,
        detail: `模型服务 ${readableState(providerState)}；手动覆盖 ${aiRuntime.manual_override_active ? "生效" : "未生效"}；档位自动控制 ${profileAutoEffective ? "生效" : "未生效"}。`,
        timestamp: formatMaybeTimestamp(aiRuntime.last_provider_recovered_at || aiRuntime.last_provider_degraded_at, "AI 时间待同步"),
        pill: pill("AI", aiRuntimeTone(effectiveMode, aiRuntime)),
      },
      {
        title: "策略与门禁",
        subtitle: `${readableState(activeFamily)} / ${readableState(latestBundleStatus)}`,
        detail: entryExecutionGuard.operator_summary || executionControlSummary.summary || strategyRuntimeSummary.operator_summary || "当前没有额外策略门禁说明。",
        timestamp: formatMaybeTimestamp(strategyRuntime.generated_at, "策略时间待同步"),
        pill: pill("策略", guardActive ? "warning" : "positive"),
      },
      latestDecision.decision_id
        ? {
            title: "最新决策证据",
            subtitle: middleEllipsis(latestDecision.decision_id, 12, 8),
            detail: `${overviewIntentLabel(latestDecision)}；${latestOrder ? `委托 ${readableState(latestOrder.status)}` : "未生成新委托"}；成交累计 ${formatNumber(metrics.fill_count, 0)}。`,
            timestamp: formatMaybeTimestamp(decisionFreshness),
            pill: pill("链路", latestOrder ? toneForOrderStatus(latestOrder.status) : "info"),
          }
        : null,
      hasTerminalNoFill(terminalNoFill)
        ? {
            title: "终端无成交解释",
            subtitle: readableTerminalNoFillReason(terminalNoFill.reason),
            detail: `这不是成交链路丢失；${terminalNoFillMeta(terminalNoFill)}。`,
            timestamp: formatMaybeTimestamp(terminalNoFill.latest_order_updated_at, "执行时间待同步"),
            pill: pill("no-fill已解释", "warning"),
          }
        : null,
      blockers.length
        ? {
            title: "当前阻断",
            subtitle: blockers[0].subsystem ? `来源：${readableState(blockers[0].subsystem)}` : "系统阻断",
            detail: localizeError(blockers[0].recommended_action || blockers[0].blocker),
            pill: pill(blockers[0].affects_execution ? "阻断执行" : "人工关注", blockers[0].affects_execution ? "danger" : "warning"),
          }
        : {
            title: "恢复与对账",
            subtitle: recovery.safe_to_trade ? "当前允许执行" : readableState(recovery.recovery_state),
            detail: reconciliation ? `最近对账 ${readableState(reconciliation.severity)}。` : "当前没有新的对账异常。",
            timestamp: formatMaybeTimestamp(reconciliation?.as_of_ts, "对账时间待同步"),
            pill: pill(recovery.safe_to_trade ? "未阻断" : "恢复受限", recovery.safe_to_trade ? "positive" : "warning"),
          },
    ].filter(Boolean),
  };
}

function textOrFallback(value, fallback = "unknown") {
  const text = String(value ?? "").trim();
  return text || fallback;
}

function aiRuntimeTone(effectiveMode, aiRuntime) {
  if (aiRuntime.provider_degraded || aiRuntime.outcome_review_required) return "warning";
  if (effectiveMode === "ai_decision_maker") return "positive";
  if (effectiveMode === "ai_assisted") return "info";
  if (effectiveMode === "baseline_only") return "neutral";
  return "warning";
}

function decisionExecutionTone(latestDecision, latestOrder) {
  if (!latestDecision.decision_id) return "neutral";
  if (!latestOrder) return "info";
  return toneForOrderStatus(latestOrder.status);
}

function hasTerminalNoFill(explanation) {
  return Boolean(explanation && explanation.classification === "terminal_order_surface_without_fill");
}

function readableTerminalNoFillReason(reason) {
  const map = {
    terminal_order_blocked_before_fill: "下单前被阻断",
    terminal_order_failed_or_rejected_before_fill: "失败或拒单且无成交",
    terminal_order_canceled_before_fill: "撤单且无成交",
    terminal_order_expired_before_fill: "过期且无成交",
    terminal_order_dry_run_no_fill_expected: "演练单不期待成交",
    terminal_order_surface_without_fill: "终端委托无成交",
  };
  return map[String(reason || "").trim()] || readableState(reason || "terminal_order_surface_without_fill");
}

function terminalNoFillStateSummary(explanation) {
  const states = Array.isArray(explanation?.terminal_states) ? explanation.terminal_states : [];
  return states.length ? states.map((item) => readableState(item, item)).join(" / ") : "状态待确认";
}

function terminalNoFillIntentSummary(explanation) {
  const intents = Array.isArray(explanation?.terminal_position_intents) ? explanation.terminal_position_intents : [];
  return intents.length ? intents.map((item) => readableState(item, item)).join(" / ") : "意图待确认";
}

function terminalNoFillMeta(explanation) {
  return `${terminalNoFillIntentSummary(explanation)} | ${terminalNoFillStateSummary(explanation)}`;
}

function overviewFocusItems({ blockers, recovery, reconciliation, uiHints }) {
  if (blockers.length > 0) {
    return blockers.slice(0, 3).map((item) => ({
      title: localizeError(item.blocker),
      subtitle: item.subsystem ? `来源：${readableState(item.subsystem)}` : "系统阻断",
      detail: localizeError(item.recommended_action || item.blocker),
      pill: pill(item.affects_execution ? "阻断执行" : "人工关注", item.affects_execution ? "danger" : "warning"),
    }));
  }
  const items = [];
  if (!recovery.safe_to_trade) {
    items.push({
      title: statusHeadline(recovery.halted && recovery.resume_eligible ? "待恢复" : "恢复受限"),
      subtitle: readableState(recovery.recovery_state),
        detail: operationalStatusCopy({
          recovery,
          recoveryReasonText: uiHints.recoveryReasonsText || localizeList(recovery.resume_blocked_reasons, "当前没有额外恢复说明"),
        }),
      pill: pill(recovery.halted && recovery.resume_eligible ? "待恢复" : "恢复受限", "warning"),
    });
  }
  if (reconciliation?.reconciliation_id) {
    items.push({
      title: "最近一次对账结论",
      subtitle: reconciliation.reconciliation_id,
      detail: readableState(reconciliation.severity),
      pill: pill(reconciliationStatusLabel(reconciliation), reconciliation.halt_required ? "danger" : toneForReconciliationSeverity(reconciliation.severity)),
    });
  }
  return items;
}

function trackedPosition(portfolio, symbol) {
  const positions = portfolio.positions || [];
  return positions.find((item) => item.symbol === symbol) || positions[0] || null;
}

function trackedSymbol(runtime, mode) {
  return runtime.symbols?.[0] || mode.default_symbol || "标的待确认";
}

function activityOrderMeta({ currentOpenOrderCount, positionCount }) {
  if (Number(currentOpenOrderCount || 0) > 0) {
    return "执行还在收敛中";
  }
  if (Number(positionCount || 0) > 0) {
    return "当前无挂单，但仍有未平仓仓位";
  }
  return "当前没有活动委托";
}

function renderCurrentPositionsTable({ portfolio, positionsView = {}, fallbackSymbol }) {
  const instrumentPositions = Array.isArray(positionsView.local_instrument_positions)
    ? [...positionsView.local_instrument_positions]
    : [];
  const useInstrumentState = instrumentPositions.some((item) => Number(item.leg_count || 0) > 1 || item.position_mode === "long_short_mode");
  if (useInstrumentState) {
    const rows = instrumentPositions.sort(
      (left, right) => Math.abs(Number(right?.gross_position_notional || 0)) - Math.abs(Number(left?.gross_position_notional || 0))
    );
    return responsiveTable(
      ["标的", "仓位模式 / 双腿", "净敞口", "毛敞口", "浮盈亏 / 快照"],
      rows.map((position) => [
        `<div><strong>${escapeHtml(position.symbol || fallbackSymbol)}</strong><div class="table-meta">${positionModeLabel(position.position_mode)} | ${readableState(position.margin_mode, "保证金模式待确认")}</div></div>`,
        `<div><strong>${position.dual_legged ? "双腿并存" : readableState(position.exposure_side || "flat")}</strong><div class="table-meta">多头 ${formatNumber(position.long_position_qty)} / 空头 ${formatNumber(position.short_position_qty)}</div></div>`,
        `<div><strong>${formatSigned(position.net_position_notional)}</strong><div class="table-meta">净数量 ${formatSigned(position.net_position_qty)}</div></div>`,
        `<div><strong>${formatNumber(position.gross_position_notional)}</strong><div class="table-meta">毛数量 ${formatNumber(position.gross_position_qty)} | 杠杆 ${formatNumber(position.target_leverage, 2)}</div></div>`,
        `<div><strong>${formatSigned(position.unrealized_pnl)}</strong><div class="table-meta">${formatInstrumentLegMeta(position, portfolio.snapshot_ts)}</div></div>`,
      ]),
      "当前没有持仓。",
      rows.map((position) => ({
        kicker: "实时持仓",
        title: position.symbol || fallbackSymbol,
        meta: portfolio.snapshot_ts ? `快照 ${formatMaybeTimestamp(portfolio.snapshot_ts)}` : "快照时间待同步",
        tone: Number(position.unrealized_pnl || 0) >= 0 ? "positive" : "warning",
        badge: pill(position.dual_legged ? "双腿模式" : readableState(position.exposure_side || "flat"), "info"),
        fields: [
          { label: "持仓模式", value: positionModeLabel(position.position_mode), meta: readableState(position.margin_mode, "保证金模式待确认") },
          { label: "净敞口", value: formatSigned(position.net_position_notional), meta: `净数量 ${formatSigned(position.net_position_qty)}` },
          { label: "毛敞口", value: formatNumber(position.gross_position_notional), meta: `多头 ${formatNumber(position.long_position_notional)} / 空头 ${formatNumber(position.short_position_notional)}` },
        ],
        details: [
          { label: "多头腿", value: formatLegValue(position, "long") },
          { label: "空头腿", value: formatLegValue(position, "short") },
          { label: "浮盈亏", value: formatSigned(position.unrealized_pnl) },
        ],
        detailLabel: "展开持仓详情",
      }))
    );
  }
  const positions = [...(portfolio.positions || [])].sort(
    (left, right) => Math.abs(Number(right?.position_notional || 0)) - Math.abs(Number(left?.position_notional || 0))
  );
  return responsiveTable(
    ["标的", "方向与数量", "名义敞口", "开仓均价 / 保证金", "浮盈亏 / 快照"],
    positions.map((position) => [
      `<div><strong>${escapeHtml(position.symbol || fallbackSymbol)}</strong><div class="table-meta">${readableState(position.product_type, "产品类型待确认")} | ${readableState(position.margin_mode, "保证金模式待确认")}</div></div>`,
      `<div><strong>${readableState(position.exposure_side || "flat")}</strong><div class="table-meta">数量 ${formatSigned(position.position_qty)}</div></div>`,
      `<div><strong>${formatSigned(position.position_notional)}</strong><div class="table-meta">杠杆 ${formatNumber(position.target_leverage, 2)}</div></div>`,
      `<div><strong>${formatNumber(position.avg_entry_price)}</strong><div class="table-meta">保证金 ${formatNumber(position.margin_allocated)}</div></div>`,
      `<div><strong>${formatSigned(position.unrealized_pnl)}</strong><div class="table-meta">${portfolio.snapshot_ts ? formatMaybeTimestamp(portfolio.snapshot_ts) : "快照时间待同步"}</div></div>`,
    ]),
    "当前没有持仓。",
    positions.map((position) => ({
      kicker: "实时持仓",
      title: position.symbol || fallbackSymbol,
      meta: portfolio.snapshot_ts ? `快照 ${formatMaybeTimestamp(portfolio.snapshot_ts)}` : "快照时间待同步",
      tone: Number(position.unrealized_pnl || 0) >= 0 ? "positive" : "warning",
      badge: pill(readableState(position.exposure_side || "flat"), "info"),
      fields: [
        { label: "产品与模式", value: readableState(position.product_type, "产品类型待确认"), meta: readableState(position.margin_mode, "保证金模式待确认") },
        { label: "持仓数量", value: formatSigned(position.position_qty), meta: `名义 ${formatSigned(position.position_notional)}` },
        { label: "浮盈亏", value: formatSigned(position.unrealized_pnl), meta: `杠杆 ${formatNumber(position.target_leverage, 2)}` },
      ],
      details: [
        { label: "开仓均价", value: formatNumber(position.avg_entry_price) },
        { label: "保证金占用", value: formatNumber(position.margin_allocated) },
        { label: "强平价格", value: position.liquidation_price === null ? "当前没有强平价格" : formatNumber(position.liquidation_price) },
      ],
      detailLabel: "展开持仓详情",
    }))
  );
}

function positionModeLabel(value) {
  if (value === "long_short_mode") return "对冲模式";
  if (value === "net_mode") return "净仓模式";
  return readableState(value, "持仓模式待确认");
}

function formatInstrumentLegMeta(position = {}, snapshotTs = null) {
  const legText = `多头 ${formatNumber(position.long_position_qty)} / 空头 ${formatNumber(position.short_position_qty)}`;
  if (!snapshotTs) return legText;
  return `${legText} | ${formatMaybeTimestamp(snapshotTs)}`;
}

function formatLegValue(position = {}, side) {
  const prefix = side === "short" ? "short" : "long";
  const qty = formatNumber(position[`${prefix}_position_qty`]);
  const notional = formatNumber(position[`${prefix}_position_notional`]);
  return `${qty} / ${notional}`;
}

function buildTimeline({ latestDecision, latestOrder, latestFill, terminalNoFill, reconciliation }) {
  return [
    latestDecision?.decision_id
      ? {
          title: "最新决策",
          subtitle: latestDecision.decision_id,
          detail: `${overviewIntentLabel(latestDecision)} | ${latestDecision.decision_context?.symbol || "标的待确认"}`,
          timestamp: formatMaybeTimestamp(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts),
          pill: pill("策略", "info"),
        }
      : null,
    latestOrder?.client_order_id
      ? {
          title: "最新委托",
          subtitle: latestOrder.client_order_id,
          detail: `${readableState(latestOrder.status)} | ${formatSigned(latestOrder.requested_qty)}`,
          timestamp: formatMaybeTimestamp(latestOrder.last_update_ts || latestOrder.created_at),
          pill: pill("执行", toneForOrderStatus(latestOrder.status)),
        }
      : null,
    latestFill?.fill_id
      ? {
          title: "最新成交",
          subtitle: latestFill.fill_id,
          detail: `${formatNumber(latestFill.fill_qty)} @ ${formatNumber(latestFill.fill_price)}`,
          timestamp: formatMaybeTimestamp(latestFill.ingestion_timestamp),
          pill: pill("成交", "positive"),
        }
      : null,
    hasTerminalNoFill(terminalNoFill)
      ? {
          title: "终端无成交",
          subtitle: readableTerminalNoFillReason(terminalNoFill.reason),
          detail: terminalNoFillMeta(terminalNoFill),
          timestamp: formatMaybeTimestamp(terminalNoFill.latest_order_updated_at),
          pill: pill("no-fill", "warning"),
        }
      : null,
    reconciliation?.reconciliation_id
      ? {
          title: "最新对账",
          subtitle: reconciliation.reconciliation_id,
          detail: readableState(reconciliation.severity),
          timestamp: formatMaybeTimestamp(reconciliation.as_of_ts),
          pill: pill(reconciliationStatusLabel(reconciliation), reconciliation?.halt_required ? "danger" : toneForReconciliationSeverity(reconciliation?.severity)),
        }
      : null,
  ].filter(Boolean);
}

function overviewIntentLabel(detail) {
  const target = detail.position_target || {};
  if (hasFamilyExecutionSummary(target)) {
    const summary = readableFamilyExecutionSummary(target, readableState(target.position_intent || "hold"));
    const parentSignalSummary = readableOverlayParentSignalSummary(target, "");
    return parentSignalSummary ? `${summary} | ${parentSignalSummary}` : summary;
  }
  const rawIntent = String(target.position_intent || "hold").toLowerCase();
  const currentQty = Number(target.current_position_qty ?? detail.decision_context?.current_position_qty ?? 0);
  const targetQty = Number(target.target_position_qty ?? 0);
  const openOrders = Array.isArray(detail.decision_context?.current_open_orders) ? detail.decision_context.current_open_orders : [];
  if (rawIntent === "hold" && currentQty === 0 && targetQty === 0 && openOrders.length === 0) {
    return "继续观望";
  }
  return readableFamilyExecutionSummary(target, readableState(rawIntent));
}
