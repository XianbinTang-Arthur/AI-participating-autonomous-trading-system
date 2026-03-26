import { localizeError, readableState } from "./terms.js";

export function hasMeaningfulValue(value) {
  if (value === null || value === undefined) return false;
  if (Array.isArray(value)) {
    return value.some((item) => String(item ?? "").trim() !== "");
  }
  return String(value).trim() !== "";
}

export function textOrFallback(value, fallback = "待确认") {
  if (!hasMeaningfulValue(value)) return fallback;
  return String(value).trim();
}

export function splitCodeList(value) {
  const items = Array.isArray(value) ? value : [value];
  return items
    .flatMap((item) => String(item ?? "").split(/[、,，\s]+/))
    .map((item) => item.trim())
    .filter(Boolean);
}

export function localizeList(value, fallback = "当前暂无说明") {
  if (!hasMeaningfulValue(value)) return fallback;
  const localized = splitCodeList(value)
    .map((item) => localizeError(item))
    .filter(Boolean);
  return localized.length ? localized.join("；") : fallback;
}

export function summarizeLocalizedList(
  value,
  {
    fallback = "当前暂无说明",
    limit = 3,
    suffix = "等",
  } = {},
) {
  if (!hasMeaningfulValue(value)) return fallback;
  const localized = splitCodeList(value)
    .map((item) => localizeError(item))
    .filter(Boolean);
  if (!localized.length) return fallback;
  if (localized.length <= limit) return localized.join("；");
  return `${localized.slice(0, limit).join("；")} ${suffix}`;
}

export function stateOrFallback(value, fallback = "待确认") {
  if (!hasMeaningfulValue(value)) return fallback;
  return readableState(value, fallback);
}

export function meaningfulEntries(value) {
  if (!value || typeof value !== "object") return [];
  return Object.entries(value).filter(([, item]) => {
    if (item === null || item === undefined) return false;
    if (typeof item === "string") return item.trim() !== "";
    return true;
  });
}
