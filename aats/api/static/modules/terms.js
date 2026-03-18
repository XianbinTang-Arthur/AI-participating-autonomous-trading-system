const TERM_MAP = {
  anonymous: "未登录",
  authenticated: "已登录",
  unknown: "未识别",
  ok: "正常",
  healthy: "运行正常",
  degraded: "需要关注",
  blocked: "已阻断",
  halted: "已暂停交易",
  ready: "已通过",
  enabled: "已启用",
  disabled: "未启用",
  active: "正在生效",
  pending: "等待处理",
  review_required: "等待人工确认",
  resume_blocked: "暂不能恢复交易",
  normal_operation: "正常运行",
  rebaseline_completed: "基线确认完成",
  created: "已生成",
  submitting: "正在报单",
  submitted: "已报单",
  partially_filled: "部分成交",
  filled: "全部成交",
  cancel_pending: "正在撤单",
  canceled: "已撤单",
  failed: "执行失败",
  rejected: "已拒绝",
  expired: "已过期",
  local: "本地",
  exchange: "交易所",
  paper_local: "本地模拟撮合",
  exchange_simulated_spot: "OKX 模拟盘现货",
  exchange_simulated_derivatives: "OKX 模拟盘合约",
  exchange_live_reserved: "预留真实资金线路",
  guarded_live: "守护模式",
  derivatives: "合约",
  spot: "现货",
  cross: "全仓",
  isolated: "逐仓",
  cash: "现金模式",
  buy: "买入",
  sell: "卖出",
  market: "市价",
  limit: "限价",
  long: "多头",
  short: "空头",
  flat: "空仓",
  hold: "维持当前仓位",
  executed: "已执行",
  open_long: "开多",
  reduce_long: "减多",
  close_long: "平多",
  open_short: "开空",
  reduce_short: "减空",
  close_short: "平空",
  reverse_to_long: "反手开多",
  reverse_to_short: "反手开空",
  trend: "趋势",
  breakout: "突破",
  range: "震荡",
  uncertain: "不确定",
  operator: "操作员",
  admin: "管理员",
  viewer: "只读账号",
  session: "浏览器会话",
  api_key: "API Key",
  env_fallback: "环境配置",
  yes: "是",
  no: "否",
};

const ERROR_MAP = {
  operator_auth_required: "当前操作需要先登录。",
  operator_write_auth_required: "当前账号只有查看权限，不能执行人工操作。",
  operator_write_access_required: "当前角色不允许执行这项操作。",
  operator_admin_access_required: "只有管理员才能执行这项操作。",
  operator_login_failed: "用户名或密码错误。",
  operator_session_auth_not_configured: "系统没有启用浏览器会话登录。",
  operator_auth_disabled: "当前没有启用操作员认证。",
  operator_last_admin_required: "系统至少需要保留一个启用中的管理员。",
  operator_self_disable_forbidden: "不能禁用当前登录的管理员账户。",
  operator_self_delete_forbidden: "不能删除当前登录的管理员账户。",
  rebaseline_not_supported_for_runtime_profile: "当前运行配置不支持人工重建基线。",
  runtime_profile_control_disabled: "当前运行配置仍由环境文件控制，页面内不能直接改。",
  kill_switch_active: "系统被手动暂停，恢复前不会继续自动交易。",
  reconciliation_halt_required: "最新对账发现高风险差异，系统已要求暂停交易。",
  reconciliation_stale: "对账结果已过期，需要先重新核对。",
  market_connection_down: "行情连接中断，当前不能继续依赖市场数据做交易。",
  market_data_stale: "行情快照已过期，当前禁止继续下单。",
  account_snapshot_missing: "账户快照缺失，暂时无法确认余额、仓位和挂单状态。",
  insufficient_quote_balance: "可用资金不足，当前不满足开仓或加仓条件。",
  insufficient_base_balance: "可卖数量不足，当前不满足卖出条件。",
  insufficient_initial_margin: "可用保证金不足，当前不满足开仓条件。",
  max_open_orders_reached: "活动委托数达到上限，暂不继续发单。",
  operator_rebaseline_required: "当前账实状态需要人工确认并重建基线后，才能继续交易。",
  rebaseline_in_progress: "基线重建进行中，完成前不要恢复交易。",
  resume_blocked: "恢复检查未通过，系统暂不允许继续自动交易。",
  live_submit_disabled: "当前没有开放向交易所正式报单。",
  guarded_execution_dry_run: "当前是只演练不报单模式，系统不会真正下单。",
  guarded_live_blocked_by_default: "当前运行档位默认禁止真实报单。",
  local_demo_no_exchange_submission: "当前是本地演示模式，不会把委托发到交易所。",
  real_market_paper_uses_local_paper_execution: "当前模式只读取真实行情，但成交仍由本地模拟完成。",
  real_money_live_not_supported: "当前版本不支持真实资金自动交易。",
};

export function readableState(value) {
  if (value === null || value === undefined || value === "") return "-";
  return TERM_MAP[String(value).toLowerCase()] || String(value);
}

export function localizeError(value) {
  if (!value) return "-";
  const normalized = String(value).trim();
  return ERROR_MAP[normalized] || TERM_MAP[normalized.toLowerCase()] || normalized;
}

export function toneForRuntimeState(runtimeState) {
  switch (String(runtimeState || "").toLowerCase()) {
    case "healthy":
    case "ok":
    case "ready":
      return "positive";
    case "degraded":
    case "review_required":
      return "warning";
    case "blocked":
    case "halted":
    case "failed":
      return "danger";
    default:
      return "neutral";
  }
}

export function toneForOrderStatus(status) {
  switch (String(status || "").toLowerCase()) {
    case "filled":
    case "submitted":
      return "positive";
    case "partially_filled":
    case "cancel_pending":
    case "submitting":
      return "warning";
    case "failed":
    case "rejected":
      return "danger";
    case "canceled":
    case "blocked":
    case "dry_run":
      return "outline";
    default:
      return "neutral";
  }
}
