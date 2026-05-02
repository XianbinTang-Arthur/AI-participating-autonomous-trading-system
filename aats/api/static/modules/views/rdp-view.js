// 顶级 RDP 治理视图。历史上 RDP 工作台与 "AI 配置" 页共用同一个 tab，
// operator 要先进 AI 配置再向下滚动才能找到 RDP 面板，信息架构把两件不相关
// 的事强耦合在一起。现在把 RDP 拆到独立的顶级 tab "RDP 治理"，ai-config
// 页只保留 AI 决策模式与策略换档控制，各司其职。
//
// 本视图本身是一个薄壳：真正的卡片渲染仍在 rdp-control-panel.js::
// renderRdpControlPanelV2，这里只负责：
//  1. 用 callout 处理 RDP 读接口被权限拦下（operator_auth_required / https
//     required）的场景——和 ai-config-view 里的 resolveRdpAuthError 逻辑
//     保持一致，避免两个入口行为漂移。
//  2. 在真实面板被包在 <section role="region" aria-label=...> 内，让
//     屏幕阅读器能把 "RDP 治理工作台" 当作一个可跳转的 landmark。
import { actorTags, callout, surfaceCard } from "../components.js";
import { localizeError } from "../terms.js";
import { renderRdpControlPanelV2 } from "./rdp-control-panel.js";

const AUTH_PANEL_KEYS = [
  "rdpControl",
  "rdpWorkbenchOverview",
  "rdpWorkbenchItems",
  "rdpWorkbenchAlerts",
  "rdpTuningOverview",
  "rdpTuningProposals",
];

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
  if (blockedReason) {
    return localizeError(blockedReason);
  }
  if (authProviders.auth_enabled && session.authenticated === false) {
    return localizeError("operator_auth_required");
  }
  return "";
}

export function renderRdpView(data) {
  const session = data.session || {};
  const rdpControl = data.rdpControl || {};
  const rdpWorkbenchOverview = data.rdpWorkbenchOverview || {};
  const rdpWorkbenchItems = data.rdpWorkbenchItems || {};
  const rdpWorkbenchAlerts = data.rdpWorkbenchAlerts || {};
  const rdpTuningOverview = data.rdpTuningOverview || {};
  const rdpTuningProposals = data.rdpTuningProposals || {};
  const uiState = data.uiState?.rdp || {};
  const pendingPanels = data.uiHints?.pendingPanels || {};
  const canAdmin = session.role === "admin" || session.identity === "api_key_write";
  const rdpAuthError = resolveRdpAuthError(data);

  if (rdpAuthError) {
    return surfaceCard({
      title: "RDP 访问需要先建立会话",
      kicker: "权限未放行",
      copy: "先到“账户与权限”完成登录，再回到这里审批 / 发布 recommendation。",
      content: callout({
        title: "未登录或会话已过期",
        copy: rdpAuthError,
        pills: [actorTags("system")],
      }),
    });
  }

  // RDP 读接口集体为空（很可能是后端尚未返回）——给一个温和的等待态，
  // 避免空白页误导为"数据已加载完、但什么都没有"。
  if (
    Object.keys(rdpWorkbenchOverview).length === 0
    && Object.keys(rdpWorkbenchItems).length === 0
    && Object.keys(rdpWorkbenchAlerts).length === 0
  ) {
    return surfaceCard({
      title: "RDP 数据暂未就绪",
      kicker: "等待后端",
      copy: "工作台数据正在加载中，或 RDP 读接口尚未成功返回。请稍候刷新。",
      content: callout({
        title: "尚未拿到 workbench 数据",
        copy: "如果长时间不刷新，请检查 rdp-daemon 容器是否启动、DB 是否可达。",
        pills: [actorTags("system")],
      }),
    });
  }

  return renderRdpControlPanelV2({
    rdpControl,
    rdpWorkbenchOverview,
    rdpWorkbenchItems,
    rdpWorkbenchAlerts,
    rdpTuningOverview,
    rdpTuningProposals,
    canAdmin,
    uiState,
    pendingPanels,
  });
}
