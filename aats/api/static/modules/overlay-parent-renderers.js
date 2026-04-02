import { responsiveTable } from "./components.js";
import { textOrFallback } from "./copy.js";
import { booleanWord, escapeHtml, formatMaybeTimestamp, formatNumber } from "./formatters.js";
import {
  readableOverlayParentLegQuantitySummary,
  readableOverlayParentPostmortemMeta,
  readableOverlayParentSignalSummary,
  readableState,
} from "./terms.js";

function overlayParentSourceOfTruth(summary = {}) {
  return summary.source_of_truth || summary.parent_source_of_truth;
}

function overlayParentQuantityMeta(summary = {}) {
  return `判定口径 ${textOrFallback(readableState(overlayParentSourceOfTruth(summary)), "待确认")}`;
}

function replayHealthSummary(validation = {}) {
  return `${booleanWord(validation?.healthy)} / 偏差 ${formatNumber(validation?.divergence_count, 0, "待确认")} / 分数 ${formatNumber(validation?.chain_health_score, 3, "待确认")}`;
}

export function overlayParentPostmortemMeta(summary = {}, fallback = "当前没有额外父腿契约说明") {
  return readableOverlayParentPostmortemMeta(summary, fallback);
}

export function overlayParentPostmortemRows(summary = {}) {
  return [
    [
      "父腿阶段",
      readableOverlayParentSignalSummary(summary, "当前没有额外父腿阶段说明"),
      overlayParentPostmortemMeta(summary),
    ],
    [
      "双腿数量拆解",
      readableOverlayParentLegQuantitySummary(summary, "当前没有父腿多空数量拆解"),
      overlayParentQuantityMeta(summary),
    ],
  ];
}

export function renderOverlayParentHistoryTable(validations = [], options = {}) {
  const {
    emptyMessage = "当前没有回放父腿历史。",
    includeHealthColumn = false,
    includeHealthDetail = includeHealthColumn,
  } = options;
  const headers = ["回放时间 / 决策", "父腿阶段", "契约口径", "双腿数量拆解"];
  if (includeHealthColumn) {
    headers.push("回放健康");
  }
  const rows = validations.map((validation) => {
    const summary = validation?.overlay_parent_exposure_summary || {};
    const row = [
      `<strong>${escapeHtml(formatMaybeTimestamp(validation?.validated_at))}</strong><div class="table-meta">${escapeHtml(textOrFallback(validation?.decision_id, "当前没有决策编号"))}</div>`,
      escapeHtml(readableOverlayParentSignalSummary(summary, "当前没有父腿阶段说明")),
      escapeHtml(readableOverlayParentPostmortemMeta(summary, "当前没有额外父腿契约说明")),
      escapeHtml(readableOverlayParentLegQuantitySummary(summary, "当前没有父腿多空数量拆解")),
    ];
    if (includeHealthColumn) {
      row.push(escapeHtml(replayHealthSummary(validation)));
    }
    return row;
  });
  const cards = validations.map((validation) => {
    const summary = validation?.overlay_parent_exposure_summary || {};
    return {
      kicker: formatMaybeTimestamp(validation?.validated_at),
      title: textOrFallback(validation?.decision_id, "当前没有决策编号"),
      meta: overlayParentPostmortemMeta(summary),
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
          label: "判定口径",
          value: textOrFallback(readableState(overlayParentSourceOfTruth(summary)), "待确认"),
          meta: `链路分数 ${formatNumber(validation?.chain_health_score, 3, "待确认")}`,
        },
        ...(includeHealthDetail ? [{
          label: "回放健康",
          value: replayHealthSummary(validation),
        }] : []),
      ],
    };
  });
  return responsiveTable(headers, rows, emptyMessage, cards);
}
