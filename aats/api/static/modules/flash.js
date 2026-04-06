// modules/flash.js
//
// 集中管理 state.flash 的写入和"动作进行中"的人工 guard。
//
// 为什么需要这层 helper：
//
// 1. setFlash —— 防止 sticky-flash 设计的隐式约定被破坏
//    state.flash 通过 shell-renderer.js 的 renderBanners + tickFlashExpiry
//    机制做 lazy 8s TTL 自动清理。这要求 *每次* 设置 flash 都用一个 *全新*
//    对象，而不是去 mutate 已有的 state.flash —— 否则上一次的私有 _expiresAt
//    字段会被保留下来，新 flash 一上来就可能立即过期。
//
//    所以禁止散落各处的 `state.flash = { tone, message }` 字面量赋值，
//    统一走 setFlash 之后，未来若需要给 flash 打 trace id / source / debug
//    标记，也只用改这一处。
//
//    特例：shell-renderer.js 的 renderBanners 会通过 `state.flash._expiresAt`
//    字段做 lazy TTL 标注。这是 sticky-flash 的内部实现协议，setFlash 不应
//    包含此字段，shell-renderer 之外的任何模块也不应读写它。isFlashLive 是
//    封装这一协议的唯一外部读取入口。
//
// 2. clearFlash —— 与 setFlash 配对，避免 `state.flash = null` 散落
//
// 3. isFlashLive —— sticky-flash 是否仍处在 8s TTL 窗口内
//    给 dashboard-refresh.js 的"manual refresh 落地时是否要覆盖屏幕上原有
//    flash"判断用。直接 `!state.flash` 检查会漏掉"_expiresAt 已过去但
//    tickFlashExpiry 还没 tick 到"的 1 秒窗口，造成 manual 的"页面数据已
//    刷新"被沉默吞掉，原本的 flash 同时被 lazy expire 清空的情况。
//
// 4. ensureNotBusy —— 收敛散落各处的 actionInFlight guard
//    之前 admin-actions / app.js / risk-actions 内部各自复制了同样一段
//    "如果 actionInFlight 就 set 排队提示并 return false" 的逻辑。现在统一
//    收敛到这个 helper（共 11 处调用站点），如果未来要改"忙时提示文案"或者
//    要把 guard 升级成 promise-queue，也只用改这一处。
//
//    调用密集的模块（如 admin-actions.js）可以本地包一层 zero-arg thunk
//    `function ensureNotBusy() { return ensureNotBusyHelper(state, renderBanners); }`
//    让 5+ 处 call site 短一些，这是推荐用法。
//
//    注意：ensureNotBusy 失败时会立即 setFlash "请等待..." 覆盖屏幕上原有的
//    success flash（仍在 8s TTL 内）。这是有意行为：用户错误地双击需要立即
//    被提醒，而 success flash 在用户已经看见之后被覆盖是可以接受的代价。

const FLASH_DEFAULT_TTL_MS = 8000;

export function setFlash(state, tone, message) {
  state.flash = { tone, message };
}

export function clearFlash(state) {
  state.flash = null;
}

export function isFlashLive(state) {
  if (!state.flash) return false;
  // 没有 _expiresAt 表示 renderBanners 还没给它打过 TTL 戳 —— 视为"全新刚
  // 设置、还没被任何 render 消费过"，必然是 live 的。
  if (!state.flash._expiresAt) return true;
  return Date.now() < state.flash._expiresAt;
}

export function ensureNotBusy(state, renderBanners) {
  if (!state.actionInFlight) return true;
  setFlash(state, "info", "当前已有人工控制请求在提交，请等待上一次完成。");
  renderBanners();
  return false;
}

export { FLASH_DEFAULT_TTL_MS };
