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
    signal: options.signal,
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

export async function fetchPanels(specs, options = {}) {
  const results = await Promise.all(
    specs.map(async ([key, path]) => {
      try {
        return [key, { data: await requestJson(path, options), error: null }];
      } catch (error) {
        if (isAbortError(error)) {
          throw error;
        }
        return [key, { data: null, error: error instanceof Error ? error.message : String(error) }];
      }
    })
  );
  return localizePanelResults(Object.fromEntries(results));
}

export async function fetchDashboardBundle(path, options = {}) {
  const payload = await requestJson(path, options);
  const panels =
    typeof payload === "object" && payload !== null && typeof payload.panels === "object" && payload.panels !== null
      ? payload.panels
      : {};
  return localizePanelResults(panels);
}

function safeJsonParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function localizePanelResults(results) {
  return Object.fromEntries(
    Object.entries(results || {}).map(([key, result]) => [
      key,
      {
        data: result?.data ?? null,
        error: localizePanelError(result?.error),
      },
    ])
  );
}

function localizePanelError(error) {
  if (error === null || error === undefined || error === "") return null;
  return localizeError(typeof error === "string" ? error : JSON.stringify(error));
}

function isAbortError(error) {
  return Boolean(error && typeof error === "object" && "name" in error && error.name === "AbortError");
}
