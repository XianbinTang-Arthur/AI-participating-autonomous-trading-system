import { escapeHtml } from "../../formatters.js";
import { renderCount, statusPill, statusTone } from "./formatters.js";

const STAGE_LABELS = {
  idle: "待处理",
  queued: "已排入",
  running: "进行中",
  complete: "已完成",
  action_required: "需要处理",
  blocked: "已阻断",
};

export function renderLifecycle(lifecycle = {}) {
  const stages = Array.isArray(lifecycle.stages) ? lifecycle.stages : [];
  if (!stages.length) return "";
  return `
    <section class="rdp-v3-lifecycle" aria-label="RDP 研究生命周期">
      ${stages.map((stage, index) => {
        const status = stage.status || "idle";
        const isCurrent = lifecycle.current_stage === stage.key;
        return `
          <article class="rdp-v3-stage tone-${escapeHtml(statusTone(status))}${isCurrent ? " is-current" : ""}">
            <div class="rdp-v3-stage__index" aria-hidden="true">${index + 1}</div>
            <div class="rdp-v3-stage__body">
              <div class="rdp-v3-stage__head">
                <strong>${escapeHtml(stage.label || stage.key || "未命名阶段")}</strong>
                ${statusPill(STAGE_LABELS[status] || "状态未知", statusTone(status))}
              </div>
              <p>${escapeHtml(stage.summary || "")}</p>
              <span>${escapeHtml(`证据 ${renderCount(stage.evidence_count)} 项${isCurrent ? " · 当前阶段" : ""}`)}</span>
            </div>
          </article>
        `;
      }).join("")}
    </section>
  `;
}
