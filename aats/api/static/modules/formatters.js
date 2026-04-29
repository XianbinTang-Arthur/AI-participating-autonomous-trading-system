export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// #22 修复说明：digits 参数会在大额数字上被自动覆盖，这是有意设计但容易踩坑。
//
// 当 |number| >= 1000 时，本函数把小数位数硬钳到 2 位（不管调用方传了几位）。
// 这个 1000 阈值的目的是让面板上的"金额类"数字（USDT 余额、敞口、市值）
// 不会因为传统 4 位浮点小数把界面挤出 16 位长度——例如 12345.6789012 会被
// 渲染成 12345.68，而 0.0001234 仍按调用方要求显示 4 位。
//
// 副作用：调用方写 formatNumber(value, 6) 期望"无论如何都给 6 位小数"是
// 拿不到的；实际上 |value| >= 1000 时它只能拿到 2 位精度。如果未来出现
// 需要"高精度大额数字"的场景（例如某些 BTC 单位），应当：
//   1. 增加一个 `forceDigits: true` 选项绕过这个钳制；或者
//   2. 把"金额钳制"从这里抽出去，做成专门的 formatMoney(value) 包装。
// 现阶段所有调用点都接受这个 trade-off，未单独实现。
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
  return `
    <details class="debug-json">
      <summary>展开排障原文</summary>
      <pre class="raw-json">${escapeHtml(JSON.stringify(value ?? {}, null, 2))}</pre>
    </details>
  `;
}

// #21 修复说明：把 "Z" → "+00:00" 这个字符串替换的来由写清楚，避免后人觉得是
// 误改而把它去掉。
//
// 后端给的 ISO 时间戳大多是 "2026-04-06T12:34:56.789Z" 这种 Z 结尾的 UTC 形式。
// 现代浏览器（Chrome/Firefox/Safari/Edge 当代版本）都能直接 new Date(stringWithZ)
// 并正确解析成 UTC，但仍有少量旧 WebView / 嵌入式浏览器（特别是某些工业屏、运维
// 一体机里的旧 Chromium）会把 "Z" 当成"无时区"，于是按本地时间解释，导致前端
// 显示出现 +08 小时偏移。
//
// 把 Z 显式替换成 "+00:00" 可以让所有引擎都走"带显式时区偏移"的解析路径，结果
// 是稳定的 UTC，再交给浏览器按用户本地时区显示。这是一个有意保留的 kludge，
// 不是疏漏，请勿删除。
//
// 如果未来彻底放弃旧 WebView 支持，可以把 replace 删掉，用 new Date(value)
// 直接解析；但要先在所有目标终端上验过。
export function parseDate(value) {
  if (!value) return null;
  const normalized = String(value).replace("Z", "+00:00");
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function trimTrailingZeros(value) {
  return String(value).replace(/(\.\d*?[1-9])0+$/u, "$1").replace(/\.0+$/u, "");
}
