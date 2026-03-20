import { emptyState, escapeHtml } from "./formatters.js";

export function pill(label, tone = "neutral") {
  return `<span class="signal-pill tone-${escapeHtml(tone)}">${escapeHtml(label)}</span>`;
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

export function primaryStatusPanel({ eyebrow = "", title = "", headline = "", summary = "", pills = [], metrics = [], actions = "", tone = "neutral" }) {
  return `
    <section class="primary-status-panel tone-${escapeHtml(tone)}">
      <div class="panel-head">
        <div>
          ${eyebrow ? `<p class="panel-kicker">${escapeHtml(eyebrow)}</p>` : ""}
          ${title ? `<h3>${escapeHtml(title)}</h3>` : ""}
          ${headline ? `<strong class="primary-status-panel__headline">${escapeHtml(headline)}</strong>` : ""}
          ${summary ? `<p class="meta-copy">${escapeHtml(summary)}</p>` : ""}
        </div>
        ${actions}
      </div>
      ${pills.length ? `<div class="inline-pills">${pills.join("")}</div>` : ""}
      ${metrics.length ? summaryStrip(metrics) : ""}
    </section>
  `;
}

export function summaryStrip(items) {
  if (!items.length) return "";
  return `
    <div class="summary-strip">
      ${items
        .map(
          (item) => `
            <article class="summary-tile tone-${escapeHtml(item.tone || "neutral")}">
              <span class="summary-tile__label">${escapeHtml(item.label)}</span>
              <strong class="summary-tile__value">${escapeHtml(item.value)}</strong>
              ${item.meta ? `<span class="summary-tile__meta">${escapeHtml(item.meta)}</span>` : ""}
            </article>
          `
        )
        .join("")}
    </div>
  `;
}

export function alertQueue(items, emptyText = "当前暂无需要处理的提醒。") {
  if (!items.length) return emptyState(emptyText);
  return `
    <div class="alert-queue">
      ${items
        .map(
          (item) => `
            <article class="alert-queue__item tone-${escapeHtml(item.tone || "neutral")}">
              <div class="panel-head">
                <strong>${escapeHtml(item.title)}</strong>
                ${item.pill || ""}
              </div>
              ${item.copy ? `<p>${escapeHtml(item.copy)}</p>` : ""}
              ${item.meta ? `<span class="table-meta">${escapeHtml(item.meta)}</span>` : ""}
            </article>
          `
        )
        .join("")}
    </div>
  `;
}

export function kvList(rows) {
  if (!rows.length) return emptyState("当前暂无可展示数据。");
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
  if (!items.length) return emptyState("当前暂无统计数据。");
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
          ${rows.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}
        </tbody>
      </table>
    </div>
  `;
}

export function responsiveTable(headers, rows, emptyText, cards = []) {
  if (!rows.length) return emptyState(emptyText);
  return `
    ${table(headers, rows, emptyText)}
    ${cards.length ? `<div class="mobile-record-list">${cards.map((card) => mobileRecordCard(card)).join("")}</div>` : ""}
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

export function actionButton(label, action, value = "", tone = "ghost", options = {}) {
  const disabled = options.disabled ? " disabled" : "";
  const title = options.title ? ` title="${escapeHtml(options.title)}"` : "";
  const extraClass = options.className ? ` ${escapeHtml(options.className)}` : "";
  return `<button class="${escapeHtml(buttonClass(tone))}${extraClass}" data-action="${escapeHtml(action)}" data-value="${escapeHtml(value)}"${title}${disabled}>${escapeHtml(label)}</button>`;
}

function buttonClass(tone) {
  if (tone === "primary") return "primary-button";
  if (tone === "secondary") return "secondary-button";
  if (tone === "warning") return "warning-button";
  if (tone === "danger") return "danger-button";
  return "table-button";
}

function mobileRecordCard(card) {
  return `
    <article class="mobile-record-card tone-${escapeHtml(card.tone || "neutral")}">
      <div class="panel-head">
        <div>
          ${card.kicker ? `<p class="panel-kicker">${escapeHtml(card.kicker)}</p>` : ""}
          <strong>${escapeHtml(card.title || "待确认")}</strong>
          ${card.meta ? `<p class="meta-copy">${escapeHtml(card.meta)}</p>` : ""}
        </div>
        ${card.badge || ""}
      </div>
      <div class="mobile-record-card__body">
        ${(card.fields || [])
          .map(
            (field) => `
              <div class="mobile-record-card__row">
                <span class="mobile-record-card__label">${escapeHtml(field.label || "字段")}</span>
                <strong class="mobile-record-card__value">${escapeHtml(field.value || "待确认")}</strong>
                ${field.meta ? `<span class="mobile-record-card__meta">${escapeHtml(field.meta)}</span>` : ""}
              </div>
            `
          )
          .join("")}
      </div>
      ${card.details?.length
        ? `
          <details class="mobile-record-card__details">
            <summary>${escapeHtml(card.detailLabel || "展开详情")}</summary>
            <div class="mobile-record-card__detail-grid">
              ${card.details
                .map(
                  (field) => `
                    <div class="mobile-record-card__row is-secondary">
                      <span class="mobile-record-card__label">${escapeHtml(field.label || "字段")}</span>
                      <strong class="mobile-record-card__value">${escapeHtml(field.value || "待确认")}</strong>
                      ${field.meta ? `<span class="mobile-record-card__meta">${escapeHtml(field.meta)}</span>` : ""}
                    </div>
                  `
                )
                .join("")}
            </div>
          </details>
        `
        : ""}
      ${card.action ? `<div class="stack-actions">${card.action}</div>` : ""}
    </article>
  `;
}
