import { actionButton, pill, primaryStatusPanel, summaryStrip, surfaceCard, timeline } from "../components.js";
import { booleanWord, formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, listOrDash, middleEllipsis } from "../formatters.js";
import {
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
  const reconciliation = data.reconciliationLatest?.reconciliation || null;
  const metrics = data.metrics || {};
  const uiHints = data.uiHints || {};
  const currentPosition = trackedPosition(portfolio, runtime.symbols?.[0] || mode.default_symbol);

  return `
    <div class="panel-grid">
      <div class="span-12">
        ${primaryStatusPanel({
          eyebrow: "交易总览",
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
            { label: "目标仓位变化", value: formatSigned(latestDecision.position_target?.delta_position_qty), meta: readableState(latestDecision.position_target?.target_exposure_side || latestDecision.position_target?.position_intent, "方向待确认"), tone: latestDecision.decision_id ? "info" : "neutral" },
            { label: "策略门禁", value: booleanWord(latestDecision.policy_decision?.execution_allowed), meta: listOrDash(latestDecision.policy_decision?.blocker_reasons, "当前没有额外门禁说明"), tone: latestDecision.policy_decision?.execution_allowed ? "positive" : "warning" },
            { label: "风控结论", value: booleanWord(latestDecision.risk_decision?.approved), meta: listOrDash(latestDecision.risk_decision?.rejection_reasons, "当前没有额外风控说明"), tone: latestDecision.risk_decision?.approved ? "positive" : "danger" },
          ],
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "资产概览",
          kicker: "资产状态",
          copy: "值班视角下只保留最关键的仓位和收益信息。",
          content: summaryStrip([
            { label: "总权益", value: formatNumber(portfolio.total_equity), meta: `已实现 ${formatSigned(portfolio.realized_pnl)}`, tone: "info" },
            { label: "未实现收益", value: formatSigned(portfolio.unrealized_pnl), meta: `总敞口 ${formatNumber(portfolio.gross_exposure)}`, tone: Number(portfolio.unrealized_pnl || 0) >= 0 ? "positive" : "warning" },
            { label: "跟踪标的", value: trackedSymbol(runtime, mode), meta: currentPosition ? "来自最新组合快照" : "当前没有持仓", tone: "info" },
            { label: "当前仓位", value: readableState(currentPosition?.exposure_side || "flat"), meta: currentPosition ? formatSigned(currentPosition?.position_qty) : "当前没有持仓", tone: currentPosition ? "info" : "neutral" },
          ]),
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "执行概览",
          kicker: "执行状态",
          copy: "用一组摘要判断当前动作是否已经真正进入执行链路。",
          content: summaryStrip([
            { label: "最新委托", value: readableState(latestOrder?.status || "unknown"), meta: middleEllipsis(latestOrder?.client_order_id, 10, 6, "暂未生成委托"), tone: toneForOrderStatus(latestOrder?.status) },
            { label: "最新成交", value: latestFill ? `${formatNumber(latestFill.fill_qty)} @ ${formatNumber(latestFill.fill_price)}` : "暂未成交", meta: middleEllipsis(latestFill?.fill_id, 10, 6, "当前暂无成交编号"), tone: latestFill ? "positive" : "neutral" },
            { label: "对账结果", value: readableState(reconciliation?.severity || "unknown"), meta: middleEllipsis(reconciliation?.reconciliation_id, 10, 6, "暂时没有最新对账"), tone: reconciliation?.halt_required ? "danger" : toneForReconciliationSeverity(reconciliation?.severity) },
            { label: "活动委托", value: formatNumber(metrics.current_open_order_count, 0), meta: metrics.current_open_order_count > 0 ? "执行还在收敛中" : "当前没有活动委托", tone: metrics.current_open_order_count > 0 ? "warning" : "positive" },
          ]),
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "关注事项",
          kicker: "风险提示",
          copy: blockers.length ? "这里专门提醒当前最需要关注的风险和限制。" : "当前暂无新的硬阻断，但仍保留恢复和对账上下文。",
          classes: blockers.length || !recovery.safe_to_trade ? "" : "is-muted",
          content: timeline(overviewFocusItems({ blockers, recovery, reconciliation, uiHints }), "当前暂无新的高优先级关注项。"),
        })}
      </div>

      <div class="span-7">
        ${surfaceCard({
          title: "运行时间线",
          kicker: "关键链路",
          copy: "按时间把最近一次关键节点串起来，方便快速定位问题卡在哪一段链路。",
          content: timeline(buildTimeline({ latestDecision, latestOrder, latestFill, reconciliation }), "当前暂无新的运行活动。"),
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
          recoveryReasonText: listOrDash(recovery.resume_blocked_reasons, "当前没有给出额外恢复说明"),
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
        recoveryReasonText: uiHints.recoveryReasonsText || listOrDash(recovery.resume_blocked_reasons, "当前没有额外恢复说明"),
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

function buildTimeline({ latestDecision, latestOrder, latestFill, reconciliation }) {
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
  const rawIntent = String(target.position_intent || "hold").toLowerCase();
  const currentQty = Number(target.current_position_qty ?? detail.decision_context?.current_position_qty ?? 0);
  const targetQty = Number(target.target_position_qty ?? 0);
  const openOrders = Array.isArray(detail.decision_context?.current_open_orders) ? detail.decision_context.current_open_orders : [];
  if (rawIntent === "hold" && currentQty === 0 && targetQty === 0 && openOrders.length === 0) {
    return "继续观望";
  }
  return readableState(rawIntent);
}
