const REFRESH_LOCKED_FLAG = "refreshLocked";
const REFRESH_TITLE_FLAG = "refreshLockedTitle";
const MISSING_TITLE_SENTINEL = "__refresh_title_missing__";
const REFRESH_LOCKED_CLASS = "is-refresh-locked";
const PANEL_REFRESHING_CLASS = "is-refreshing";

export function syncRefreshDisabledButtons({
  roots = [],
  refreshing = false,
  pendingPanels = {},
  reason = "当前区域正在刷新，请等待刷新完成后再操作。",
  panelReason = "该卡片数据还在补充加载，请稍候再操作。",
} = {}) {
  const targets = Array.isArray(roots) ? roots : [roots];
  targets.forEach((root) => {
    if (!root || typeof root.querySelectorAll !== "function") return;
    syncPendingPanelIndicators(root, pendingPanels);
    Array.from(root.querySelectorAll("button")).forEach((button) => {
      if (shouldIgnoreButton(button)) return;
      const panelPending = isInsidePendingPanel(button, pendingPanels);
      const shouldLock = refreshing || panelPending;
      const lockReason = refreshing ? reason : panelReason;
      syncRefreshDisabledButton(button, { shouldLock, reason: lockReason });
    });
  });
}

function shouldIgnoreButton(button) {
  if (!button || typeof button.hasAttribute !== "function") return false;
  return button.hasAttribute("data-refresh-ignore");
}

function syncPendingPanelIndicators(root, pendingPanels) {
  const panels = Array.from(root.querySelectorAll("[data-panel-key]"));
  panels.forEach((panel) => {
    if (!panel.classList) return;
    const pending = panelHasPendingKey(panel, pendingPanels);
    panel.classList.toggle(PANEL_REFRESHING_CLASS, pending);
  });
}

function panelHasPendingKey(panel, pendingPanels) {
  if (!panel || !pendingPanels) return false;
  const raw = typeof panel.getAttribute === "function" ? panel.getAttribute("data-panel-key") : "";
  if (!raw) return false;
  const keys = String(raw).split(/\s+/).filter(Boolean);
  return keys.some((key) => Boolean(pendingPanels[key]));
}

function isInsidePendingPanel(element, pendingPanels) {
  if (!pendingPanels || !element || typeof element.closest !== "function") return false;
  const panel = element.closest("[data-panel-key]");
  if (!panel) return false;
  return panelHasPendingKey(panel, pendingPanels);
}

function syncRefreshDisabledButton(button, { shouldLock, reason }) {
  if (!button || typeof button !== "object") return;
  if (shouldLock) {
    lockButtonForRefresh(button, reason);
    return;
  }
  unlockButtonAfterRefresh(button);
}

function lockButtonForRefresh(button, reason) {
  const alreadyLocked = button.dataset && button.dataset[REFRESH_LOCKED_FLAG] === "true";
  if (!alreadyLocked) {
    // Do not touch buttons that were already disabled for other reasons (e.g. permissions)
    if (button.disabled) return;
    const previousTitle = typeof button.getAttribute === "function" ? button.getAttribute("title") : null;
    if (button.dataset) {
      button.dataset[REFRESH_LOCKED_FLAG] = "true";
      button.dataset[REFRESH_TITLE_FLAG] = previousTitle === null ? MISSING_TITLE_SENTINEL : previousTitle;
    }
  }
  button.disabled = true;
  if (button.classList) {
    button.classList.add(REFRESH_LOCKED_CLASS);
  }
  if (typeof button.setAttribute === "function") {
    button.setAttribute("title", reason);
  }
}

function unlockButtonAfterRefresh(button) {
  if (!button.dataset || button.dataset[REFRESH_LOCKED_FLAG] !== "true") return;
  button.disabled = false;
  if (button.classList) {
    button.classList.remove(REFRESH_LOCKED_CLASS);
  }
  const previousTitle = button.dataset[REFRESH_TITLE_FLAG];
  if (typeof button.removeAttribute === "function" && previousTitle === MISSING_TITLE_SENTINEL) {
    button.removeAttribute("title");
  } else if (typeof button.setAttribute === "function") {
    button.setAttribute("title", previousTitle || "");
  }
  delete button.dataset[REFRESH_LOCKED_FLAG];
  delete button.dataset[REFRESH_TITLE_FLAG];
}
