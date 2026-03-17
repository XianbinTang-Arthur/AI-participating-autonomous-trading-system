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

  const currentPosition = trackedPosition(portfolio, runtime.symbols?.[0] || mode.default_symbol);

  return `
    <div class="panel-grid">
      <div class="span-8">
        ${surfaceCard({
          title: "当前交易结论",
          kicker: "首要关注",
          copy: "先看系统是否允许交易，再看最新策略想做什么，以及这一轮决策是否真的需要下单。",
          classes: "hero-card",
          actions: latestDecision.decision_id ? actionButton("查看决策详情", "inspect-decision", latestDecision.decision_id) : "",
          content: `
            ${callout({
              title: latestDecision.position_target?.position_intent ? `最新动作：${readableState(latestDecision.position_target.position_intent)}` : "当前没有新的交易动作",
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
              { label: "策略放行", value: booleanWord(latestDecision.policy_decision?.execution_allowed), meta: listOrDash(latestDecision.policy_decision?.blocker_reasons) },
              { label: "风控结果", value: booleanWord(latestDecision.risk_decision?.approved), meta: listOrDash(latestDecision.risk_decision?.rejection_reasons) },
            ])}
          `,
        })}
      </div>
      <div class="span-4">
        ${surfaceCard({
          title: "当前风险与阻断",
          kicker: "人工介入提示",
          copy: blockers.length ? "下面这些原因正在影响交易资格。" : "当前没有阻断项，系统没有被明显卡住。",
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
                ["是否已暂停", booleanWord(health.halted), "人工 kill switch"],
                ["是否允许恢复交易", booleanWord(recovery.resume_eligible), listOrDash(recovery.resume_blocked_reasons)],
              ]),
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "账户权益与收益",
          kicker: "资金概览",
          copy: "用更接近交易者的语言查看权益、收益和当前主仓位。",
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
          copy: currentPosition ? "这里显示正在跟踪的主仓位。" : "当前没有检测到主仓位。",
          content: kvList([
            ["跟踪标的", trackedSymbol(runtime, mode), "当前主交易标的"],
            ["仓位方向", readableState(currentPosition?.exposure_side || "flat"), currentPosition ? "来自最新 portfolio snapshot" : "暂无持仓"],
            ["仓位数量", formatSigned(currentPosition?.position_qty), currentPosition ? `目标杠杆 ${formatNumber(currentPosition?.target_leverage)}` : ""],
            ["平均开仓价", formatNumber(currentPosition?.average_entry_price), currentPosition ? `最新价格 ${formatNumber(currentPosition?.mark_price)}` : ""],
          ]),
        })}
      </div>
      <div class="span-4">
        ${surfaceCard({
          title: "恢复与基线",
          kicker: "可信状态",
          copy: "当系统进入恢复、复核或重建基线流程时，应在这里快速看懂当前阶段。",
          content: kvList([
            ["恢复状态", readableState(recovery.recovery_state), recovery.safe_to_trade ? "已满足继续交易条件" : "当前仍受限制"],
            ["是否需要人工确认", booleanWord(recovery.review_required), listOrDash(recovery.resume_blocked_reasons)],
            ["基线状态", readableState(runtime.baseline_takeover?.status), readableState(runtime.baseline_takeover?.baseline_kind)],
            ["最近重建基线", formatMaybeTimestamp(runtime.baseline_takeover?.last_rebaseline_at), runtime.baseline_takeover?.last_rebaseline_event_ref || "-"],
          ]),
        })}
      </div>

      <div class="span-6">
        ${surfaceCard({
          title: "最新执行进展",
          kicker: "订单与成交",
          copy: "快速查看最新订单、最新成交和对账状态，不必先切到执行页。",
          actions: latestOrder?.client_order_id ? actionButton("查看订单详情", "inspect-order", latestOrder.client_order_id) : "",
          content: kvList([
            ["最新订单状态", readableState(latestOrder?.status || "unknown"), latestOrder?.client_order_id || "暂无订单"],
            ["最新成交", latestFill ? `${formatNumber(latestFill.fill_qty)} @ ${formatNumber(latestFill.fill_price)}` : "-", latestFill?.fill_id || "暂无成交"],
            ["成交写入时间", formatMaybeTimestamp(latestFill?.ingestion_timestamp), formatRelativeAge(latestFill?.ingestion_timestamp)],
            ["最新对账结果", readableState(reconciliation?.severity || "-"), reconciliation?.reconciliation_id || "-"],
          ]),
        })}
      </div>
      <div class="span-6">
        ${surfaceCard({
          title: "最近运行时间线",
          kicker: "时间顺序",
          copy: "用时间线把最新决策、最新订单、最新成交和最新对账串起来。",
          content: timeline(buildTimeline({ latestDecision, latestOrder, latestFill, reconciliation }), "最近没有新的运行活动。"),
        })}
      </div>

      <div class="span-12">
        ${surfaceCard({
          title: "核心运行指标",
          kicker: "运行密度",
          copy: "这里只保留真正能帮助判断系统是否在稳定工作的指标。",
          content: statGrid([
            { label: "决策轮次", value: formatNumber(metrics.decision_cycle_count), meta: "系统累计完成的决策轮次" },
            { label: "订单意图数", value: formatNumber(metrics.order_intent_count), meta: "真正走到执行规划阶段的次数" },
            { label: "当前活动订单数", value: formatNumber(metrics.current_open_order_count), meta: "仍在活动生命周期里的订单数" },
            { label: "成交数", value: formatNumber(metrics.fill_count), meta: "已经确认写入的 fill 数量" },
            { label: "拒单数", value: formatNumber(metrics.rejection_count), meta: "被策略、风控或交易所拒绝的次数" },
            { label: "对账异常数", value: formatNumber(metrics.reconciliation_mismatch_count), meta: "当前仍未清理的对账异常" },
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
  return `${decisionTime}，系统针对 ${latestDecision.decision_context?.symbol || "当前标的"} 形成了 ${readableState(target.position_intent || "hold")} 的判断。` +
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
          detail: `${readableState(latestDecision.position_target?.position_intent || "hold")} | ${latestDecision.decision_context?.symbol || "-"}`,
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
