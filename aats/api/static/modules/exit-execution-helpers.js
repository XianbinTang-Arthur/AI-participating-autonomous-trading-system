// #36 修复：原本 exit-execution 的工作台 helper 全部写在 modules/views/risk-view.js
// 里，modules/views/exit-execution-view.js 需要复用时不得不反向 import 同级 view：
//     views/exit-execution-view.js  →  views/risk-view.js
// view ↔ view 的水平依赖和 #5 的 drawer → view 一样，是"上层模块反向依赖上层
// 模块"的信号，容易在未来拆成懒加载模块时触发循环依赖，也让两个视图的职责
// 看起来互相嵌套。
//
// #27 关联修复：risk-view.js 原来还本地重新实现了一份
// exitExecutionHistoryWindowThresholdMs（阈值函数），但 modules/navigation-state.js
// 已经导出同名 helper，两处定义容易在未来窗口配置变化时漂移。这里直接 import
// navigation-state 的版本，删掉本地重复实现。
//
// 抽出的函数集合里既有渲染器（workspace / action 列表 / filter 表单 /
// list item）也有纯筛选逻辑（filter / normalize），它们之间互相调用、
// 和 risk-view 的 review 列表没有反向耦合——是一个干净的独立子系统。
//
// 命名约定（modules/ 下的"提取模块"后缀，配合 reconciliation-controls.js 一起读）：
//   - `*-helpers.js`：模块以"无副作用纯计算 / 数据归一化 / 字符串拼接"为主，
//      产出供视图使用的中间数据。本文件的 normalizedExitExecutionHistoryFilters /
//      mergedExitExecutionReviewItems / exitExecutionLatestActionLabel 都是典型。
//      虽然这里也确实有 renderExitExecutionWorkspace 这种 HTML 拼接函数，但它们
//      是"工作台局部 partial"而不是"控件按钮渲染"，主体仍以纯函数为主，所以
//      整个模块按 helpers 命名。
//   - `*-controls.js`：核心职责是"渲染 / 决定一组动作按钮的可用性"，参考
//      reconciliation-controls.js 中 renderReconciliationControls /
//      shouldShowResumeAction 这一簇。
// 新模块加进来时按这个标准选后缀；如果一个模块同时干这两类事，那它太大、
// 应当先按职责拆开再分别命名。
//
// ── 仍留在 risk-view.js 的函数（review 专用，只给 riskExitExecutionReview
// card 用，和工作台表格无关）：
//   renderExitExecutionReviewList / renderExitExecutionReviewItem /
//   renderExitExecutionReviewActions / exitExecutionReviewSummary /
//   exitExecutionReviewReasonLabel / exitExecutionReviewMeta /
//   exitExecutionLatestAction / renderExitExecutionRecentActions /
//   renderExitExecutionRecentAction / exitExecutionOperatorActionSignature /
//   exitExecutionCurrentBlocker / exitExecutionActionDisabledReason /
//   exitExecutionAdminPermissionReason / hasExitExecutionAdminAccess /
//   normalizedExitExecutionOperatorActions /
//   exitExecutionOperatorActionDescriptor

import { actionButton } from "./components.js";
import { textOrFallback } from "./copy.js";
import { escapeHtml, formatMaybeTimestamp, formatNumber } from "./formatters.js";
// #19/#37/#38 修复：原本 normalize / 渲染下拉两处都各自写一遍允许的 action / window
// 列表，现在统一从 navigation-state.js 的唯一来源 import：
//   - EXIT_EXECUTION_HISTORY_ACTION_OPTIONS / WINDOW_OPTIONS：[value, label] 列表
//   - EXIT_EXECUTION_HISTORY_ACTION_FILTERS / WINDOW_FILTERS：从上面 derive 出来的 Set
import {
  EXIT_EXECUTION_HISTORY_ACTION_FILTERS,
  EXIT_EXECUTION_HISTORY_ACTION_OPTIONS,
  EXIT_EXECUTION_HISTORY_WINDOW_FILTERS,
  EXIT_EXECUTION_HISTORY_WINDOW_OPTIONS,
  exitExecutionHistoryWindowThresholdMs,
} from "./navigation-state.js";
import { localizeError, readableState } from "./terms.js";

export function mergedExitExecutionReviewItems(recovery = {}) {
  const merged = [];
  const mergedByKey = new Map();
  const append = (items, source) => {
    if (!Array.isArray(items)) return;
    items.forEach((item) => {
      if (!item || typeof item !== "object") return;
      const parentIntentId = String(item.parent_intent_id || "").trim();
      if (!parentIntentId) return;
      const normalized = {
        ...item,
        review_source: source,
        startup_snapshot_backed: source === "startup_snapshot",
      };
      const existingIndex = mergedByKey.get(parentIntentId);
      if (existingIndex === undefined) {
        mergedByKey.set(parentIntentId, merged.length);
        merged.push(normalized);
        return;
      }
      const existing = merged[existingIndex];
      if (existing.review_source === "runtime" && source !== "runtime") {
        merged[existingIndex] = {
          ...existing,
          startup_snapshot_backed: true,
        };
        return;
      }
      merged[existingIndex] = {
        ...normalized,
        startup_snapshot_backed: Boolean(existing.startup_snapshot_backed) || source === "startup_snapshot",
      };
    });
  };
  append(recovery.exit_execution_review_items, "runtime");
  const latestStateSnapshot = recovery.latest_state_snapshot;
  const snapshotDetails = latestStateSnapshot?.details_json;
  if (snapshotDetails && snapshotDetails.source === "startup_exit_execution_review") {
    append(snapshotDetails.review_items, "startup_snapshot");
  }
  return merged;
}

export function normalizedExitExecutionHistoryFilters(filters = {}) {
  // #19/#37/#38 修复：用 navigation-state 里的统一 Set 校验，避免在这里重写第二份允许值。
  const action = String(filters?.action || "all").trim();
  const normalizedAction = EXIT_EXECUTION_HISTORY_ACTION_FILTERS.has(action) ? action : "all";
  const windowHours = String(filters?.windowHours || "all").trim();
  const normalizedWindowHours = EXIT_EXECUTION_HISTORY_WINDOW_FILTERS.has(windowHours) ? windowHours : "all";
  return {
    action: normalizedAction,
    parent: String(filters?.parent || "").trim(),
    actor: String(filters?.actor || "").trim(),
    windowHours: normalizedWindowHours,
    offset: Math.max(Number(filters?.offset || 0), 0),
    limit: Math.max(Number(filters?.limit || 20), 1),
  };
}

export function renderExitExecutionActionHistoryList(items = [], filters = {}) {
  const normalizedFilters = normalizedExitExecutionHistoryFilters(filters);
  const filteredCount = filterExitExecutionActionHistory(items, normalizedFilters).length;
  return `
    <div data-exit-history-root>
      ${renderExitExecutionActionHistoryFilters(normalizedFilters, { mode: "card" })}
      ${items.length ? `
        <div class="alert-list">
          ${items.map((item) => renderExitExecutionActionHistoryItem(item, normalizedFilters)).join("")}
        </div>
      ` : ""}
      <p class="meta-copy" data-exit-history-empty ${filteredCount > 0 ? "hidden" : ""}>当前筛选条件下没有退出任务处理记录。</p>
    </div>
  `;
}

export function renderExitExecutionWorkspace({ page = {}, filters = {} } = {}) {
  const normalizedFilters = normalizedExitExecutionHistoryFilters(filters);
  const items = Array.isArray(page.actions)
    ? page.actions.filter((item) => item && typeof item === "object")
    : [];
  const offset = Number(page.offset || normalizedFilters.offset || 0);
  const totalAvailable = Number(page.total_available || 0);
  const pageStart = totalAvailable > 0 ? offset + 1 : 0;
  const pageEnd = totalAvailable > 0 ? offset + items.length : 0;
  const hasMore = Boolean(page.has_more);
  const hasPrev = offset > 0;
  const filteredCount = filterExitExecutionActionHistory(items, normalizedFilters).length;
  return `
    <div data-exit-history-root data-exit-history-workspace>
      ${renderExitExecutionActionHistoryFilters(normalizedFilters, { mode: "workspace" })}
      <div class="panel-head">
        <div>
          <strong>完整时间线</strong>
          <p class="meta-copy">当前显示 ${formatNumber(pageStart, 0)} - ${formatNumber(pageEnd, 0)} / ${formatNumber(totalAvailable, 0)} 条。</p>
        </div>
        <div class="stack-actions table-actions--compact">
          ${actionButton("上一页", "paginate-exit-execution-history", "prev", "ghost", {
            disabled: !hasPrev,
            title: hasPrev ? "查看更早一页退出任务处理记录。" : "当前已经是第一页。",
          })}
          ${actionButton("下一页", "paginate-exit-execution-history", "next", "ghost", {
            disabled: !hasMore,
            title: hasMore ? "查看更晚一页退出任务处理记录。" : "当前没有更多记录。",
          })}
        </div>
      </div>
      ${items.length ? `
        <div class="alert-list">
          ${items.map((item) => renderExitExecutionActionHistoryItem(item, normalizedFilters)).join("")}
        </div>
      ` : ""}
      <p class="meta-copy" data-exit-history-empty ${filteredCount > 0 ? "hidden" : ""}>当前筛选条件下没有退出任务处理记录。</p>
      ${!items.length ? `<p class="meta-copy">当前工作区还没有命中的退出任务处理记录。你可以放宽筛选条件，或翻页查看更多历史。</p>` : ""}
      <p class="meta-copy">这里展示的是独立 operator 工作区列表；卡片上的筛选条件会同步到这里，点击“应用到完整列表”后会按当前条件重新拉取长历史。</p>
    </div>
  `;
}

export function exitExecutionLatestActionLabel(action) {
  if (action === "refresh_exchange_state") return "刷新交易所状态";
  if (action === "retry_limit_lookup") return "重试拆单上限查询";
  if (action === "safe_cancel") return "安全取消退出任务";
  return textOrFallback(action, "未知动作");
}

export function exitExecutionLatestActionStatus(status) {
  if (status === "completed") return "已完成";
  if (status === "failed") return "失败";
  if (status === "rejected") return "已拒绝";
  return textOrFallback(status, "状态待确认");
}

function renderExitExecutionActionHistoryItem(item, filters = {}) {
  const title = `退出任务 ${textOrFallback(item.symbol, "未知标的")}`;
  const statusSuffix = item.aggregate_status ? ` / 父任务 ${readableState(item.aggregate_status)}` : "";
  const subtitle = `${textOrFallback(item.parent_intent_id, "未知父任务")} / ${exitExecutionLatestActionLabel(item.action)} / ${exitExecutionLatestActionStatus(item.status)}${statusSuffix}`;
  const metaParts = [];
  if (item.created_at) {
    metaParts.push(`时间 ${formatMaybeTimestamp(item.created_at)}`);
  }
  if (item.actor_identity || item.actor_role) {
    metaParts.push(`操作人 ${textOrFallback(item.actor_identity, textOrFallback(item.actor_role, "未知"))}`);
  }
  const blocker = item && typeof item === "object" ? item.remaining_blocker : null;
  const blockerSummary = blocker && typeof blocker === "object"
    ? textOrFallback(blocker.summary, localizeError(blocker.code, "当前还有未解除的退出任务阻断。"))
    : "";
  const visible = filterExitExecutionActionHistory([item], filters).length > 0;
  return `
    <article
      class="timeline-item"
      data-exit-history-entry
      data-parent-intent-id="${escapeHtml(textOrFallback(item.parent_intent_id, ""))}"
      data-actor-search="${escapeHtml(exitExecutionActionActorSearch(item))}"
      data-action-kind="${escapeHtml(textOrFallback(item.action, ""))}"
      data-created-at-ms="${escapeHtml(String(exitExecutionActionCreatedAtMs(item)))}"
      ${visible ? "" : "hidden"}
    >
      <div class="panel-head">
        <div>
          <strong>${escapeHtml(title)}</strong>
          <p class="meta-copy">${escapeHtml(subtitle)}</p>
        </div>
      </div>
      ${item.summary ? `<p>${escapeHtml(String(item.summary))}</p>` : ""}
      ${metaParts.length ? `<p class="meta-copy">${escapeHtml(metaParts.join("；"))}</p>` : ""}
      ${blockerSummary ? `<p class="meta-copy"><strong>动作后仍卡在：</strong>${escapeHtml(blockerSummary)}</p>` : ""}
    </article>
  `;
}

function renderExitExecutionActionHistoryFilters(filters = {}, { mode = "card" } = {}) {
  const normalizedFilters = normalizedExitExecutionHistoryFilters(filters);
  const applyButton = actionButton(
    mode === "workspace" ? "应用筛选" : "应用到完整列表",
    "apply-exit-execution-history-workspace",
    "",
    "secondary",
    {
      title: mode === "workspace"
        ? "按当前筛选条件重新拉取 parent-exit 长历史。"
        : "把当前筛选条件同步到下方工作区列表，并按这些条件重新拉取完整历史。",
    },
  );
  const resetButton = actionButton(
    "重置筛选",
    "reset-exit-execution-history-workspace",
    "",
    "ghost",
    {
      title: "清空 parent-exit 时间线筛选条件并回到第一页。",
    },
  );
  return `
    <div class="stack-actions table-actions--compact">
      <label>
        <span class="meta-copy">动作</span>
        <select data-exit-history-filter="action">
          ${renderExitExecutionActionFilterOptions(normalizedFilters.action)}
        </select>
      </label>
      <label>
        <span class="meta-copy">父任务</span>
        <input
          type="text"
          data-exit-history-filter="parent"
          value="${escapeHtml(normalizedFilters.parent)}"
          placeholder="例如 exit_parent:btc_close"
        />
      </label>
      <label>
        <span class="meta-copy">操作人</span>
        <input
          type="text"
          data-exit-history-filter="actor"
          value="${escapeHtml(normalizedFilters.actor)}"
          placeholder="例如 risk-admin"
        />
      </label>
      <label>
        <span class="meta-copy">时间窗口</span>
        <select data-exit-history-filter="windowHours">
          ${renderExitExecutionActionWindowOptions(normalizedFilters.windowHours)}
        </select>
      </label>
      ${applyButton}
      ${resetButton}
    </div>
  `;
}

function renderExitExecutionActionFilterOptions(selectedAction = "all") {
  // #19/#37 修复：直接遍历 navigation-state 里的 [value, label] 列表，避免重写。
  return EXIT_EXECUTION_HISTORY_ACTION_OPTIONS.map(([value, label]) => (
    `<option value="${escapeHtml(value)}"${value === selectedAction ? " selected" : ""}>${escapeHtml(label)}</option>`
  )).join("");
}

function renderExitExecutionActionWindowOptions(selectedWindow = "all") {
  // #19/#38 修复：直接遍历 navigation-state 里的 [value, label] 列表，避免重写。
  return EXIT_EXECUTION_HISTORY_WINDOW_OPTIONS.map(([value, label]) => (
    `<option value="${escapeHtml(value)}"${value === selectedWindow ? " selected" : ""}>${escapeHtml(label)}</option>`
  )).join("");
}

function filterExitExecutionActionHistory(items = [], filters = {}) {
  const normalized = normalizedExitExecutionHistoryFilters(filters);
  // #27 关联修复：这里原本调用本地重复实现的 exitExecutionHistoryWindowThresholdMs，
  // 现在统一复用 navigation-state.js 的版本（见文件顶部 import）。
  const thresholdMs = exitExecutionHistoryWindowThresholdMs(normalized.windowHours);
  return items.filter((item) => {
    const actionMatches = normalized.action === "all" || String(item?.action || "").trim() === normalized.action;
    const parentMatches = !normalized.parent
      || String(item?.parent_intent_id || "").toLowerCase().includes(normalized.parent.toLowerCase());
    const actorMatches = !normalized.actor
      || exitExecutionActionActorSearch(item).includes(normalized.actor.toLowerCase());
    const createdAtMs = exitExecutionActionCreatedAtMs(item);
    const windowMatches = thresholdMs === null || createdAtMs >= thresholdMs;
    return actionMatches && parentMatches && actorMatches && windowMatches;
  });
}

function exitExecutionActionActorSearch(item) {
  return `${String(item?.actor_identity || "").trim()} ${String(item?.actor_role || "").trim()}`.trim().toLowerCase();
}

function exitExecutionActionCreatedAtMs(item) {
  const parsed = Date.parse(String(item?.created_at || ""));
  return Number.isFinite(parsed) ? parsed : 0;
}
