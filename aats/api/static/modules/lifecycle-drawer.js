import { callout, kvList, pill, responsiveTable, surfaceCard, timeline } from "./components.js";
import {
  escapeHtml,
  formatDuration,
  formatMaybeTimestamp,
  formatNumber,
  formatRelativeAge,
  formatSigned,
  rawJson,
} from "./formatters.js";
import { localizeError, readableState } from "./terms.js";

const TRANSITION_CATEGORY_LABELS = {
  strategy_exit: "策略性退出",
  protective_exit: "保护性退出",
  execution_guard_exit: "执行守护退出",
};

const TRANSITION_CATEGORY_TONES = {
  strategy_exit: "info",
  protective_exit: "warning",
  execution_guard_exit: "danger",
};

export function buildLifecycleAttributionDrawer(detail = {}) {
  const summary = detail.summary || {};
  const lifecycleId = summary.lifecycle_id || detail.lifecycle_id || "";
  const symbol = summary.symbol || "标的待确认";
  const direction = readableState(summary.direction || summary.pos_side || "unknown", "方向待确认");
  const combinedNet = formatSigned(summary.combined_net_realized_pnl);
  const holdText = formatDuration(summary.hold_seconds, "持有时长待确认");
  return {
    eyebrow: "仓位生命周期诊断",
    title: lifecycleId ? `当前记录：${lifecycleId}` : "当前记录：仓位生命周期诊断",
    summary: `${symbol} ${direction} 仓位持有 ${holdText}，最终综合净收益 ${combinedNet}。这里按整笔仓位口径展示，不是单笔委托口径。`,
    body: [
      surfaceCard({
        title: "整笔仓位摘要",
        content: kvList(lifecycleSummaryRows(summary, detail)),
      }),
      surfaceCard({
        title: "退出链诊断",
        content: [
          renderTraceCompletenessCallout(detail),
          timeline(decisionTraceItems(detail.decision_trace || []), "当前还没有强关联退出链诊断。"),
        ].join(""),
      }),
      (detail.candidate_decisions || []).length
        ? surfaceCard({
            title: "弱关联候选决策",
            content: timeline(candidateDecisionItems(detail.candidate_decisions || []), "当前没有弱关联候选决策。"),
          })
        : "",
      surfaceCard({
        title: "关键事件时间线",
        content: timeline(keyMetricsTimelineItems(detail.key_metrics_timeline || []), "当前还没有关键事件时间线。"),
      }),
      surfaceCard({
        title: "子成交明细",
        content: renderChildFillTable(detail.child_fills || []),
      }),
      surfaceCard({
        title: "排障原文",
        kicker: "默认折叠",
        copy: "普通值班只看上面的摘要；只有排查字段来源时再展开这里。",
        content: rawJson(detail),
      }),
    ].join(""),
  };
}

function lifecycleSummaryRows(summary = {}, detail = {}) {
  return [
    [
      "标的 / 周期",
      summary.symbol || "标的待确认",
      [
        readableState(summary.direction || summary.pos_side || "unknown", "方向待确认"),
        summary.timeframe || "周期待确认",
        readableState(summary.family || "unknown", summary.family || "策略家族待确认"),
      ].join(" | "),
    ],
    [
      "生命周期 / 仓位键",
      summary.lifecycle_id || "当前没有生命周期编号",
      summary.position_key || "当前没有仓位键",
    ],
    [
      "开仓 / 平仓",
      formatMaybeTimestamp(summary.opened_at),
      summary.closed_at
        ? `${formatMaybeTimestamp(summary.closed_at)} | 持有 ${formatDuration(summary.hold_seconds, "时长待确认")}`
        : `当前仍未闭合 | 持有 ${formatDuration(summary.hold_seconds, "时长待确认")}`,
    ],
    [
      "综合净收益",
      formatSigned(summary.combined_net_realized_pnl),
      `毛收益 ${formatSigned(summary.gross_realized_pnl)} | 交易净收益 ${formatSigned(summary.net_realized_pnl)}`,
    ],
    [
      "费用拆分",
      formatSigned(summary.total_fee_quote),
      `开仓 ${formatSigned(summary.entry_fee_quote)} | 平仓 ${formatSigned(summary.exit_fee_quote)} | 资金费 ${formatSigned(summary.funding_fee_quote)}`,
    ],
    [
      "执行拆分",
      `${formatNumber(summary.entry_fill_count, 0, "0")} 开 / ${formatNumber(summary.exit_fill_count, 0, "0")} 平`,
      `${formatNumber(summary.child_order_count, 0, "0")} 个子委托 | ${formatNumber(summary.decision_trace_count, 0, "0")} 条决策链记录`,
    ],
    [
      "退出链完整性",
      traceCompletenessLabel(detail.trace_completeness || summary.trace_completeness),
      `缺失强关联 ${formatNumber(detail.missing_linked_reference_count ?? summary.missing_linked_reference_count, 0, "0")} 条 | 弱关联候选 ${formatNumber(detail.unmatched_actionable_decision_count ?? summary.unmatched_actionable_decision_count, 0, "0")} 条`,
    ],
    [
      "毛转净捕获率",
      ratioText(summary.gross_to_net_capture_ratio),
      `子执行尝试 ${formatNumber(summary.child_execution_count, 0, "0")} | 状态 ${readableState(summary.status || "unknown")}`,
    ],
    [
      "退出归因",
      exitReasonSummary(detail.exit_reason_breakdown || []),
      exitIntentSummary(detail.exit_intent_breakdown || []),
    ],
  ];
}

function decisionTraceItems(rows = []) {
  return rows.map((row) => {
    const transition = String(row?.transition_category || "").trim();
    const detailParts = [
      `预期净边际 ${bpsText(row?.expected_lifecycle_net_edge_bps)}`,
      `生命周期成本 ${bpsText(row?.expected_lifecycle_cost_bps)}`,
      row?.execution_health_state ? `执行健康 ${readableState(row.execution_health_state, row.execution_health_state)}` : "",
      row?.fee_drag_ratio !== undefined && row?.fee_drag_ratio !== null ? `手续费拖累 ${formatNumber(row.fee_drag_ratio, 3)}` : "",
      row?.churn_ratio !== undefined && row?.churn_ratio !== null ? `来回交易 ${formatNumber(row.churn_ratio, 3)}` : "",
      row?.close_notional_quote !== undefined && row?.close_notional_quote !== null ? `本次收口名义 ${formatSigned(row.close_notional_quote)}` : "",
      row?.residual_notional_quote !== undefined && row?.residual_notional_quote !== null ? `残余名义 ${formatSigned(row.residual_notional_quote)}` : "",
      row?.expectancy_scope === "decision_fallback" ? "生命周期成本口径暂回退到决策级估计" : "",
    ].filter(Boolean);
    return {
      title: localizeError(row?.close_reason || row?.book_action || "unknown"),
      subtitle: [
        readableState(row?.book_state || "unknown", "状态待确认"),
        readableState(row?.book_action || "unknown", "动作待确认"),
        `仓位 ${formatNumber(row?.position_qty_before, 4, "待确认")} -> ${formatNumber(row?.position_qty_after, 4, "待确认")}`,
      ].join(" | "),
      detail: detailParts.join(" | "),
      timestamp: timelineTimestamp(row?.timestamp),
      pill: pill(
        transitionCategoryLabel(transition),
        TRANSITION_CATEGORY_TONES[transition] || "info",
      ),
    };
  });
}

function candidateDecisionItems(rows = []) {
  return rows.map((row) => ({
    title: localizeError(row?.close_reason || row?.book_action || "unknown"),
    subtitle: [
      readableState(row?.book_state || "unknown", "状态待确认"),
      readableState(row?.book_action || "unknown", "动作待确认"),
      row?.timeframe || "周期待确认",
    ].join(" | "),
    detail: [
      "与成交缺少强关联，仅按同标的同方向时间窗命中",
      `预期净边际 ${bpsText(row?.expected_lifecycle_net_edge_bps ?? row?.expected_net_edge_bps)}`,
      `生命周期成本 ${bpsText(row?.expected_lifecycle_cost_bps ?? row?.expected_cost_bps)}`,
      row?.execution_health_state ? `执行健康 ${readableState(row.execution_health_state, row.execution_health_state)}` : "",
      row?.fee_drag_ratio !== undefined && row?.fee_drag_ratio !== null ? `手续费拖累 ${formatNumber(row.fee_drag_ratio, 3)}` : "",
      row?.execution_chain_id ? `执行链 ${row.execution_chain_id}` : "",
    ].filter(Boolean).join(" | "),
    timestamp: timelineTimestamp(row?.timestamp),
    pill: pill("候选", "warning"),
  }));
}

function keyMetricsTimelineItems(rows = []) {
  return rows.map((row) => {
    const eventType = String(row?.event_type || "").trim().toLowerCase();
    if (eventType === "decision") {
      return {
        title: `退出判断：${localizeError(row?.close_reason || row?.book_action || "unknown")}`,
        subtitle: [
          readableState(row?.transition_category || "unknown", transitionCategoryLabel(row?.transition_category)),
          `仓位 ${formatNumber(row?.position_qty_before, 4, "待确认")} -> ${formatNumber(row?.position_qty_after, 4, "待确认")}`,
        ].join(" | "),
        detail: [
          `生命周期净边际 ${bpsText(row?.expected_net_edge_bps)}`,
          row?.execution_health_state ? `执行健康 ${readableState(row.execution_health_state, row.execution_health_state)}` : "",
        ].filter(Boolean).join(" | "),
        timestamp: timelineTimestamp(row?.timestamp),
        pill: pill("决策", "info"),
      };
    }
    if (eventType === "funding_fee") {
      return {
        title: "资金费",
        subtitle: `账单 ${row?.bill_id || "当前没有账单编号"}`,
        detail: `金额 ${formatSigned(row?.amount)} | ${readableState(row?.direction || "unknown", row?.direction || "方向待确认")}`,
        timestamp: timelineTimestamp(row?.timestamp),
        pill: pill("资金费", "warning"),
      };
    }
    return {
      title: `成交：${fillBucketLabel(row?.fill_bucket)}`,
      subtitle: `${localizeError(row?.position_intent || "unknown")} | 名义 ${formatSigned(row?.fill_notional_quote)}`,
      detail: [
        `费用 ${formatSigned(row?.fee_quote)}`,
        `已实现 ${formatSigned(row?.realized_pnl_delta)}`,
        `毛收益 ${formatSigned(row?.gross_realized_pnl)}`,
      ].join(" | "),
      timestamp: timelineTimestamp(row?.timestamp),
      pill: pill(fillBucketLabel(row?.fill_bucket), row?.fill_bucket === "entry" ? "positive" : "warning"),
    };
  });
}

function renderChildFillTable(rows = []) {
  return responsiveTable(
    ["成交", "数量 / 价格", "费用 / 盈亏", "仓位变化"],
    rows.map((row) => [
      `<div><strong>${escapeHtml(fillBucketLabel(row.fill_bucket))}</strong><div class="table-meta">${escapeHtml(localizeError(row.position_intent || row.execution_action || "unknown"))}</div></div>`,
      `<div><strong>${formatNumber(row.fill_qty)}</strong><div class="table-meta">@ ${formatNumber(row.fill_price)} | ${formatMaybeTimestamp(row.timestamp)}</div></div>`,
      `<div><strong>${formatSigned(row.realized_pnl_delta)}</strong><div class="table-meta">毛收益 ${formatSigned(row.gross_realized_pnl)} | 费用 ${formatSigned(row.fee_quote)}</div></div>`,
      `<div><strong>${formatNumber(row.starting_position_qty, 4, "待确认")} -> ${formatNumber(row.ending_position_qty, 4, "待确认")}</strong><div class="table-meta">${escapeHtml(row.fill_id || "当前没有成交编号")}</div></div>`,
    ]),
    "当前还没有子成交明细。",
    rows.map((row) => ({
      kicker: "子成交",
      title: `${fillBucketLabel(row.fill_bucket)} | ${localizeError(row.position_intent || row.execution_action || "unknown")}`,
      meta: timelineTimestamp(row.timestamp),
      tone: row.fill_bucket === "entry" ? "positive" : "warning",
      badge: pill(fillBucketLabel(row.fill_bucket), row.fill_bucket === "entry" ? "positive" : "warning"),
      fields: [
        { label: "数量 / 价格", value: formatNumber(row.fill_qty), meta: `@ ${formatNumber(row.fill_price)}` },
        { label: "已实现盈亏", value: formatSigned(row.realized_pnl_delta), meta: `毛收益 ${formatSigned(row.gross_realized_pnl)}` },
        { label: "手续费", value: formatSigned(row.fee_quote), meta: row.fill_id || "当前没有成交编号" },
      ],
      details: [
        { label: "起始仓位", value: formatNumber(row.starting_position_qty, 4, "待确认") },
        { label: "结束仓位", value: formatNumber(row.ending_position_qty, 4, "待确认") },
        { label: "成交名义", value: formatSigned(row.fill_notional_quote) },
      ],
      detailLabel: "展开成交诊断",
    })),
  );
}

function transitionCategoryLabel(value) {
  const key = String(value || "").trim().toLowerCase();
  return TRANSITION_CATEGORY_LABELS[key] || (key ? key : "退出分类待确认");
}

function fillBucketLabel(value) {
  const key = String(value || "").trim().toLowerCase();
  if (key === "entry") return "开仓成交";
  if (key === "exit") return "退出成交";
  if (key === "adjustment") return "调整成交";
  return key ? key : "成交阶段待确认";
}

function exitReasonSummary(rows = []) {
  if (!rows.length) return "当前还没有退出原因归因。";
  return rows
    .slice(0, 3)
    .map((row) => `${localizeError(row.reason || "unknown")}（${transitionCategoryLabel(row.transition_category)}，${formatNumber(row.decision_count, 0, "0")} 次）`)
    .join(" / ");
}

function exitIntentSummary(rows = []) {
  if (!rows.length) return "当前还没有退出成交意图归因。";
  return rows
    .slice(0, 3)
    .map((row) => `${localizeError(row.intent || "unknown")}（${formatNumber(row.fill_count, 0, "0")} 笔，名义 ${formatSigned(row.exit_notional_quote)}）`)
    .join(" / ");
}

function timelineTimestamp(value) {
  return `${formatMaybeTimestamp(value)} | ${formatRelativeAge(value)}`;
}

function ratioText(value) {
  if (value === null || value === undefined) return "当前无法计算";
  return formatNumber(value, 3);
}

function bpsText(value) {
  if (value === null || value === undefined) return "待确认";
  return `${formatNumber(value, 2)} bps`;
}

function renderTraceCompletenessCallout(detail = {}) {
  const completeness = detail.trace_completeness || "complete";
  const unmatchedCount = detail.unmatched_actionable_decision_count || 0;
  const missingLinkedCount = detail.missing_linked_reference_count || 0;
  if (completeness === "complete") {
    return callout({
      title: "退出链证据完整",
      copy: "当前主诊断链仅收录与成交强关联的决策，未发现额外的窗口内候选决策。",
      pills: [pill("完整", "positive")],
    });
  }
  const missingLinkedCopy = missingLinkedCount > 0
    ? `有 ${formatNumber(missingLinkedCount, 0, "0")} 条成交强关联键未找到对应审计/运行态证据。`
    : "";
  const candidateCopy = completeness === "candidate_only"
    ? `当前还没有与成交强关联的主诊断链决策，只发现 ${formatNumber(unmatchedCount, 0, "0")} 条窗口内候选决策。候选不会计入退出原因归因。`
    : unmatchedCount > 0
      ? `当前主诊断链已限制为强关联决策，但仍有 ${formatNumber(unmatchedCount, 0, "0")} 条窗口内候选决策未计入主归因。请结合下方候选决策一起复核。`
      : "当前主诊断链已限制为强关联决策，但存在缺失的强关联证据，需要回查审计链。";
  return callout({
    title: "退出链证据不完整",
    copy: [missingLinkedCopy, candidateCopy].filter(Boolean).join(" "),
    pills: [pill(traceCompletenessLabel(completeness), "warning")],
  });
}

function traceCompletenessLabel(value) {
  const key = String(value || "").trim().toLowerCase();
  if (key === "complete") return "完整";
  if (key === "partial") return "部分完整";
  if (key === "candidate_only") return "仅候选证据";
  if (key === "missing_linked_evidence") return "强关联证据缺失";
  return "完整性待确认";
}
