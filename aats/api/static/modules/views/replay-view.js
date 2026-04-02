import { actionButton, kvList, primaryStatusPanel, summaryStrip, surfaceCard } from "../components.js";
import { textOrFallback } from "../copy.js";
import { booleanWord, escapeHtml, formatMaybeTimestamp, formatNumber, formatRelativeAge } from "../formatters.js";
import { overlayParentPostmortemMeta, overlayParentPostmortemRows, renderOverlayParentHistoryTable } from "../overlay-parent-renderers.js";
import {
  readableIndependentAdaptiveMeta,
  readableIndependentAdaptiveSummary,
  readableIndependentTransitionExceptionMeta,
  readableIndependentTransitionExceptionSummary,
  readableOverlayParentSignalSummary,
  readableState,
  toneForReconciliationSeverity,
} from "../terms.js";

const DEFAULT_REPLAY_PARENT_FILTER = "all";

const REPLAY_PARENT_FILTERS = [
  { value: "all", label: "全部阶段" },
  { value: "inventory_only", label: "仅库存活跃" },
  { value: "target_only", label: "仅目标活跃" },
  { value: "target_and_inventory", label: "目标与库存" },
];

export function renderReplaySections(data, uiState = {}, paging = {}) {
  const replay = data.replayStatus || {};
  const recentPayload = data.replayRecentValidations || {};
  const recentValidations = Array.isArray(recentPayload.validations)
    ? recentPayload.validations
    : Array.isArray(replay.recent_validations)
      ? replay.recent_validations
      : [];
  const latestValidation = replay.last_validation || recentValidations[0] || null;
  const latestSummary = latestValidation?.overlay_parent_exposure_summary || null;
  const latestAdaptiveSummary = latestValidation?.independent_adaptive_summary || null;
  const latestTransitionSummary = latestValidation?.independent_transition_exception_summary || null;
  const replayFilter = textOrFallback(uiState.parentFilter, DEFAULT_REPLAY_PARENT_FILTER);
  const filteredValidations = filterReplayValidations(recentValidations, replayFilter);
  const reconciliation = data.reconciliationLatest?.reconciliation || null;
  const mismatchSummary = data.reconciliationLatest?.mismatch_summary || {};
  const legMismatchSummary = mismatchSummary.leg_mismatch_summary || {};
  const currentLimit = Number(paging.recentReplayValidationsLimit || recentPayload.limit || recentValidations.length || 0);
  const defaultLimit = Number(paging.defaultReplayValidationsLimit || 8);

  return {
    replayHero: primaryStatusPanel({
      eyebrow: "回放与复盘",
      headline: replay.healthy ? "回放链路目前健康" : "回放链路仍有差异",
      summary: latestValidation
        ? `最近一次回放在 ${formatMaybeTimestamp(latestValidation.validated_at)} 完成，当前可以直接对读父腿阶段、链路分数和腿级对账。`
        : "当前还没有回放校验记录，先手动触发一次回放，才能开始复盘。",
      tone: replay.healthy ? "positive" : "warning",
      pills: [
        replayHeroPill("回放健康度", booleanWord(replay.healthy), replay.healthy ? "positive" : "warning"),
        replayHeroPill("最新决策", textOrFallback(latestValidation?.decision_id, "当前没有决策编号"), latestValidation?.decision_id ? "info" : "neutral"),
        replayHeroPill("最新对账", readableState(reconciliation?.severity || "unknown"), reconciliation?.halt_required ? "danger" : toneForReconciliationSeverity(reconciliation?.severity)),
      ],
      metrics: [
        {
          label: "最近回放时间",
          value: formatMaybeTimestamp(latestValidation?.validated_at),
          meta: formatRelativeAge(latestValidation?.validated_at),
          tone: latestValidation?.validated_at ? "info" : "neutral",
        },
        {
          label: "链路分数",
          value: formatNumber(latestValidation?.chain_health_score, 3),
          meta: `偏差 ${formatNumber(latestValidation?.divergence_count || 0, 0)} / 事件 ${formatNumber(latestValidation?.replayed_event_count || 0, 0)}`,
          tone: replay.healthy ? "positive" : "warning",
        },
        {
          label: "历史样本",
          value: formatNumber(recentPayload.total_available || recentValidations.length, 0),
          meta: filteredValidations.length === recentValidations.length
            ? "当前没有额外筛选条件"
            : `筛选后还剩 ${formatNumber(filteredValidations.length, 0)} 条`,
          tone: recentValidations.length ? "info" : "neutral",
        },
        {
          label: "腿级异常",
          value: Number(legMismatchSummary.total_count || 0) > 0 ? `${formatNumber(legMismatchSummary.total_count || 0, 0)} 条` : "当前没有腿级异常",
          meta: Number(legMismatchSummary.missing_execution_chain_count || 0) > 0
            ? `其中 ${formatNumber(legMismatchSummary.missing_execution_chain_count || 0, 0)} 条缺少执行链`
            : "当前没有缺少执行链的腿级异常",
          tone: Number(legMismatchSummary.total_count || 0) > 0 ? "warning" : "positive",
        },
      ],
    }),
    replayLatestPostmortem: latestSummary
      ? surfaceCard({
          title: "最新父腿复盘",
          kicker: "最近一次回放",
          copy: "把最近一次回放的父腿阶段和数量拆解单独收口，先看目标、库存和最终生效信号到底是谁在主导。",
          content: kvList(overlayParentPostmortemRows(latestSummary)),
        })
      : "",
    replayAdaptivePostmortem: latestAdaptiveSummary
      ? surfaceCard({
          title: "最新自适应复盘",
          kicker: "阈值回放",
          copy: "这里单独收口独立双书在回放里的动态阈值、缩量入场和健康约束结果。",
          content: kvList([
            [
              "自适应阈值",
              readableIndependentAdaptiveSummary(latestAdaptiveSummary, "当前还没有独立双书自适应摘要"),
              readableIndependentAdaptiveMeta(latestAdaptiveSummary, "当前没有额外自适应说明"),
            ],
          ]),
        })
      : "",
    replayTransitionPostmortem: latestTransitionSummary
      ? surfaceCard({
          title: "最新迁移异常复盘",
          kicker: "状态机异常",
          copy: "把独立双书里非法状态迁移单独收口，方便快速确认是哪条书、哪次 prior -> next 迁移出了问题。",
          content: kvList([
            [
              "迁移异常摘要",
              readableIndependentTransitionExceptionSummary(latestTransitionSummary, "当前没有独立双书迁移异常摘要"),
              readableIndependentTransitionExceptionMeta(latestTransitionSummary, "当前没有额外迁移异常说明"),
            ],
          ]),
        })
      : "",
    replayLinkedRead: surfaceCard({
      title: "父腿复盘与腿级对账联读",
      kicker: "回放与对账联读",
      copy: "把回放里的父腿阶段和最新腿级对账异常放在一起读，减少库存残留、目标切换和执行链残留的解释成本。",
      actions: `<div class="stack-actions table-actions--compact">${actionButton("查看风险页", "navigate-view", "risk", "ghost")}</div>`,
      content: renderReplayReconciliationLinkedRead(latestSummary, legMismatchSummary, reconciliation),
    }),
    replayHistory: surfaceCard({
      title: "回放父腿历史",
      kicker: "历史对比",
      copy: "这里专门做回放历史筛选、折叠和横向比较。风险页只保留摘要，这里才是详细工作区。",
      actions: renderReplayHistoryActions({
        replayFilter,
        hasMore: Boolean(recentPayload.has_more),
        canCollapse: currentLimit > defaultLimit,
      }),
      content: renderOverlayParentHistoryTable(filteredValidations, {
        emptyMessage: "当前没有可供筛选的回放父腿历史。",
        includeHealthColumn: true,
      }),
    }),
  };
}

export function renderReplayView(data, uiState = {}, paging = {}) {
  const sections = renderReplaySections(data, uiState, paging);
  return `
    <div class="panel-grid">
      <div class="span-12">${sections.replayHero}</div>
      ${sections.replayLatestPostmortem ? `<div class="${sections.replayAdaptivePostmortem || sections.replayTransitionPostmortem ? "span-4" : "span-12"}">${sections.replayLatestPostmortem}</div>` : ""}
      ${sections.replayAdaptivePostmortem ? `<div class="${sections.replayLatestPostmortem && sections.replayTransitionPostmortem ? "span-4" : sections.replayLatestPostmortem || sections.replayTransitionPostmortem ? "span-8" : "span-12"}">${sections.replayAdaptivePostmortem}</div>` : ""}
      ${sections.replayTransitionPostmortem ? `<div class="${sections.replayLatestPostmortem && sections.replayAdaptivePostmortem ? "span-4" : sections.replayLatestPostmortem || sections.replayAdaptivePostmortem ? "span-8" : "span-12"}">${sections.replayTransitionPostmortem}</div>` : ""}
      <div class="span-12">${sections.replayLinkedRead}</div>
      <div class="span-12">${sections.replayHistory}</div>
    </div>
  `;
}

function replayHeroPill(label, value, tone) {
  return `<span class="signal-pill tone-${escapeHtml(tone)}">${escapeHtml(label)} ${escapeHtml(value)}</span>`;
}

function renderReplayHistoryActions({ replayFilter, hasMore, canCollapse }) {
  const filterButtons = REPLAY_PARENT_FILTERS.map((option) =>
    actionButton(
      option.label,
      "set-replay-parent-filter",
      option.value,
      option.value === replayFilter ? "secondary" : "ghost",
    )
  ).join("");
  const pagingButtons = [
    hasMore ? actionButton("查看更多", "load-more-replay-validations", "", "ghost") : "",
    canCollapse ? actionButton("收起历史", "collapse-replay-validations", "", "ghost") : "",
  ].filter(Boolean).join("");
  return `<div class="stack-actions table-actions--compact">${filterButtons}${pagingButtons}</div>`;
}

function filterReplayValidations(validations, replayFilter) {
  if (!Array.isArray(validations) || replayFilter === DEFAULT_REPLAY_PARENT_FILTER) {
    return Array.isArray(validations) ? validations : [];
  }
  return validations.filter((validation) => {
    const lifecycleState = String(validation?.overlay_parent_exposure_summary?.lifecycle_state || "").trim().toLowerCase();
    return lifecycleState === replayFilter;
  });
}

function renderReplayReconciliationLinkedRead(replaySummary = null, legMismatchSummary = {}, reconciliation = null) {
  return summaryStrip([
    {
      label: "最新父腿阶段",
      value: replaySummary
        ? readableOverlayParentSignalSummary(replaySummary, "当前没有额外父腿阶段说明")
        : "当前没有回放父腿复盘",
      meta: replaySummary
        ? overlayParentPostmortemMeta(replaySummary)
        : "先手动触发一次回放，才能开始复盘父腿阶段",
      tone: replaySummary ? replayLifecycleTone(replaySummary.lifecycle_state) : "neutral",
    },
    {
      label: "腿级对账异常",
      value: Number(legMismatchSummary.total_count || 0) > 0 ? `${formatNumber(legMismatchSummary.total_count || 0, 0)} 条` : "当前没有腿级异常",
      meta: replayLegMismatchMeta(legMismatchSummary),
      tone: Number(legMismatchSummary.total_count || 0) > 0 ? "warning" : "positive",
    },
    {
      label: "联读结论",
      value: replayReconciliationNarrative(replaySummary, legMismatchSummary),
      meta: reconciliation?.halt_required
        ? "最新对账已要求先暂停自动交易。"
        : "当前可以继续用回放父腿复盘解释腿级差异。",
      tone: reconciliation?.halt_required ? "danger" : Number(legMismatchSummary.total_count || 0) > 0 ? "warning" : "info",
    },
  ]);
}

function replayReconciliationNarrative(replaySummary = null, legMismatchSummary = {}) {
  const mismatchCount = Number(legMismatchSummary.total_count || 0);
  if (!replaySummary && mismatchCount === 0) {
    return "当前既没有回放父腿复盘，也没有腿级异常。";
  }
  if (!replaySummary) {
    return "最新对账已经发现腿级异常，但还缺回放父腿复盘，先补一次回放再解释。";
  }
  if (replaySummary.lifecycle_state === "inventory_only" && mismatchCount > 0) {
    return "父腿仍靠真实库存维持，同时最新对账还有腿级差异，优先核对库存残留和执行链残留。";
  }
  if (replaySummary.lifecycle_state === "target_only" && mismatchCount > 0) {
    return "父腿只剩目标驱动，但腿级对账还有差异，优先检查未完成的目标切换或迟到成交。";
  }
  if (replaySummary.lifecycle_state === "target_and_inventory" && mismatchCount > 0) {
    return "父腿同时受目标和库存影响，腿级异常也还在，先按 mixed 场景核对主腿与 overlay 是否同步。";
  }
  if (mismatchCount === 0) {
    return "当前回放父腿阶段和最新腿级对账没有明显冲突，可以把它当作复盘基线。";
  }
  return "当前已经有回放父腿复盘，也有腿级差异，建议继续联读主腿阶段、目标切换和执行链残留。";
}

function replayLegMismatchMeta(summary = {}) {
  const items = Array.isArray(summary.items) ? summary.items : [];
  if (!items.length) {
    return "当前没有额外 long / short 腿级差异。";
  }
  const firstItem = items[0] || {};
  const firstSide = firstItem.leg_side === "long" ? "多头腿" : firstItem.leg_side === "short" ? "空头腿" : "净仓腿";
  const firstSymbol = textOrFallback(firstItem.symbol, "未知合约");
  const firstDelta = `${formatNumber(firstItem.stored_qty)} / ${formatNumber(firstItem.exchange_qty)}`;
  const missingChainMeta = Number(summary.missing_execution_chain_count || 0) > 0
    ? `缺少执行链 ${formatNumber(summary.missing_execution_chain_count || 0, 0)} 条`
    : "当前没有缺少执行链的腿级异常";
  return `${missingChainMeta}；${firstSymbol} ${firstSide} 本地/交易所 ${firstDelta}`;
}

function replayLifecycleTone(lifecycleState) {
  if (lifecycleState === "inventory_only") return "warning";
  if (lifecycleState === "target_only") return "info";
  if (lifecycleState === "target_and_inventory") return "positive";
  return "neutral";
}
