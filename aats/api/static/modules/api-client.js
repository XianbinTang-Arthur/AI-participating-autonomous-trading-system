import { localizeError } from "./terms.js";

// 超时阈值收紧历史：原本 DEFAULT_TIMEOUT_MS=60s / DEFERRED=120s 是在
// decision_engine.run_cycle 还会堵塞 event loop 15–30s 的时代定的容错窗口。
// 现在 memory_bus.publish_envelope 和 orchestrator.run_cycle 里的 sync
// 部分都已经 `asyncio.to_thread` 化，正常主 bundle 的后端处理时间已经收敛到
// 秒级（冷启动 < 3s、稳定态 < 1s），保留 60s 只会让"真出问题"的情况晚 30s 才被
// 用户感知。收紧到：
//   - 主 bundle 30s：覆盖 p99 + event loop 轻微卡顿，仍然给故障留足诊断窗口。
//   - deferred 45s：覆盖 trial review / shadow eval / guarded-live preflight
//     这几类"背景慢报告"，它们是 best-effort 填充，超时不影响主页面交互。
// 注意：任何"允许 loading 转圈 1 分钟再失败"的需求要走 options.timeout 显式覆盖，
// 不要回退这里的默认值。
const DEFAULT_TIMEOUT_MS = 30_000;
export const DEFERRED_BUNDLE_TIMEOUT_MS = 45_000;

export async function requestJson(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  // We always own the AbortController so the timeout can fire. If the caller
  // also supplied a signal (e.g. refreshDashboard's supersede controller),
  // chain it into ours so either source can trigger abort. The previous
  // implementation silently dropped the timeout when options.signal was
  // provided, which defeated the purpose of having a deadline.
  const timeoutMs = options.timeout ?? DEFAULT_TIMEOUT_MS;
  const controller = new AbortController();
  const timeoutId = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : undefined;

  let externalAbortForwarder = null;
  if (options.signal) {
    if (options.signal.aborted) {
      // #18 修复：原本这里只调 controller.abort() 就继续往下走 fetch()。虽然
      // fetch(signal=aborted) 会立即拒绝，但这条路径和下面 try 块的正常分支风格
      // 不对称、还要走一轮异步 reject。现在直接 throw AbortError，调用方立即感知。
      if (timeoutId !== undefined) clearTimeout(timeoutId);
      const abortError = new Error("请求在发起前已被外部信号取消。");
      abortError.name = "AbortError";
      throw abortError;
    }
    externalAbortForwarder = () => controller.abort();
    options.signal.addEventListener("abort", externalAbortForwarder);
  }

  try {
    // 网络层自动重试：当浏览器在已被服务端关闭的 keep-alive 连接上发请求时，
    // 会立即得到 TypeError("Failed to fetch")。这类错误可以安全地在新连接上
    // 重试一次。只对 GET 请求做重试（幂等），且仅重试 TypeError（网络层故障），
    // 不重试 AbortError（超时/取消）或 HTTP 4xx/5xx（服务端明确拒绝）。
    const method = options.method || "GET";
    const fetchOpts = {
      method,
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      credentials: "same-origin",
      signal: controller.signal,
    };
    let response;
    try {
      response = await fetch(path, fetchOpts);
    } catch (fetchError) {
      if (
        method === "GET" &&
        fetchError instanceof TypeError &&
        !(fetchError.name === "AbortError") &&
        !controller.signal.aborted
      ) {
        // eslint-disable-next-line no-console
        console.warn("[api-client] 网络层错误，自动重试一次", fetchError.message);
        response = await fetch(path, fetchOpts);
      } else {
        throw fetchError;
      }
    }

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
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
    if (externalAbortForwarder && options.signal) {
      options.signal.removeEventListener("abort", externalAbortForwarder);
    }
  }
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
  } catch (parseError) {
    // 解析失败 fallback 到原始文本是有意为之：后端部分 endpoint 在 4xx/5xx 时会返回纯
    // 文本错误信息而不是 JSON。把异常落到 debug 级别，既保留诊断痕迹又不干扰正常流程。
    // eslint-disable-next-line no-console
    console.debug("[api-client] safeJsonParse 回退到原始文本", parseError);
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
