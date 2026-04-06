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

  async function updateOperatorUserRole(username) {
    const user = findOperatorUser(username);
    if (!user) return;
    const nextRole = windowRef.prompt("请输入新的角色：viewer / operator / admin", user.role || "viewer");
    if (!nextRole || nextRole === user.role) return;
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

  async function resetOperatorPassword(username) {
    const password = windowRef.prompt(`请输入 ${username} 的新密码`);
    if (!password) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, `正在重置 ${username} 的密码…`);
    try {
      await requestJson(`/auth/users/${encodeURIComponent(username)}`, {
        method: "PATCH",
        body: { password },
      });
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
      "change-user-role": (value) => updateOperatorUserRole(value),
      "reset-user-password": (value) => resetOperatorPassword(value),
      "delete-user": (value) => deleteOperatorUser(value),
    },
    createOperatorUser,
  };
}
