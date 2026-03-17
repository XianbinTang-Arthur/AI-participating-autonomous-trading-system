import { emptyState, escapeHtml } from "./formatters.js";

export function pill(label, tone = "neutral") {
  return `<span class="signal-pill tone-${escapeHtml(tone)}">${escapeHtml(label)}</span>`;
}

export function statusCard({ title, value, meta = "", pills = [] }) {
  return `
    <article class="status-card">
      <h3>${escapeHtml(title)}</h3>
      <strong class="status-card__value">${escapeHtml(value)}</strong>
      <p class="status-card__meta">${escapeHtml(meta)}</p>
      <div class="status-card__foot">${pills.join("")}</div>
    </article>
  `;
}

export function surfaceCard({ title, kicker = "", copy = "", actions = "", content = "", classes = "" }) {
  return `
    <section class="surface-card ${escapeHtml(classes)}">
      <div class="panel-head">
        <div>
          ${kicker ? `<p class="panel-kicker">${escapeHtml(kicker)}</p>` : ""}
          <h3>${escapeHtml(title)}</h3>
          ${copy ? `<p class="meta-copy">${escapeHtml(copy)}</p>` : ""}
        </div>
        ${actions}
      </div>
      ${content}
    </section>
  `;
}

export function kvList(rows) {
  if (!rows.length) return emptyState("暂无数据。");
  return `
    <div class="kv-list">
      ${rows
        .map(
          ([label, value, meta = ""]) => `
            <div class="kv-row">
              <span class="kv-row__label">${escapeHtml(label)}</span>
              <strong class="kv-row__value">${escapeHtml(value)}</strong>
              ${meta ? `<span class="meta-copy">${escapeHtml(meta)}</span>` : ""}
            </div>
          `
        )
        .join("")}
    </div>
  `;
}

export function statGrid(items) {
  if (!items.length) return emptyState("暂无统计数据。");
  return `
    <div class="stat-grid">
      ${items
        .map(
          (item) => `
            <div class="stat-item">
              <span class="stat-item__label">${escapeHtml(item.label)}</span>
              <strong class="stat-item__value">${escapeHtml(item.value)}</strong>
              ${item.meta ? `<span class="stat-item__meta">${escapeHtml(item.meta)}</span>` : ""}
            </div>
          `
        )
        .join("")}
    </div>
  `;
}

export function callout({ title, copy, pills = [] }) {
  return `
    <article class="callout">
      <div class="panel-head">
        <h3>${escapeHtml(title)}</h3>
        <div class="inline-pills">${pills.join("")}</div>
      </div>
      <p>${escapeHtml(copy)}</p>
    </article>
  `;
}

export function table(headers, rows, emptyText) {
  if (!rows.length) return emptyState(emptyText);
  return `
    <div class="table-wrap">
      <table class="data-table">
        <thead>
          <tr>${headers.map((item) => `<th>${escapeHtml(item)}</th>`).join("")}</tr>
        </thead>
        <tbody>
          ${rows
            .map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`)
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

export function timeline(items, emptyText) {
  if (!items.length) return emptyState(emptyText);
  return `
    <div class="alert-list">
      ${items
        .map(
          (item) => `
            <article class="timeline-item">
              <div class="panel-head">
                <strong>${escapeHtml(item.title)}</strong>
                ${item.pill || ""}
              </div>
              <p class="meta-copy">${escapeHtml(item.subtitle || "")}</p>
              ${item.detail ? `<p>${escapeHtml(item.detail)}</p>` : ""}
              ${item.timestamp ? `<span class="table-meta">${escapeHtml(item.timestamp)}</span>` : ""}
            </article>
          `
        )
        .join("")}
    </div>
  `;
}

export function notice(message, tone = "info") {
  return `<div class="notice-card tone-${escapeHtml(tone)}">${escapeHtml(message)}</div>`;
}

export function codeValue(value) {
  return `<span class="mono">${escapeHtml(value)}</span>`;
}

export function actionButton(label, action, value = "", tone = "ghost") {
  return `<button class="${escapeHtml(buttonClass(tone))}" data-action="${escapeHtml(action)}" data-value="${escapeHtml(value)}">${escapeHtml(label)}</button>`;
}

function buttonClass(tone) {
  if (tone === "primary") return "primary-button";
  if (tone === "secondary") return "secondary-button";
  if (tone === "warning") return "warning-button";
  if (tone === "danger") return "danger-button";
  return "table-button";
}
