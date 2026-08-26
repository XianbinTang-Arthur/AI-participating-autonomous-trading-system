import { actorTags, callout, surfaceCard } from "../components.js";
import { localizeError } from "../terms.js";
import { renderRdpControlPanelV3 } from "./rdp-control-panel.js";

const AUTH_PANEL_KEYS = ["rdpWorkspace"];

function isAuthRelatedError(message) {
  const text = String(message || "").trim();
  if (!text) return false;
  return (
    text === "operator_auth_required"
    || text === "operator_https_required_for_secure_session"
    || text === localizeError("operator_auth_required")
    || text === localizeError("operator_https_required_for_secure_session")
  );
}

function resolveRdpAuthError(data) {
  const errors = data.errors || {};
  const firstPanelError = AUTH_PANEL_KEYS
    .map((key) => errors[key])
    .find((message) => isAuthRelatedError(message));
  if (firstPanelError) return localizeError(String(firstPanelError).trim());
  const authProviders = data.authProviders || {};
  const session = data.session || {};
  const blockedReason = authProviders.auth_blocked_reason || session.auth_blocked_reason;
  if (blockedReason) return localizeError(blockedReason);
  if (authProviders.auth_enabled && session.authenticated === false) {
    return localizeError("operator_auth_required");
  }
  return "";
}

export function renderRdpView(data) {
  const session = data.session || {};
  const workspace = data.rdpWorkspace || {};
  const pendingPanels = data.uiHints?.pendingPanels || {};
  const canAdmin = ["admin", "operator"].includes(session.role)
    || session.identity === "api_key_write";
  const authError = resolveRdpAuthError(data);

  if (authError) {
    return surfaceCard({
      title: "RDP 访问需要先建立会话",
      kicker: "权限未放行",
      copy: "先到“账户与权限”完成登录，再回到这里操作 RDP。",
      content: callout({
        title: "未登录或会话已过期",
        copy: authError,
        pills: [actorTags("system")],
      }),
    });
  }

  if (Object.keys(workspace).length === 0) {
    return surfaceCard({
      title: "RDP 工作台暂未就绪",
      kicker: "等待单一快照",
      copy: "后端正在一次性组装运行、研究、治理和发布状态。",
      content: callout({
        title: "正在生成 RDP Workspace V3",
        copy: pendingPanels.rdpWorkspace
          ? "不需要重复点击刷新，页面会在快照完成后自动更新。"
          : "如果长时间没有结果，请检查 RDP 读接口与 governance DB。",
        pills: [actorTags("system")],
      }),
    });
  }

  return renderRdpControlPanelV3({ workspace, canAdmin });
}
