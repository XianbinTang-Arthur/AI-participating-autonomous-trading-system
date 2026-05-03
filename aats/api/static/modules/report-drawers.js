import { kvList, responsiveTable, surfaceCard, summaryStrip } from "./components.js";
import {
  escapeHtml,
  formatMaybeTimestamp,
  formatNumber,
  formatRelativeAge,
  formatSigned,
  rawJson,
} from "./formatters.js";
import { readableState } from "./terms.js";

export function buildDecisionHistoryDrawer(payload = {}) {
  const decisions = Array.isArray(payload.decisions) ? payload.decisions : [];
  return {
    eyebrow: "策略决策历史",
    title: "最近决策明细",
    summary: `当前按需加载 ${formatNumber(decisions.length, 0, "0")} 条最近决策，不占用首屏刷新链路。`,
    body: [
      surfaceCard({
        title: "记录概览",
        content: summaryStrip([
          {
            label: "已加载",
            value: formatNumber(decisions.length, 0, "0"),
            meta: `总量 ${formatNumber(payload.total_available, 0, "0")}`,
            tone: "info",
          },
          {
            label: "分页",
            value: `${formatNumber(payload.offset, 0, "0")} / ${formatNumber(payload.limit, 0, "0")}`,
            meta: payload.has_more ? "仍有更早记录" : "当前页已到末尾",
            tone: payload.has_more ? "warning" : "positive",
          },
        ]),
      }),
      surfaceCard({
        title: "最近决策",
        content: responsiveTable(
          ["时间", "标的", "意图", "结果", "编号"],
          decisions.map((item) => [
            `<div><strong>${formatRelativeAge(item.decision_time)}</strong><div class="table-meta">${formatMaybeTimestamp(item.decision_time)}</div></div>`,
            `<div><strong>${escapeHtml(item.symbol || "标的待确认")}</strong><div class="table-meta">${escapeHtml(item.timeframe || "周期待确认")}</div></div>`,
            escapeHtml(readableState(item.intent || item.book_action || item.target_action || "unknown", "意图待确认")),
            `${item.policy_result ? "策略允许" : "策略拦截"} / ${item.risk_result ? "风控允许" : "风控拦截"}`,
            escapeHtml(item.decision_id || "当前没有编号"),
          ]),
          "当前暂无决策记录。"
        ),
      }),
      surfaceCard({
        title: "排障原文",
        kicker: "默认折叠",
        content: rawJson(payload),
      }),
    ].join(""),
  };
}

export function buildStrategyAttributionDrawer(payload = {}) {
  const summary = payload.summary || {};
  const bySleeve = Array.isArray(payload.profitability_by_strategy_sleeve)
    ? payload.profitability_by_strategy_sleeve
    : [];
  const byBundle = Array.isArray(payload.profitability_by_strategy_bundle)
    ? payload.profitability_by_strategy_bundle
    : [];
  return {
    eyebrow: "策略归因明细",
    title: "完整归因报表",
    summary: "这里按需加载完整归因报表，用于排查收益、资金费和库存归属，不进入首屏 bundle。",
    body: [
      surfaceCard({
        title: "汇总",
        content: kvList([
          ["归因记录", formatNumber(summary.sleeve_pnl_record_count, 0, "0"), "参与本次归因的 sleeve PnL 记录数"],
          ["组合净收益", formatSigned(summary.combined_net_realized_pnl), `资金费 ${formatSigned(summary.funding_fee_net_pnl)}`],
          ["库存最大子策略", summary.top_inventory_sleeve_id || "当前没有库存", `名义 ${formatSigned(summary.top_inventory_notional)}`],
          ["受保护成交", formatNumber(summary.protected_fill_count, 0, "0"), `未受保护 ${formatNumber(summary.unprotected_fill_count, 0, "0")} 笔`],
        ]),
      }),
      surfaceCard({
        title: "子策略收益",
        content: responsiveTable(
          ["子策略", "净收益", "实现收益", "资金费", "记录数"],
          bySleeve.map((item) => [
            escapeHtml(item.strategy_sleeve_id || "未归属"),
            formatSigned(item.combined_net_realized_pnl),
            formatSigned(item.realized_pnl),
            formatSigned(item.funding_fee_amount),
            formatNumber(item.record_count, 0, "0"),
          ]),
          "当前还没有子策略收益归因。"
        ),
      }),
      surfaceCard({
        title: "执行包收益",
        content: responsiveTable(
          ["执行包", "净收益", "实现收益", "资金费", "记录数"],
          byBundle.map((item) => [
            escapeHtml(item.strategy_bundle_id || item.bundle_id || "未归属"),
            formatSigned(item.combined_net_realized_pnl),
            formatSigned(item.realized_pnl),
            formatSigned(item.funding_fee_amount),
            formatNumber(item.record_count, 0, "0"),
          ]),
          "当前还没有执行包归因。"
        ),
      }),
      surfaceCard({
        title: "排障原文",
        kicker: "默认折叠",
        content: rawJson(payload),
      }),
    ].join(""),
  };
}

export function buildTrialReviewDetailsDrawer(payload = {}) {
  const sections = payload.sections || {};
  const forwardValidation = sections.forward_validation || {};
  const forwardSummary = forwardValidation.summary || {};
  const periods = Array.isArray(forwardValidation.periods) ? forwardValidation.periods : [];
  const latestPeriod = periods[0] || {};
  const scalingReadiness = sections.scaling_readiness || {};
  const summary = payload.summary || {
    readiness: scalingReadiness.readiness || forwardSummary.verdict,
    verdict: forwardSummary.verdict,
    headline: scalingReadiness.summary || forwardSummary.summary,
    combined_net_realized_pnl: latestPeriod.combined_net_realized_pnl,
    closed_fill_count: latestPeriod.closed_fill_count,
    fee_to_notional_ratio: latestPeriod.fee_to_notional_ratio,
  };
  const anomalies = Array.isArray(sections.execution_anomalies) ? sections.execution_anomalies : [];
  return {
    eyebrow: "试盘复盘明细",
    title: "Trial Review 分段明细",
    summary: "这里按需加载试盘分段、异常和验证周期，用于深度复盘，不占用策略页首屏刷新。",
    body: [
      surfaceCard({
        title: "复盘摘要",
        content: kvList([
          ["建议", readableState(summary.readiness || summary.verdict || "unknown", "建议待确认"), summary.headline || "当前没有摘要说明"],
          ["综合净收益", formatSigned(summary.combined_net_realized_pnl), `成交 ${formatNumber(summary.closed_fill_count, 0, "0")} 笔`],
          ["费用拖累", formatNumber(summary.fee_to_notional_ratio, 4, "0"), "按最近验证窗口计算"],
        ]),
      }),
      surfaceCard({
        title: "验证周期",
        content: responsiveTable(
          ["周期", "净收益", "成交数", "费用拖累"],
          periods.map((item) => [
            `${formatMaybeTimestamp(item.window_start)} - ${formatMaybeTimestamp(item.window_end)}`,
            formatSigned(item.combined_net_realized_pnl),
            formatNumber(item.closed_fill_count, 0, "0"),
            formatNumber(item.fee_to_notional_ratio, 4, "0"),
          ]),
          "当前没有验证周期明细。"
        ),
      }),
      surfaceCard({
        title: "执行异常",
        content: responsiveTable(
          ["类型", "标的", "影响", "时间"],
          anomalies.map((item) => [
            escapeHtml(item.anomaly_type || item.reason || "异常待确认"),
            escapeHtml(item.symbol || "标的待确认"),
            escapeHtml(item.impact || item.summary || "影响待确认"),
            formatMaybeTimestamp(item.observed_at || item.timestamp),
          ]),
          "当前没有执行异常明细。"
        ),
      }),
      surfaceCard({
        title: "排障原文",
        kicker: "默认折叠",
        content: rawJson(payload),
      }),
    ].join(""),
  };
}
