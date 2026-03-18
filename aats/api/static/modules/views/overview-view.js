import { actionButton, callout, kvList, pill, statGrid, surfaceCard, timeline } from "../components.js";
import { booleanWord, formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, listOrDash } from "../formatters.js";
import { localizeError, readableState, toneForOrderStatus, toneForRuntimeState } from "../terms.js";

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
      <div class="span-8">
        ${surfaceCard({
          title: "当前交易结论",
          kicker: "先看这里",
          copy: "先确认现在能不能交易，再看这一轮策略到底是想开仓、平仓，还是继续观望。",
          classes: "hero-card",
          actions: latestDecision.decision_id ? actionButton("查看决策详情", "inspect-decision", latestDecision.decision_id) : "",
          content: `
            ${callout({
              title: latestDecision.position_target?.position_intent ? `最新动作：${overviewIntentLabel(latestDecision)}` : "当前没有新的交易动作",
              copy: decisionNarrative(latestDecision, latestOrder),
              pills: [
                pill(`系统：${readableState(health.runtime_state || health.overall_status)}`, toneForRuntimeState(health.runtime_state || health.overall_status)),
                pill(`交易资格：${booleanWord(recovery.safe_to_trade)}`, recovery.safe_to_trade ? "positive" : "danger"),
                pill(`最新订单：${readableState(latestOrder?.status || "unknown")}`, toneForOrderStatus(latestOrder?.status)),
              ],
            })}
            ${statGrid([
              { label: "最新决策时间", value: formatMaybeTimestamp(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts), meta: formatRelativeAge(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts) },
              { label: "目标仓位变化", value: formatSigned(latestDecision.position_target?.delta_position_qty), meta: readableState(latestDecision.position_target?.target_exposure_side) },
              { label: "策略门禁", value: booleanWord(latestDecision.policy_decision?.execution_allowed), meta: listOrDash(latestDecision.policy_decision?.blocker_reasons) },
              { label: "风控结论", value: booleanWord(latestDecision.risk_decision?.approved), meta: listOrDash(latestDecision.risk_decision?.rejection_reasons) },
            ])}
          `,
        })}
      </div>
      <div class="span-4">
        ${surfaceCard({
          title: "当前为什么不能交易",
          kicker: "人工介入提示",
          copy: blockers.length ? "下面这些原因正在阻止系统继续自动交易。" : "当前没有明确阻断项，系统没有被硬性卡住。",
          content: blockers.length
            ? timeline(
                blockers.map((item) => ({
                  title: localizeError(item.blocker),
                  subtitle: item.subsystem ? `来源：${readableState(item.subsystem)}` : "系统阻断",
                  detail: localizeError(item.recommended_action || item.blocker),
                  pill: pill(item.affects_execution ? "阻断交易" : "仅提示", item.affects_execution ? "danger" : "warning"),
                })),
                "当前没有阻断项。"
              )
            : kvList([
                ["系统整体状态", readableState(health.overall_status), readableState(health.runtime_state)],
                ["是否已暂停", booleanWord(health.halted), "手动 kill switch"],
                ["能否恢复自动交易", booleanWord(recovery.resume_eligible), uiHints.recoveryReasonsText || listOrDash(recovery.resume_blocked_reasons)],
              ]),
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "账户权益与收益",
          kicker: "资金概览",
          copy: "这里看账户净值、已实现收益、浮动盈亏和当前敞口。",
          content: statGrid([
            { label: "总权益", value: formatNumber(portfolio.total_equity), meta: "账户当前总价值" },
            { label: "已实现收益", value: formatSigned(portfolio.realized_pnl), meta: "已经落袋的收益" },
            { label: "未实现收益", value: formatSigned(portfolio.unrealized_pnl), meta: "持仓浮动盈亏" },
            { label: "总敞口", value: formatNumber(portfolio.gross_exposure), meta: `净敞口 ${formatSigned(portfolio.net_exposure)}` },
          ]),
        })}
      </div>
      <div class="span-4">
        ${surfaceCard({
          title: "当前持仓",
          kicker: "仓位状态",
          copy: currentPosition ? "这里显示当前主跟踪仓位。" : "当前没有检测到持仓。",
          content: kvList([
            ["跟踪标的", trackedSymbol(runtime, mode), "当前主交易标的"],
            ["仓位方向", readableState(currentPosition?.exposure_side || "flat"), currentPosition ? "来自最新组合快照" : "当前空仓"],
            ["仓位数量", formatSigned(currentPosition?.position_qty), currentPosition ? `目标杠杆 ${formatNumber(currentPosition?.target_leverage)}` : ""],
            ["持仓均价", formatNumber(currentPosition?.average_entry_price), currentPosition ? `最新标记价 ${formatNumber(currentPosition?.mark_price)}` : ""],
          ]),
        })}
      </div>
      <div class="span-4">
        ${surfaceCard({
          title: "恢复与基线",
          kicker: "可信状态",
          copy: "当系统进入恢复、复核或重建基线流程时，这里告诉你现在卡在哪一步。",
          content: kvList([
            ["恢复状态", readableState(recovery.recovery_state), recovery.safe_to_trade ? "已满足继续自动交易条件" : "当前仍受限制"],
            ["是否需要人工确认", booleanWord(recovery.review_required), uiHints.recoveryReasonsText || listOrDash(recovery.resume_blocked_reasons)],
            ["基线状态", readableState(runtime.baseline_takeover?.status), readableState(runtime.baseline_takeover?.baseline_kind)],
            ["最近确认基线", formatMaybeTimestamp(runtime.baseline_takeover?.last_rebaseline_at), runtime.baseline_takeover?.last_rebaseline_event_ref || "-"],
          ]),
        })}
      </div>

      <div class="span-6">
        ${surfaceCard({
          title: "最新执行进展",
          kicker: "订单与成交",
          copy: "快速看最近一笔委托、最近一笔成交和最新对账结论。",
          actions: latestOrder?.client_order_id ? actionButton("查看订单详情", "inspect-order", latestOrder.client_order_id) : "",
          content: kvList([
            ["最新委托状态", readableState(latestOrder?.status || "unknown"), latestOrder?.client_order_id || "暂无委托"],
            ["最新成交", latestFill ? `${formatNumber(latestFill.fill_qty)} @ ${formatNumber(latestFill.fill_price)}` : "-", latestFill?.fill_id || "暂无成交"],
            ["成交落库时间", formatMaybeTimestamp(latestFill?.ingestion_timestamp), formatRelativeAge(latestFill?.ingestion_timestamp)],
            ["最新对账结果", readableState(reconciliation?.severity || "-"), reconciliation?.reconciliation_id || "-"],
          ]),
        })}
      </div>
      <div class="span-6">
        ${surfaceCard({
          title: "最近运行时间线",
          kicker: "时间顺序",
          copy: "按时间把最新决策、委托、成交和对账串起来，方便快速定位问题。",
          content: timeline(buildTimeline({ latestDecision, latestOrder, latestFill, reconciliation }), "最近没有新的运行活动。"),
        })}
      </div>

      <div class="span-12">
        ${surfaceCard({
          title: "核心运行指标",
          kicker: "关键计数",
          copy: "这里只保留最能说明系统是否在稳定工作的指标。",
          content: statGrid([
            { label: "策略轮次", value: formatNumber(metrics.decision_cycle_count), meta: "系统累计完成的策略判断次数" },
            { label: "拟下单次数", value: formatNumber(metrics.order_intent_count), meta: "真正进入执行规划的次数" },
            { label: "活动委托数", value: formatNumber(metrics.current_open_order_count), meta: "仍在活动生命周期里的委托数" },
            { label: "累计成交笔数", value: formatNumber(metrics.fill_count), meta: "已经确认落库的成交笔数" },
            { label: "拒单次数", value: formatNumber(metrics.rejection_count), meta: "被策略、风控或交易所拒绝的次数" },
            { label: "异常对账数", value: formatNumber(metrics.reconciliation_mismatch_count), meta: "当前尚未清理的对账异常" },
          ]),
        })}
      </div>
    </div>
  `;
}

function decisionNarrative(latestDecision, latestOrder) {
  const target = latestDecision.position_target || {};
  const decisionTime = formatRelativeAge(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts);
  if (!latestDecision.decision_id) {
    return "系统最近没有新的策略决策，因此当前更适合先关注账户状态和对账状态。";
  }
  return `${decisionTime}，系统针对 ${latestDecision.decision_context?.symbol || "当前标的"} 形成了 ${overviewIntentLabel(latestDecision)} 的判断。` +
    `${latestDecision.policy_decision?.execution_allowed ? "策略层允许继续执行，" : "策略层没有放行，"}` +
    `${latestDecision.risk_decision?.approved ? "风控层也已通过。" : "风控层仍未通过。"}${latestOrder ? ` 最近一笔订单状态为 ${readableState(latestOrder.status)}。` : ""}`;
}

function trackedPosition(portfolio, symbol) {
  const positions = portfolio.positions || [];
  return positions.find((item) => item.symbol === symbol) || positions[0] || null;
}

function trackedSymbol(runtime, mode) {
  return runtime.symbols?.[0] || mode.default_symbol || "-";
}

function buildTimeline({ latestDecision, latestOrder, latestFill, reconciliation }) {
  return [
    latestDecision?.decision_id
      ? {
          title: "最新决策",
          subtitle: latestDecision.decision_id,
          detail: `${overviewIntentLabel(latestDecision)} | ${latestDecision.decision_context?.symbol || "-"}`,
          timestamp: formatMaybeTimestamp(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts),
          pill: pill("策略", "info"),
        }
      : null,
    latestOrder?.client_order_id
      ? {
          title: "最新订单",
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
          pill: pill("对账", toneForRuntimeState(reconciliation.severity)),
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
