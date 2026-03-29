const REFRESH_LOCKED_FLAG = "refreshLocked";
const REFRESH_TITLE_FLAG = "refreshLockedTitle";
const MISSING_TITLE_SENTINEL = "__refresh_title_missing__";

export function syncRefreshDisabledButtons({
  roots = [],
  refreshing = false,
  reason = "当前区域正在刷新，请等待刷新完成后再操作。",
} = {}) {
  const targets = Array.isArray(roots) ? roots : [roots];
  targets.forEach((root) => {
    if (!root || typeof root.querySelectorAll !== "function") return;
    Array.from(root.querySelectorAll("button")).forEach((button) => {
      syncRefreshDisabledButton(button, { refreshing, reason });
    });
  });
}

function syncRefreshDisabledButton(button, { refreshing, reason }) {
  if (!button || typeof button !== "object") return;
  if (refreshing) {
    lockButtonForRefresh(button, reason);
    return;
  }
  unlockButtonAfterRefresh(button);
}

function lockButtonForRefresh(button, reason) {
  if (button.disabled) return;
  const previousTitle = typeof button.getAttribute === "function" ? button.getAttribute("title") : null;
  if (button.dataset) {
    button.dataset[REFRESH_LOCKED_FLAG] = "true";
    button.dataset[REFRESH_TITLE_FLAG] = previousTitle === null ? MISSING_TITLE_SENTINEL : previousTitle;
  }
  button.disabled = true;
  if (typeof button.setAttribute === "function") {
    button.setAttribute("title", reason);
  }
}

function unlockButtonAfterRefresh(button) {
  if (!button.dataset || button.dataset[REFRESH_LOCKED_FLAG] !== "true") return;
  button.disabled = false;
  const previousTitle = button.dataset[REFRESH_TITLE_FLAG];
  if (typeof button.removeAttribute === "function" && previousTitle === MISSING_TITLE_SENTINEL) {
    button.removeAttribute("title");
  } else if (typeof button.setAttribute === "function") {
    button.setAttribute("title", previousTitle || "");
  }
  delete button.dataset[REFRESH_LOCKED_FLAG];
  delete button.dataset[REFRESH_TITLE_FLAG];
}
