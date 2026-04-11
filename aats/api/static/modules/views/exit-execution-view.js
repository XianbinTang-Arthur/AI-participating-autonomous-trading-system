import { actionButton, summaryStrip, surfaceCard } from "../components.js";
import { textOrFallback } from "../copy.js";
// #36 修复：原本这里从 ./risk-view.js 反向 import 以下三个 helper，导致
// views/exit-execution-view.js ↔ views/risk-view.js 之间出现水平依赖。
// 已把 exit-execution 工作台相关 helper（~13 个）提到
// ../exit-execution-helpers.js，两个 view 模块都从该独立模块 import。
import {
  mergedExitExecutionReviewItems,
  normalizedExitExecutionHistoryFilters,
  renderExitExecutionWorkspace,
} from "../exit-execution-helpers.js";
import { escapeHtml, formatNumber } from "../formatters.js";

export function renderExitExecutionView(data, uiState = {}) {
  const recovery = data.systemRecovery?.recovery || {};
  const reviewItems = mergedExitExecutionReviewItems(recovery);
  const page = data.exitExecutionActionHistoryPage || {};
  const filters = normalizedExitExecutionHistoryFilters(uiState.exitExecutionHistory);
  const totalAvailable = Math.max(Number(page.total_available || 0), 0);
  const pendingReviewCount = reviewItems.length;
  const hasActiveFilters = Boolean(
    filters.parent
    || filters.actor
    || filters.action !== "all"
    || filters.windowHours !== "all"
  );

  return `
    <div class="panel-grid">
      <section class="span-12">
        ${surfaceCard({
          title: "退出任务独立工作台",
          kicker: "退出任务工作台",
          copy: "这里专门承接 parent-exit 的长历史排查、分页回看和人工动作追踪。当前筛选会写入地址栏，刷新页面或复制链接后仍能恢复同一组条件。",
          actions: `<div class="stack-actions table-actions--compact">${actionButton("返回风险页", "navigate-view", "risk", "ghost")}</div>`,
          content: summaryStrip([
            {
              label: "待人工处理",
              value: `${formatNumber(pendingReviewCount, 0)} 条`,
              meta: pendingReviewCount > 0
                ? "这些 parent-exit 仍有 review 或 resume blocker，建议先看下方完整工作台。"
                : "当前没有待人工处理的退出任务。",
              tone: pendingReviewCount > 0 ? "warning" : "positive",
            },
            {
              label: "历史总量",
              value: `${formatNumber(totalAvailable, 0)} 条`,
              meta: "这里展示的是 parent-exit operator action 的长历史分页结果。",
              tone: totalAvailable > 0 ? "info" : "neutral",
            },
            {
              label: "筛选状态",
              value: hasActiveFilters ? "已应用筛选" : "全部记录",
              meta: hasActiveFilters
                ? [
                    filters.parent ? `父任务 ${filters.parent}` : "",
                    filters.action !== "all" ? `动作 ${filters.action}` : "",
                    filters.actor ? `操作人 ${filters.actor}` : "",
                    filters.windowHours !== "all" ? `时间窗口 ${filters.windowHours} 小时` : "",
                  ].filter(Boolean).join("，")
                : "当前未限制父任务、动作、操作人或时间窗口。",
              tone: hasActiveFilters ? "warning" : "neutral",
            },
          ]),
        })}
      </section>

      <section class="span-12" id="exit-execution-workspace">
        ${surfaceCard({
          title: "完整处理列表",
          kicker: "历史记录与分页",
          copy: "筛选条件和分页位置会同步进 URL，可直接分享给值班同事，或在刷新后继续查看同一页。",
          content: renderExitExecutionWorkspace({
            page,
            filters,
          }),
        })}
      </section>

      <section class="span-12">
        ${surfaceCard({
          title: "当前 review 提示",
          kicker: "复核快照",
          copy: pendingReviewCount > 0
            ? "这些提示来自运行时 recovery 视图和启动快照回补，用来说明为什么某些 parent-exit 还不能继续自动续派。"
            : "当前没有额外的 parent-exit review 提示。",
          content: pendingReviewCount > 0
            ? `<div class="alert-list">${reviewItems.slice(0, 5).map((item) => `
                <article class="timeline-item">
                  <div class="panel-head">
                    <strong>${escapeHtml(textOrFallback(item.symbol, "未知标的"))} / ${escapeHtml(textOrFallback(item.parent_intent_id, "未知父任务"))}</strong>
                  </div>
                  <p>${escapeHtml(textOrFallback(item.review_summary, "当前仍需继续确认 parent-exit 的真实状态。"))}</p>
                  <p class="meta-copy">
                    可续派 ${formatNumber(item.remaining_dispatchable_quantity)}，
                    未确认 ${formatNumber(item.open_child_unknown_quantity)}，
                    来源 ${item.review_source === "startup_snapshot" ? "启动快照" : "运行时视图"}
                  </p>
                </article>
              `).join("")}</div>`
            : `<p class="meta-copy">当前没有待人工处理的 parent-exit 任务。</p>`,
        })}
      </section>
    </div>
  `;
}
