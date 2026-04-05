export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function formatNumber(value, digits = 4, fallback = "待确认") {
  if (value === null || value === undefined || value === "") return fallback;
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return trimTrailingZeros(number.toFixed(Math.abs(number) >= 1000 ? 2 : digits));
}

export function formatSigned(value, digits = 4, fallback = "待确认") {
  if (value === null || value === undefined || value === "") return fallback;
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return `${number > 0 ? "+" : ""}${formatNumber(number, digits, fallback)}`;
}

export function formatMaybeTimestamp(value, fallback = "时间待同步") {
  if (!value) return fallback;
  const date = parseDate(value);
  if (!date) return escapeHtml(String(value));
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function formatRelativeAge(value, fallback = "时间待同步") {
  if (!value) return fallback;
  const date = parseDate(value);
  if (!date) return fallback;
  const deltaSeconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (deltaSeconds < 60) return `${deltaSeconds} 秒前`;
  if (deltaSeconds < 3600) return `${Math.floor(deltaSeconds / 60)} 分钟前`;
  if (deltaSeconds < 86400) return `${Math.floor(deltaSeconds / 3600)} 小时前`;
  return `${Math.floor(deltaSeconds / 86400)} 天前`;
}

export function formatDuration(value, fallback = "时长待确认") {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return fallback;
  const day = Math.floor(seconds / 86400);
  const hour = Math.floor((seconds % 86400) / 3600);
  const minute = Math.floor((seconds % 3600) / 60);
  if (day > 0) return `${day} 天 ${hour} 小时`;
  if (hour > 0) return `${hour} 小时 ${minute} 分钟`;
  if (minute > 0) return `${minute} 分钟`;
  return `${Math.floor(seconds)} 秒`;
}

export function listOrDash(value, fallback = "当前没有额外说明") {
  if (!value) return fallback;
  if (Array.isArray(value)) {
    const items = value.map((item) => String(item ?? "").trim()).filter(Boolean);
    return items.length ? items.join("、") : fallback;
  }
  const text = String(value).trim();
  return text || fallback;
}

export function booleanWord(value, fallback = "待确认") {
  if (value === true) return "是";
  if (value === false) return "否";
  return fallback;
}

export function middleEllipsis(value, start = 10, end = 6, fallback = "暂无编号") {
  if (value === null || value === undefined || value === "") return fallback;
  const text = String(value);
  if (text.length <= start + end + 3) return text;
  return `${text.slice(0, start)}...${text.slice(-end)}`;
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
