import { actionButton, kvList, summaryStrip, surfaceCard, timeline } from "./components.js";
import { escapeHtml, formatMaybeTimestamp, formatRelativeAge, listOrDash, rawJson } from "./formatters.js";

export function buildPhase1ShadowDrawer(detail, { shadowBlocker = null, uiHints = {}, history = [] } = {}) {
  const lag = detail?.lag || {};
  const executionShadow = detail?.execution_shadow || {};
  const ledgerShadow = detail?.ledger_shadow || {};
  const latestReviewAction = detail?.latest_review_action || null;
  const latestAlert = detail?.latest_alert || null;
  const latestFailure = detail?.latest_failure || null;
  const status = String(detail?.status || "unknown");

  return {
    eyebrow: "影子兼容层详情",
    title: "Phase 1 影子兼容层",
    summary: detail?.summary || "当前没有额外说明。",
    body: [
      surfaceCard({
        title: "当前状态",
        kicker: "运行概览",
        copy: "这里集中看影子兼容层是否追平旧链路、最近是否失败，以及当前为什么会阻断恢复。",
        content: summaryStrip([
          {
            label: "当前状态",
            value: readableShadowStatus(status),
            meta: detail?.detail || detail?.summary || "当前没有额外状态说明",
            tone: toneForShadowStatus(status),
          },
          {
            label: "恢复门禁",
            value: detail?.ready ? "已就绪" : "未就绪",
            meta: detail?.fresh ? "当前没有新鲜度降级" : "当前仍有兼容层降级或积压",
            tone: detail?.ready ? "positive" : "warning",
          },
          {
            label: "当前阻断",
            value: listOrDash(detail?.blockers, "当前没有额外阻断"),
            meta: shadowBlocker?.recommended_next_step || "当前没有额外处理建议",
            tone: Array.isArray(detail?.blockers) && detail.blockers.length ? "danger" : "neutral",
          },
        ]),
        actions: renderShadowActions(shadowBlocker, uiHints),
      }),
      surfaceCard({
        title: "同步积压",
        kicker: "追平情况",
        copy: "只有订单、成交和保留金都追平后，影子兼容层的结果才适合参与恢复判断。",
        content: summaryStrip([
          {
            label: "订单积压",
            value: backlogValue(lag.order_backlog),
            meta: syncMeta(executionShadow.last_order_sync_ts, executionShadow.last_synced_order_id),
            tone: toneForBacklog(lag.order_backlog),
          },
          {
            label: "成交积压",
            value: backlogValue(lag.fill_backlog),
            meta: syncMeta(executionShadow.last_fill_sync_ts, executionShadow.last_synced_fill_id),
            tone: toneForBacklog(lag.fill_backlog),
          },
          {
            label: "保留金积压",
            value: backlogValue(lag.obligation_backlog),
            meta: syncMeta(ledgerShadow.last_sync_ts, ledgerShadow.last_synced_order_id),
            tone: toneForBacklog(lag.obligation_backlog),
          },
        ]),
      }),
      surfaceCard({
        title: "最近异常",
        kicker: "告警与失败",
        copy: "这里保留最近一次影子兼容层告警、处理失败和同步错误，方便人工核查时快速定位。",
        // #35 修复：原本这里本地自拼 `<div class="kv-list">` + 四个 kvRow，重复了
        // components.kvList 的 row markup。一旦 kv-list / kv-row 的 class 名或
        // DOM 结构改动，shadow-drawer 不会自动跟上。改为直接调用 kvList。
        content: kvList([
          ["最近告警", latestAlert?.message || "当前没有影子兼容层告警", formatMaybeTimestamp(latestAlert?.observed_at)],
          ["最近处理失败", latestFailure?.message || "当前没有影子兼容层处理失败记录", formatMaybeTimestamp(latestFailure?.observed_at)],
          ["执行影子最近错误", executionShadow?.last_error || "当前没有执行影子错误", formatMaybeTimestamp(executionShadow?.last_failure_ts)],
          ["账本影子最近错误", ledgerShadow?.last_error || "当前没有账本影子错误", formatMaybeTimestamp(ledgerShadow?.last_failure_ts)],
        ]),
      }),
      surfaceCard({
        title: "人工核查记录",
        kicker: "运维闭环",
        copy: "人工核查不会自动放行系统，但应该留下明确记录，说明当时看到了什么状态、为什么继续阻断或继续观察。",
        content: summaryStrip([
          {
            label: "最近核查状态",
            value: latestReviewAction ? "已记录" : "尚未记录",
            meta: latestReviewAction
              ? `${latestReviewAction.actor_identity || latestReviewAction.actor_role || "未知操作人"} 于 ${formatMaybeTimestamp(latestReviewAction.details?.reviewed_at || latestReviewAction.details?.snapshot_generated_at || latestReviewAction._event_created_at)}`
              : "当前还没有影子兼容层人工核查记录",
            tone: latestReviewAction ? "info" : "neutral",
          },
          {
            label: "核查结论",
            value: latestReviewAction?.details?.snapshot_status ? readableShadowStatus(latestReviewAction.details.snapshot_status) : "待确认",
            meta: latestReviewAction?.reason || "当前没有核查原因记录",
            tone: latestReviewAction ? toneForShadowStatus(String(latestReviewAction.details?.snapshot_status || status)) : "neutral",
          },
          {
            label: "核查时积压",
            value: latestReviewAction
              ? `订单 ${backlogValue(latestReviewAction.details?.lag?.order_backlog)} / 成交 ${backlogValue(latestReviewAction.details?.lag?.fill_backlog)} / 保留金 ${backlogValue(latestReviewAction.details?.lag?.obligation_backlog)}`
              : "待确认",
            meta: latestReviewAction?.details?.summary || "当前没有额外核查摘要",
            tone: latestReviewAction ? "warning" : "neutral",
          },
        ]),
      }),
      surfaceCard({
        title: "近期历史",
        kicker: "事件时间线",
        copy: "把最近的人工核查、告警和处理失败放在一起，便于确认兼容层问题是持续存在还是已经恢复。",
        content: renderHistory(history),
      }),
      surfaceCard({
        title: "原始记录",
        kicker: "调试原文",
        copy: "当摘要信息不够时，再往下看原始 JSON，避免把主视图变成日志墙。",
        content: rawJson(detail),
      }),
    ].join(""),
  };
}

function renderShadowActions(shadowBlocker, uiHints) {
  const actions = Array.isArray(shadowBlocker?.actions) ? shadowBlocker.actions : [];
  if (!actions.length) return "";
  const permissionMessage = String(uiHints?.controlPermissionMessage || "").trim();
  const rendered = actions.map((action) => {
    const isApi = action.kind !== "client";
    const disabledReason = isApi ? permissionMessage || action.disabled_reason : action.disabled_reason;
    const disabled = Boolean((isApi && permissionMessage) || action.enabled === false);
    if (action.kind === "client") {
      return actionButton(
        action.label,
        action.client_action || "refresh-dashboard",
        action.value || "",
        action.tone || "ghost",
        {
          disabled,
          title: disabledReason || action.expected_effect || "",
        },
      );
    }
    return actionButton(
      action.label,
      "trigger-blocker-action",
      `${action.action_id}::${shadowBlocker?.blocker || ""}`,
      action.tone || "secondary",
      {
        disabled,
        title: disabledReason || action.expected_effect || "",
      },
    );
  });
  return `<div class="stack-actions">${rendered.join("")}</div>`;
}

function readableShadowStatus(status) {
  const map = {
    not_configured: "未配置",
    idle: "空闲",
    healthy: "已追平",
    lagging: "仍有积压",
    degraded: "最近失败",
  };
  return map[status] || status || "待确认";
}

function toneForShadowStatus(status) {
  if (status === "healthy") return "positive";
  if (status === "lagging") return "warning";
  if (status === "degraded") return "danger";
  if (status === "not_configured") return "neutral";
  return "info";
}

function toneForBacklog(value) {
  if (value === null || value === undefined) return "neutral";
  return Number(value) > 0 ? "warning" : "positive";
}

function backlogValue(value) {
  if (value === null || value === undefined) return "待确认";
  return String(value);
}

function syncMeta(timestamp, objectId) {
  const parts = [];
  if (timestamp) {
    parts.push(`${formatMaybeTimestamp(timestamp)}（${formatRelativeAge(timestamp)}）`);
  }
  if (objectId) {
    parts.push(`最近对象 ${objectId}`);
  }
  return parts.join("，") || "当前还没有同步记录";
}

function renderHistory(rows) {
  if (!Array.isArray(rows) || rows.length === 0) {
    return `<div class="empty-state">当前还没有影子兼容层历史记录。</div>`;
  }
  return timeline(
    rows.slice(0, 12).map((row) => ({
      title: historyTitle(row),
      subtitle: historySubtitle(row),
      detail: String(row?.summary || "当前没有额外说明"),
      timestamp: formatMaybeTimestamp(row?.observed_at),
      pill: `<span class="signal-pill tone-${escapeHtml(historyTone(row))}">${escapeHtml(historyPill(row))}</span>`,
    })),
    "当前还没有影子兼容层历史记录。",
  );
}

function historyTitle(row) {
  if (row?.entry_type === "review") return "人工核查";
  if (row?.entry_type === "alert") return "兼容层告警";
  if (row?.entry_type === "failure") return "处理失败";
  return "历史记录";
}

function historySubtitle(row) {
  if (row?.entry_type === "review") {
    return `${row?.actor_identity || row?.actor_role || "未知操作人"} · ${readableShadowStatus(String(row?.status || "unknown"))}`;
  }
  return row?.reason ? String(row.reason) : readableShadowStatus(String(row?.status || "unknown"));
}

function historyTone(row) {
  if (row?.entry_type === "failure") return "danger";
  if (row?.entry_type === "alert") return row?.status === "warning" ? "warning" : "danger";
  if (row?.entry_type === "review") return "info";
  return "neutral";
}

function historyPill(row) {
  if (row?.entry_type === "review") return "已核查";
  if (row?.entry_type === "alert") return "告警";
  if (row?.entry_type === "failure") return "失败";
  return "记录";
}
