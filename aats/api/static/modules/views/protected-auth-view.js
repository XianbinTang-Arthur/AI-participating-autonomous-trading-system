import { callout, pill, surfaceCard } from "../components.js";
import { localizeError } from "../terms.js";

function authBlockedHeadline(accessState, errorCode) {
  if (accessState === "transport_blocked") return "当前入口无法建立安全会话";
  if (errorCode === "operator_admin_access_required") return "当前账号没有管理员权限";
  if (errorCode === "operator_write_access_required" || errorCode === "operator_write_auth_required") {
    return "当前账号没有执行权限";
  }
  return "当前会话未建立或已失效";
}

function authBlockedCopy(accessState, errorCode) {
  if (accessState === "transport_blocked") {
    return localizeError(errorCode || "operator_https_required_for_secure_session");
  }
  return localizeError(errorCode || "operator_auth_required");
}

export function renderProtectedAuthBlockedView({
  viewLabel = "当前页面",
  authSummary = {},
  session = {},
  authProviders = {},
} = {}) {
  const accessState = authSummary.access_state || "auth_required";
  const errorCode = authSummary.primary_error
    || authSummary.auth_blocked_reason
    || authProviders.auth_blocked_reason
    || (session.authenticated ? "operator_auth_required" : "operator_auth_required");
  const headline = authBlockedHeadline(accessState, errorCode);
  const copy = authBlockedCopy(accessState, errorCode);
  const pills = [
    pill(`访问页：${viewLabel}`, "info"),
    pill(accessState === "transport_blocked" ? "需要 HTTPS" : "需要有效会话", accessState === "transport_blocked" ? "warning" : "neutral"),
  ];
  const nextStep =
    accessState === "transport_blocked"
      ? "请改用 HTTPS 入口重新打开控制台，再重新登录。"
      : errorCode === "operator_admin_access_required"
        ? "请切换到管理员账号后重试。"
        : errorCode === "operator_write_access_required" || errorCode === "operator_write_auth_required"
          ? "请切换到具有写入权限的账号后重试。"
          : "请重新登录后再刷新当前页面。";

  return surfaceCard({
    title: `${viewLabel} 当前不可访问`,
    kicker: "受保护数据已阻断",
    copy: "当前页面依赖受保护控制面数据。会话未建立成功或当前入口不满足安全传输要求时，不再继续展示旧状态或默认值。",
    classes: "surface-card--auth-blocked",
    content: [
      callout({ title: headline, copy, pills }),
      `<p class="meta-copy">${nextStep}</p>`,
      (errorCode === "operator_admin_access_required" || errorCode === "operator_write_access_required")
        ? ""
        : `<div class="stack-actions"><a class="secondary-button" href="/login">前往登录页</a></div>`,
    ].join(""),
  });
}
