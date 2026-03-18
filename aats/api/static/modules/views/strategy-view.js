import { actionButton, callout, kvList, pill, statGrid, surfaceCard, table } from "../components.js";
import { formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, listOrDash } from "../formatters.js";
import { readableState } from "../terms.js";
import { decisionTableHeaders, inferTradeScene } from "../trade-display.js";

export function renderStrategySections(data) {
  const latestDecision = data.latestDecision || {};
  const recentPayload = data.recentDecisions || {};
  const recentDecisions = recentPayload.decisions || [];
  const executionLatest = data.executionLatest || {};
  const baseline = latestDecision.baseline_assessment || {};
  const ai = latestDecision.ai_assessment || {};
  const target = latestDecision.position_target || {};
  const policy = latestDecision.policy_decision || {};
  const risk = latestDecision.risk_decision || {};
  const intentLabel = readableIntent(latestDecision);
  const regimeLabel = readableRegime(latestDecision);
  const decisionScene = inferDecisionScene(latestDecision, recentDecisions);

  return {
    strategyHero: surfaceCard({
      title: "最新策略判断",
      kicker: "主结论",
      copy: "把最新一轮策略结论翻译成交易语言：当前市场偏什么状态、系统现在想做什么、是否准备真正进入执行。",
      classes: "hero-card",
      actions: latestDecision.decision_id ? actionButton("查看完整决策链", "inspect-decision", latestDecision.decision_id) : "",
      content: `
        ${callout({
          title: latestDecision.decision_id ? `当前策略结论：${intentLabel}` : "最近还没有新的策略输出",
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
            meta: readableState(target.target_exposure_side || target.position_intent),
          },
          {
            label: decisionScene === "derivatives" ? "当前净仓位" : "当前持仓",
            value: formatSigned(target.current_position_qty),
            meta: decisionScene === "derivatives" ? "以净仓位口径展示" : "以现货持仓口径展示",
          },
          {
            label: "最新决策时间",
            value: formatMaybeTimestamp(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts),
            meta: formatRelativeAge(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts),
          },
          {
            label: "最近执行结果",
            value: readableState(executionLatest.latest_order?.status || "unknown"),
            meta: executionLatest.latest_order?.client_order_id || "暂无委托",
          },
        ])}
      `,
    }),
    strategySignal: surfaceCard({
      title: "信号拆解",
      kicker: "为什么会得出这个结论",
      copy: "把最新决策拆成基线信号、AI 参考、目标仓位、策略门禁和风控门禁，方便直接定位阻断点或误判点。",
      content: kvList([
        ["交易场景", decisionScene === "derivatives" ? "合约" : "现货", decisionScene === "derivatives" ? readableState(target.margin_mode || "-") : "现金买卖"],
        ["基线信号置信度", formatNumber(baseline.confidence), listOrDash(baseline.reasons)],
        ["综合 alpha", formatNumber(baseline.composite_alpha_score), `微观结构 ${formatNumber(baseline.microstructure_alpha)}`],
        ["AI 参考结论", readableState(ai.summary || ai.direction_bias || "-"), `AI 置信度 ${formatNumber(ai.confidence)}`],
        [
          decisionScene === "derivatives" ? "目标净仓位" : "目标持仓",
          formatSigned(target.target_position_qty),
          decisionScene === "derivatives" ? `目标杠杆 ${formatNumber(target.target_leverage)}` : readableState(target.target_exposure_side || "-"),
        ],
        ["策略门禁原因", listOrDash(policy.blocker_reasons), policy.execution_allowed ? "本轮允许进入执行" : "策略层未放行"],
        ["风控拦截原因", listOrDash(risk.rejection_reasons), risk.approved ? "风控已放行" : "风控仍在阻断"],
      ]),
    }),
    strategyHistory: surfaceCard({
      title: "最近决策记录",
      kicker: "时间序列",
      copy: "只看最近几轮策略判断，观察系统是在继续观望、逐步建仓，还是开始频繁反复。可继续加载更多历史。",
      content: `${table(
        decisionTableHeaders(decisionScene),
        recentDecisions.map((item) => [
          `<div><strong>${formatRelativeAge(item.decision_time)}</strong><div class="table-meta">${formatMaybeTimestamp(item.decision_time)}</div></div>`,
          `<div><strong>${item.symbol || "-"}</strong><div class="table-meta">${item.timeframe || "-"}</div></div>`,
          `<div><strong>${readableRecentIntent(item)}</strong><div class="table-meta">${recentDecisionNarrative(item, decisionScene)}</div></div>`,
          `<div class="inline-pills">${pill(item.policy_result ? "策略放行" : "策略拦截", item.policy_result ? "positive" : "danger")}${pill(item.risk_result ? "风控通过" : "风控拦截", item.risk_result ? "positive" : "danger")}</div>`,
          item.decision_id ? actionButton("查看", "inspect-decision", item.decision_id) : "",
        ]),
        "最近还没有决策记录。"
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
      <div class="span-7">${sections.strategyHero}</div>
      <div class="span-5">${sections.strategySignal}</div>
      <div class="span-12">${sections.strategyHistory}</div>
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
      <p class="meta-copy">当前展示 ${shown} / ${total} 条${singular}。</p>
      <div class="stack-actions">
        ${hasMore ? actionButton(`加载更多${singular}`, loadAction, "", "secondary") : ""}
        ${limit > 8 ? actionButton("收起到最新 8 条", collapseAction, "", "ghost") : ""}
      </div>
    </div>
  `;
}

function strategyNarrative(detail) {
  if (!detail.decision_id) {
    return "系统当前没有新的策略输出，通常表示还在等待新的市场条件、触发信号或下一轮决策窗口。";
  }
  const target = detail.position_target || {};
  const policy = detail.policy_decision || {};
  const risk = detail.risk_decision || {};
  const intentLabel = readableIntent(detail);
  const regimeLabel = readableRegime(detail);
  const currentQty = Number(target.current_position_qty ?? detail.decision_context?.current_position_qty ?? 0);
  const targetQty = Number(target.target_position_qty ?? 0);
  const openOrders = Array.isArray(detail.decision_context?.current_open_orders) ? detail.decision_context.current_open_orders : [];
  const actionSentence =
    intentLabel === "继续观望"
      ? "当前没有持仓，也没有挂单，所以这轮决策的真实含义是继续观望、暂不下单。"
      : currentQty !== 0 && targetQty === currentQty
        ? "这轮决策没有要求增减仓，表示继续维持当前仓位。"
        : openOrders.length > 0
          ? "当前已经有在途委托，本轮主要是在维持既有执行状态。"
          : `这轮决策给出了 ${intentLabel} 的交易结论。`;
  return `系统在 ${regimeLabel} 市场状态下，${actionSentence}` +
    `${policy.execution_allowed ? "策略门禁已允许进入执行阶段，" : "策略门禁仍未放行，"}` +
    `${risk.approved ? "风控层没有继续阻断。" : `风控仍在拦截：${listOrDash(risk.rejection_reasons)}。`}`;
}

function recentDecisionNarrative(item, scene) {
  const delta = item.delta_position_qty ?? item.target_delta_qty;
  if (delta === null || delta === undefined) {
    return scene === "derivatives" ? "没有新的净仓位调整" : "没有新的持仓调整";
  }
  return `${scene === "derivatives" ? "净仓位变化" : "持仓变化"} ${formatSigned(delta)} | ${item.decision_id || "-"}`;
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
