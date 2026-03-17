import { localizeError } from "./terms.js";

export async function requestJson(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    credentials: "same-origin",
  });

  const text = await response.text();
  const payload = text ? safeJsonParse(text) : null;
  if (!response.ok) {
    const detail =
      typeof payload === "object" && payload !== null && "detail" in payload
        ? payload.detail
        : text || response.statusText;
    throw new Error(localizeError(typeof detail === "string" ? detail : JSON.stringify(detail)));
  }
  return payload;
}

export async function fetchPanels(specs) {
  const results = await Promise.all(
    specs.map(async ([key, path]) => {
      try {
        return [key, { data: await requestJson(path), error: null }];
      } catch (error) {
        return [key, { data: null, error: error instanceof Error ? error.message : String(error) }];
      }
    })
  );
  return Object.fromEntries(results);
}

function safeJsonParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}
