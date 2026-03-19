import { actionButton, callout, kvList, pill, responsiveTable, statGrid, summaryStrip, surfaceCard } from "../components.js";
import { escapeHtml, formatDuration, formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, listOrDash, middleEllipsis } from "../formatters.js";
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
  const strategyHealth = latestDecision.strategy_execution_health || data.metrics?.strategy_execution_health || {};

  return {
    strategyHero: surfaceCard({
      title: "最新策略判断",
      kicker: "主结论",
      copy: "先看结论，再看是否真的准备进入执行。",
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
    strategySignal: surfaceCard({
      title: "信号拆解",
      kicker: "为什么会得出这个结论",
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
          ["基础信号说明", listText(baseline.reasons, "本轮没有额外信号说明"), numberMeta("微观结构强度", baseline.microstructure_alpha, "当前没有微观结构强度")],
          ["策略门禁", policy.execution_allowed ? "本轮允许进入执行" : "策略层未放行", policy.execution_allowed ? listText(policy.allow_reasons, "当前没有额外门禁说明") : listText(policy.blocker_reasons, "当前没有给出具体拦截原因")],
          ["风控结论", risk.approved ? "风控已放行" : "风控仍在拦截", risk.approved ? listText(risk.approval_reasons, "当前没有额外放行说明") : listText(risk.rejection_reasons, "当前没有额外拦截说明")],
        ])}
      `,
    }),
    strategyHealth: surfaceCard({
      title: "执行约束与交易质量",
      kicker: "来回交易 / 手续费拖累 / 胜率",
      copy: "把最近成交质量、冷却状态和保护规则直接摆出来，便于一眼判断当前是不是在无效来回交易。",
      content: `
        ${statGrid([
          {
            label: "最近平仓样本",
            value: formatNumber(strategyHealth.recent_closed_trade_count, 0),
            meta: strategyHealth.latest_fill_timestamp ? `最近成交 ${formatRelativeAge(strategyHealth.latest_fill_timestamp)}` : "最近还没有平仓样本",
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
          ["当前保护规则", listOrDash(strategyHealth.guardrail_flags, "当前没有额外保护规则"), cooldownSummary(strategyHealth.cooldowns)],
          ["最早持仓开始", formatMaybeTimestamp(latestDecision.decision_context?.current_position_opened_at || strategyHealth.current_position_opened_at), holdAge(latestDecision.decision_context?.current_position_opened_at || strategyHealth.current_position_opened_at)],
          ["最近平仓时间", formatMaybeTimestamp(latestDecision.decision_context?.last_position_closed_at || strategyHealth.last_position_closed_at), formatRelativeAge(latestDecision.decision_context?.last_position_closed_at || strategyHealth.last_position_closed_at)],
          ["预期净优势", formatBps(target.expected_net_edge_bps), `信号优势 ${formatBps(target.expected_signal_edge_bps)} / 成本 ${formatBps(target.expected_cost_bps)}`],
          ["本轮执行限制", listOrDash(target.guardrail_flags, "当前没有额外执行限制"), `目标动作 ${readableState(target.position_intent || "hold")}`],
        ])}
      `,
    }),
    strategyHistory: surfaceCard({
      title: "最近决策记录",
      kicker: "时间序列",
      copy: "桌面端保留表格，窄屏自动切成卡片，方便值班时在手机上快速扫读。",
      content: `${responsiveTable(
        decisionTableHeaders(decisionScene),
        recentDecisions.map((item) => [
          `<div><strong>${formatRelativeAge(item.decision_time)}</strong><div class="table-meta">${formatMaybeTimestamp(item.decision_time)}</div></div>`,
          `<div><strong>${item.symbol || "标的待确认"}</strong><div class="table-meta">${item.timeframe || "周期待确认"}</div></div>`,
          `<div><strong>${readableRecentIntent(item)}</strong><div class="table-meta">${recentDecisionNarrative(item, decisionScene)}</div></div>`,
          `<div class="inline-pills">${pill(item.policy_result ? "策略放行" : "策略拦截", item.policy_result ? "positive" : "danger")}${pill(item.risk_result ? "风控通过" : "风控拦截", item.risk_result ? "positive" : "danger")}</div>`,
          item.decision_id ? actionButton("查看", "inspect-decision", item.decision_id) : "",
        ]),
        "最近还没有决策记录。",
        recentDecisions.map((item) => ({
          kicker: "策略记录",
          title: `${readableRecentIntent(item)} | ${item.symbol || "标的待确认"}`,
          meta: `${formatRelativeAge(item.decision_time)} | ${item.timeframe || "周期待确认"}`,
          tone: item.policy_result && item.risk_result ? "positive" : item.risk_result || item.policy_result ? "warning" : "danger",
          badge: `<div class="inline-pills">${pill(item.policy_result ? "策略放行" : "策略拦截", item.policy_result ? "positive" : "danger")}${pill(item.risk_result ? "风控通过" : "风控拦截", item.risk_result ? "positive" : "danger")}</div>`,
          fields: [
            { label: "决策时间", value: formatMaybeTimestamp(item.decision_time), meta: formatRelativeAge(item.decision_time) },
            { label: "决策摘要", value: readableRecentIntent(item), meta: recentDecisionNarrative(item, decisionScene) },
          ],
          details: [
            { label: "标的", value: item.symbol || "标的待确认", meta: item.timeframe || "周期待确认" },
            { label: "策略结果", value: item.policy_result ? "已放行" : "被拦截" },
            { label: "风控结果", value: item.risk_result ? "已通过" : "被拦截" },
            { label: "决策编号", value: item.decision_id || "当前没有编号" },
          ],
          detailLabel: "展开本次决策详情",
          action: item.decision_id ? actionButton("查看", "inspect-decision", item.decision_id) : "",
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
      <div class="span-6">${sections.strategySignal}</div>
      <div class="span-12">${sections.strategyHealth}</div>
      <div class="span-12">${sections.strategyHistory}</div>
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
    return "当前没有新的策略输出，通常是在等待新的市场条件或下一轮决策窗口。";
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
      ? "当前没有持仓和挂单，这轮决策的含义就是继续观察。"
      : currentQty !== 0 && targetQty === currentQty
        ? "这轮决策没有要求增减仓，表示继续维持当前仓位。"
        : openOrders.length > 0
          ? "当前已经有在途委托，这轮主要是在维持既有执行状态。"
          : `这轮决策给出了 ${intentLabel} 的交易结论。`;
  return `当前市场状态为 ${regimeLabel}。${actionSentence}`
    + `${policy.execution_allowed ? "策略层已放行，" : "策略层仍未放行，"}`
    + `${risk.approved ? "风控层当前没有继续阻断。" : `风控仍在拦截：${listOrDash(risk.rejection_reasons, "当前没有额外风控说明")}。`}`;
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
  return listOrDash(value, fallback);
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
