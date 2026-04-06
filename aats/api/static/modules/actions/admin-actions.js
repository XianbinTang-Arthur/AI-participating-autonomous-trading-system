export function createAdminActions({
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

  async function createOperatorUser() {
    const username = documentRef.getElementById("operatorCreateUsername")?.value.trim();
    const password = documentRef.getElementById("operatorCreatePassword")?.value;
    const role = documentRef.getElementById("operatorCreateRole")?.value;
    const enabled = documentRef.getElementById("operatorCreateEnabled")?.value === "true";
    if (!username || !password || !role) {
      state.flash = { tone: "warning", message: "请完整填写用户名、密码和角色后再创建账号。" };
      renderBanners();
      return;
    }
    try {
      await requestJson("/auth/users", { method: "POST", body: { username, password, role, enabled } });
      state.flash = { tone: "info", message: `已创建账号 ${username}。` };
      await refreshDashboard({ manual: true });
    } catch (error) {
      state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
      renderBanners();
    }
  }

  async function toggleOperatorUser(username) {
    const user = findOperatorUser(username);
    if (!user) return;
    try {
      await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
        method: "PATCH",
        body: { enabled: !user.enabled },
      });
      state.flash = { tone: "info", message: `${username} 已${user.enabled ? "停用" : "启用"}。` };
      await refreshDashboard({ manual: true });
    } catch (error) {
      state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
      renderBanners();
    }
  }

  async function updateOperatorUserRole(username) {
    const user = findOperatorUser(username);
    if (!user) return;
    const nextRole = windowRef.prompt("请输入新的角色：viewer / operator / admin", user.role || "viewer");
    if (!nextRole || nextRole === user.role) return;
    try {
      await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
        method: "PATCH",
        body: { role: nextRole },
      });
      state.flash = { tone: "info", message: `${username} 的角色已更新为 ${nextRole}。` };
      await refreshDashboard({ manual: true });
    } catch (error) {
      state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
      renderBanners();
    }
  }

  async function resetOperatorPassword(username) {
    const password = windowRef.prompt(`请输入 ${username} 的新密码`);
    if (!password) return;
    try {
      await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
        method: "PATCH",
        body: { password },
      });
      state.flash = { tone: "info", message: `${username} 的密码已重置。` };
      await refreshDashboard({ manual: true });
    } catch (error) {
      state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
      renderBanners();
    }
  }

  async function deleteOperatorUser(username) {
    if (!windowRef.confirm(`确认删除账号 ${username} 吗？`)) return;
    try {
      await requestJson(`/auth/users/${encodeURIComponent(username)}`, { method: "DELETE" });
      state.flash = { tone: "info", message: `${username} 已删除。` };
      await refreshDashboard({ manual: true });
    } catch (error) {
      state.flash = { tone: "danger", message: error instanceof Error ? error.message : String(error) };
      renderBanners();
    }
  }

  return {
    actionHandlers: {
      "toggle-user": (value) => toggleOperatorUser(value),
      "change-user-role": (value) => updateOperatorUserRole(value),
      "reset-user-password": (value) => resetOperatorPassword(value),
      "delete-user": (value) => deleteOperatorUser(value),
    },
    createOperatorUser,
  };
}
