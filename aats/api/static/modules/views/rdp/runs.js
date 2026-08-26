import { actionButton, surfaceCard } from "../../components.js";
import { escapeHtml } from "../../formatters.js";
import {
  formatTime,
  relativeTime,
  shortId,
  statusPill,
  statusTone,
  stepLabel,
  workflowLabel,
} from "./formatters.js";

function runProgress(run = {}) {
  const complete = Number(run.completed_steps || 0);
  const total = Number(run.total_steps || 0);
  if (!total) return "尚未上报步骤";
  return `${complete}/${total} 步`;
}

function runActions(run = {}, canAdmin = false) {
  const status = String(run.status || "");
  const buttons = [actionButton("查看详情", "rdp-open-run", run.run_id || "", "secondary")];
  if (["queued", "running", "cancellation_requested"].includes(status)) {
    buttons.push(actionButton("取消", "rdp-cancel-run", run.run_id || "", "ghost", {
      disabled: !canAdmin || status === "cancellation_requested",
      title: !canAdmin ? "当前会话没有取消权限。" : "",
    }));
  } else if (["failed", "cancelled", "partially_succeeded"].includes(status)) {
    buttons.push(actionButton("修复后重试", "rdp-retry-run", run.run_id || "", "ghost", {
      disabled: !canAdmin,
      title: !canAdmin ? "当前会话没有重试权限。" : "",
    }));
  }
  return buttons.join("");
}

function renderActiveRun(run, canAdmin) {
  if (!run) {
    return `
      <div class="rdp-v3-empty rdp-v3-empty--compact">
        <strong>执行槽空闲</strong>
        <p>手工触发会立即创建 Run，通常在下一个 daemon 轮询周期开始。</p>
      </div>
    `;
  }
  return `
    <article class="rdp-v3-run rdp-v3-run--active tone-${escapeHtml(statusTone(run.status))}">
      <div class="rdp-v3-run__head">
        <div>
          <span class="rdp-v3-eyebrow">当前执行</span>
          <strong>${escapeHtml(workflowLabel(run.workflow))}</strong>
        </div>
        ${statusPill(run.status_label || "运行中", statusTone(run.status))}
      </div>
      <p>${escapeHtml(stepLabel(run.current_step_key))}</p>
      <div class="rdp-v3-inline-meta">
        <span>${escapeHtml(runProgress(run))}</span>
        <span>${escapeHtml(shortId(run.run_id))}</span>
        <span>${escapeHtml(relativeTime(run.heartbeat_at || run.started_at))}</span>
      </div>
      <div class="rdp-v3-actions">${runActions(run, canAdmin)}</div>
    </article>
  `;
}

function renderQueuedRuns(runs = [], canAdmin = false) {
  if (!runs.length) return '<p class="rdp-v3-muted">当前没有等待执行的 Run。</p>';
  return `
    <div class="rdp-v3-queue">
      ${runs.slice(0, 5).map((run) => `
        <article class="rdp-v3-queue__item">
          <span class="rdp-v3-queue__position" aria-label="队列位次 ${run.queue_position || 1}">${run.queue_position || 1}</span>
          <div>
            <strong>${escapeHtml(workflowLabel(run.workflow))}</strong>
            <p>${escapeHtml(run.waiting_reason || "等待 RDP daemon 领取。")}</p>
            <span>${escapeHtml(`创建于 ${formatTime(run.created_at || run.eligible_at)} · ${shortId(run.run_id)}`)}</span>
          </div>
          <div class="rdp-v3-actions">${runActions(run, canAdmin)}</div>
        </article>
      `).join("")}
    </div>
  `;
}

function renderRecentRuns(runs = [], canAdmin = false) {
  const terminal = runs.filter((run) => !["queued", "running", "cancellation_requested"].includes(run.status)).slice(0, 5);
  if (!terminal.length) return '<p class="rdp-v3-muted">尚无最近运行结果。</p>';
  return `
    <div class="rdp-v3-recent-runs">
      ${terminal.map((run) => `
        <article>
          <div>
            <strong>${escapeHtml(workflowLabel(run.workflow))}</strong>
            <span>${escapeHtml(`${relativeTime(run.finished_at || run.updated_at)} · ${shortId(run.run_id)}`)}</span>
          </div>
          ${statusPill(run.status_label || run.status || "未知", statusTone(run.status))}
          <div class="rdp-v3-actions">${runActions(run, canAdmin)}</div>
        </article>
      `).join("")}
    </div>
  `;
}

export function renderRuns(execution = {}, canAdmin = false) {
  const daemon = execution.daemon || {};
  const daemonTone = daemon.fresh ? statusTone(daemon.status) : "danger";
  return surfaceCard({
    title: "运行与执行队列",
    kicker: "单执行槽 · 立即接收",
    copy: execution.queue_explanation || "当前使用单执行槽保护研究产物一致性。",
    actions: statusPill(
      daemon.fresh ? `后台执行器${daemon.status_label || "在线"}` : "后台执行器心跳异常",
      daemonTone,
    ),
    classes: "rdp-v3-card rdp-v3-runs-card",
    panelKey: "rdpWorkspace",
    content: `
      ${renderActiveRun(execution.active_run, canAdmin)}
      <div class="rdp-v3-subsection">
        <div class="rdp-v3-subsection__head"><h4>等待队列</h4><span>${escapeHtml(String(execution.queued_count || 0))} 条</span></div>
        ${renderQueuedRuns(execution.queued_runs || [], canAdmin)}
      </div>
      <details class="rdp-v3-details">
        <summary>最近运行</summary>
        ${renderRecentRuns(execution.recent_runs || [], canAdmin)}
      </details>
    `,
  });
}
