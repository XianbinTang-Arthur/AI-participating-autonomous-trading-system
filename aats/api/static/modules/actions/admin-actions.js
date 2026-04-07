import { ensureNotBusy as ensureNotBusyHelper, setFlash } from "../flash.js";

export function createAdminActions({
  beginAction,
  documentRef = document,
  renderBanners,
  refreshDashboard,
  requestJson,
  state,
  windowRef = window,
}) {
  function findOperatorUser(username) {
    return (state.data.operatorUsers?.users || []).find((item) => item.username === username) || null;
  }

  // Local thunk over the shared helper so call sites stay short. The shared
  // helper is the one that mirrors runAction's actionInFlight guard — without
  // it, two operator-admin clicks (or an admin click landing while an
  // unrelated POST is still in flight) would race the auto-refresh and
  // clobber each other's flashes.
  function ensureNotBusy() {
    return ensureNotBusyHelper(state, renderBanners);
  }

  async function createOperatorUser() {
    const username = documentRef.getElementById("operatorCreateUsername")?.value.trim();
    const password = documentRef.getElementById("operatorCreatePassword")?.value;
    const role = documentRef.getElementById("operatorCreateRole")?.value;
    const enabled = documentRef.getElementById("operatorCreateEnabled")?.value === "true";
    if (!username || !password || !role) {
      setFlash(state, "warning", "请完整填写用户名、密码和角色后再创建账号。");
      renderBanners();
      return;
    }
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在创建运维账号…");
    try {
      await requestJson("/auth/users", { method: "POST", body: { username, password, role, enabled } });
      setFlash(state, "info", `已创建账号 ${username}。`);
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  async function toggleOperatorUser(username) {
    const user = findOperatorUser(username);
    if (!user) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, `正在${user.enabled ? "停用" : "启用"} ${username}…`);
    try {
      await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
        method: "PATCH",
        body: { enabled: !user.enabled },
      });
      setFlash(state, "info", `${username} 已${user.enabled ? "停用" : "启用"}。`);
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  // #29 修复后：原本这里用 windowRef.prompt() 采集 role 字符串，现在改为预填到 admin-view
  // 的 #changeRoleForm，由用户显式点击“确认修改角色”触发 confirmUpdateOperatorUserRole。
  function prefillChangeUserRole(username) {
    const user = findOperatorUser(username);
    if (!user) return;
    const usernameInput = documentRef.getElementById("changeRoleUsername");
    const roleSelect = documentRef.getElementById("changeRoleValue");
    if (usernameInput) usernameInput.value = username;
    if (roleSelect) {
      roleSelect.value = user.role || "viewer";
      try {
        roleSelect.focus();
      } catch (error) {
        // 聚焦失败不是致命错误（例如元素暂未挂载），记录但继续。
        // eslint-disable-next-line no-console
        console.warn("[admin-actions] 聚焦 changeRoleValue 失败", error);
      }
    }
    setFlash(state, "info", `已把 ${username} 填入“修改账号角色”表单，请选择新角色后点击确认。`);
    renderBanners();
  }

  async function confirmUpdateOperatorUserRole() {
    const username = documentRef.getElementById("changeRoleUsername")?.value.trim();
    const nextRole = documentRef.getElementById("changeRoleValue")?.value;
    if (!username) {
      setFlash(state, "warning", "请先在账号列表点击“改角色”选中用户。");
      renderBanners();
      return;
    }
    const user = findOperatorUser(username);
    if (!user) {
      setFlash(state, "warning", `未找到账号 ${username}，请刷新后重试。`);
      renderBanners();
      return;
    }
    if (!nextRole || nextRole === user.role) {
      setFlash(state, "warning", "请选择与当前角色不同的新角色。");
      renderBanners();
      return;
    }
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, `正在更新 ${username} 的角色…`);
    try {
      await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
        method: "PATCH",
        body: { role: nextRole },
      });
      setFlash(state, "info", `${username} 的角色已更新为 ${nextRole}。`);
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  // #29 核心修复：原本这里用 windowRef.prompt(`请输入 ${username} 的新密码`) 直接采集明文
  // 密码；prompt 不是 password 类型、不掩码、可被浏览器历史/截屏记录，对审计/敏感部署不合适。
  // 改为预填到 admin-view 的 #resetPasswordForm，由用户在 <input type="password"> 输入后确认。
  function prefillResetOperatorPassword(username) {
    const user = findOperatorUser(username);
    if (!user) return;
    const usernameInput = documentRef.getElementById("resetPasswordUsername");
    const passwordInput = documentRef.getElementById("resetPasswordValue");
    if (usernameInput) usernameInput.value = username;
    if (passwordInput) {
      passwordInput.value = "";
      try {
        passwordInput.focus();
      } catch (error) {
        // eslint-disable-next-line no-console
        console.warn("[admin-actions] 聚焦 resetPasswordValue 失败", error);
      }
    }
    setFlash(state, "info", `已把 ${username} 填入“重置账号密码”表单，请输入新密码后点击确认。`);
    renderBanners();
  }

  async function confirmResetOperatorPassword() {
    const username = documentRef.getElementById("resetPasswordUsername")?.value.trim();
    const password = documentRef.getElementById("resetPasswordValue")?.value;
    if (!username) {
      setFlash(state, "warning", "请先在账号列表点击“重置密码”选中用户。");
      renderBanners();
      return;
    }
    if (!password) {
      setFlash(state, "warning", "请输入新密码后再确认重置。");
      renderBanners();
      return;
    }
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, `正在重置 ${username} 的密码…`);
    try {
      await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
        method: "PATCH",
        body: { password },
      });
      // 成功后立即清空 password input，避免新密码残留在 DOM。
      const passwordInput = documentRef.getElementById("resetPasswordValue");
      if (passwordInput) passwordInput.value = "";
      setFlash(state, "info", `${username} 的密码已重置。`);
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  async function deleteOperatorUser(username) {
    // confirm → ensureNotBusy → beginAction; see activateStrategyProfile
    // in app.js for the canonical walkthrough of this ordering.
    if (!windowRef.confirm(`确认删除账号 ${username} 吗？`)) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, `正在删除 ${username}…`);
    try {
      await requestJson(`/auth/users/${encodeURIComponent(username)}`, { method: "DELETE" });
      setFlash(state, "info", `${username} 已删除。`);
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  return {
    actionHandlers: {
      "toggle-user": (value) => toggleOperatorUser(value),
      // #29：下面这两个“点击账号行”按钮不再立即发 PATCH，而是把 username 预填到专用表单。
      "change-user-role": (value) => prefillChangeUserRole(value),
      "reset-user-password": (value) => prefillResetOperatorPassword(value),
      // 表单里的“确认”按钮才真正发起 PATCH。
      "confirm-change-user-role": () => confirmUpdateOperatorUserRole(),
      "confirm-reset-user-password": () => confirmResetOperatorPassword(),
      "delete-user": (value) => deleteOperatorUser(value),
    },
    createOperatorUser,
  };
}
