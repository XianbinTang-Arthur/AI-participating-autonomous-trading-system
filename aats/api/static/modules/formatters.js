export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function formatNumber(value, digits = 4) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return trimTrailingZeros(number.toFixed(Math.abs(number) >= 1000 ? 2 : digits));
}

export function formatSigned(value, digits = 4) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${number > 0 ? "+" : ""}${formatNumber(number, digits)}`;
}

export function formatMaybeTimestamp(value) {
  if (!value) return "-";
  const date = parseDate(value);
  if (!date) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function formatRelativeAge(value) {
  if (!value) return "-";
  const date = parseDate(value);
  if (!date) return "-";
  const deltaSeconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (deltaSeconds < 60) return `${deltaSeconds} 秒前`;
  if (deltaSeconds < 3600) return `${Math.floor(deltaSeconds / 60)} 分钟前`;
  if (deltaSeconds < 86400) return `${Math.floor(deltaSeconds / 3600)} 小时前`;
  return `${Math.floor(deltaSeconds / 86400)} 天前`;
}

export function formatDuration(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "-";
  const day = Math.floor(seconds / 86400);
  const hour = Math.floor((seconds % 86400) / 3600);
  const minute = Math.floor((seconds % 3600) / 60);
  if (day > 0) return `${day} 天 ${hour} 小时`;
  if (hour > 0) return `${hour} 小时 ${minute} 分钟`;
  if (minute > 0) return `${minute} 分钟`;
  return `${Math.floor(seconds)} 秒`;
}

export function listOrDash(value) {
  if (!value) return "-";
  if (Array.isArray(value)) return value.length ? value.join("、") : "-";
  return String(value);
}

export function booleanWord(value) {
  if (value === true) return "是";
  if (value === false) return "否";
  return "-";
}

export function emptyState(message) {
  return `<div class="empty-state">${escapeHtml(message)}</div>`;
}

export function rawJson(value) {
  return `<pre class="raw-json">${escapeHtml(JSON.stringify(value ?? {}, null, 2))}</pre>`;
}

export function parseDate(value) {
  if (!value) return null;
  const normalized = String(value).replace("Z", "+00:00");
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function trimTrailingZeros(value) {
  return String(value).replace(/(\.\d*?[1-9])0+$/u, "$1").replace(/\.0+$/u, "");
}
