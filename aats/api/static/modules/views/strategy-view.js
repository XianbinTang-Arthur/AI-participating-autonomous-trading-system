import { actionButton, callout, kvList, pill, statGrid, surfaceCard, table } from "../components.js";
import { formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, listOrDash } from "../formatters.js";
import { readableState, toneForRuntimeState } from "../terms.js";

export function renderStrategyView(data) {
  const latestDecision = data.latestDecision || {};
  const recentDecisions = data.recentDecisions?.decisions || [];
  const executionLatest = data.executionLatest || {};
  const baseline = latestDecision.baseline_assessment || {};
  const ai = latestDecision.ai_assessment || {};
  const target = latestDecision.position_target || {};
  const policy = latestDecision.policy_decision || {};
  const risk = latestDecision.risk_decision || {};

  return `
    <div class="panel-grid">
      <div class="span-7">
        ${surfaceCard({
          title: "最新策略解释",
          kicker: "决策主叙事",
          copy: "把最新一轮策略的判断拆成可读的交易语言，而不是只看一串工程字段。",
          classes: "hero-card",
          actions: latestDecision.decision_id ? actionButton("查看完整决策链", "inspect-decision", latestDecision.decision_id) : "",
          content: `
            ${callout({
              title: latestDecision.decision_id ? `系统当前倾向：${readableState(target.position_intent || "hold")}` : "最近还没有决策输出",
              copy: strategyNarrative(latestDecision),
              pills: [
                pill(`市场状态：${readableState(baseline.market_regime || latestDecision.decision_context?.market_regime || "unknown")}`, "info"),
                pill(`策略放行：${readableState(policy.execution_allowed ? "ready" : "blocked")}`, policy.execution_allowed ? "positive" : "danger"),
                pill(`风控：${readableState(risk.approved ? "ready" : "blocked")}`, risk.approved ? "positive" : "danger"),
              ],
            })}
            ${statGrid([
              { label: "目标仓位变化", value: formatSigned(target.delta_position_qty), meta: readableState(target.target_exposure_side || target.position_intent) },
              { label: "当前仓位", value: formatSigned(target.current_position_qty), meta: "最新决策看到的当前仓位" },
              { label: "最新决策时间", value: formatMaybeTimestamp(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts), meta: formatRelativeAge(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts) },
              { label: "最近执行结果", value: readableState(executionLatest.latest_order?.status || "unknown"), meta: executionLatest.latest_order?.client_order_id || "暂无订单" },
            ])}
          `,
        })}
      </div>

      <div class="span-5">
        ${surfaceCard({
          title: "信号组成",
          kicker: "策略拆解",
          copy: "把最新决策拆成 baseline、AI、目标仓位、策略门禁和风控门禁。",
          content: kvList([
            ["baseline 置信度", formatNumber(baseline.confidence), listOrDash(baseline.reasons)],
            ["综合 alpha", formatNumber(baseline.composite_alpha_score), `微观结构 ${formatNumber(baseline.microstructure_alpha)}`],
            ["AI 结论", readableState(ai.summary || ai.direction_bias || "-"), `AI 置信度 ${formatNumber(ai.confidence)}`],
            ["目标仓位", formatSigned(target.target_position_qty), `目标杠杆 ${formatNumber(target.target_leverage)}`],
            ["策略阻断原因", listOrDash(policy.blocker_reasons), policy.execution_allowed ? "本轮已放行" : "策略层未放行"],
            ["风控拒绝原因", listOrDash(risk.rejection_reasons), risk.approved ? "风控已通过" : "风控仍在阻断"],
          ]),
        })}
      </div>

      <div class="span-12">
        ${surfaceCard({
          title: "最近决策记录",
          kicker: "时间序列",
          copy: "这里展示最近几轮策略判断，帮助观察系统是否在频繁反复、持续持有，还是正在等待条件成熟。",
          content: table(
            ["时间", "标的 / 周期", "策略判断", "策略与风控", "查看"],
            recentDecisions.map((item) => [
              `<div><strong>${formatRelativeAge(item.decision_time)}</strong><div class="table-meta">${formatMaybeTimestamp(item.decision_time)}</div></div>`,
              `<div><strong>${item.symbol || "-"}</strong><div class="table-meta">${item.timeframe || "-"}</div></div>`,
              `<div><strong>${readableState(item.position_intent || item.target_exposure_side || "hold")}</strong><div class="table-meta">${recentDecisionNarrative(item)}</div></div>`,
              `<div class="inline-pills">${pill(item.policy_result ? "策略放行" : "策略阻断", item.policy_result ? "positive" : "danger")}${pill(item.risk_result ? "风控通过" : "风控阻断", item.risk_result ? "positive" : "danger")}</div>`,
              item.decision_id ? actionButton("查看", "inspect-decision", item.decision_id) : "",
            ]),
            "最近还没有决策记录。"
          ),
        })}
      </div>
    </div>
  `;
}

function strategyNarrative(detail) {
  if (!detail.decision_id) {
    return "系统当前没有新的策略输出，通常表示还在等待新的市场条件、决策触发条件，或运行中尚未形成新的信号。";
  }
  const baseline = detail.baseline_assessment || {};
  const target = detail.position_target || {};
  const policy = detail.policy_decision || {};
  const risk = detail.risk_decision || {};
  return `系统在 ${readableState(baseline.market_regime || detail.decision_context?.market_regime || "unknown")} 市场状态下，` +
    `给出了 ${readableState(target.position_intent || "hold")} 的方向判断。` +
    `${policy.execution_allowed ? "策略层已允许进入执行阶段，" : "策略层仍未放行，"}` +
    `${risk.approved ? "风控层未继续阻断。" : `风控仍在拦截：${listOrDash(risk.rejection_reasons)}。`}`;
}

function recentDecisionNarrative(item) {
  const delta = item.delta_position_qty ?? item.target_delta_qty;
  const deltaText = delta === null || delta === undefined ? "无仓位变化" : `目标变化 ${formatSigned(delta)}`;
  return `${deltaText} | ${item.decision_id || "-"}`;
}
