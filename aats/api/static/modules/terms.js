const TERM_MAP = {
  anonymous: "未登录",
  authenticated: "已登录",
  unknown: "未知",
  ok: "正常",
  healthy: "健康",
  degraded: "降级",
  blocked: "阻断",
  halted: "已暂停",
  ready: "就绪",
  enabled: "已启用",
  disabled: "未启用",
  active: "生效中",
  pending: "待处理",
  review_required: "需要人工确认",
  resume_blocked: "当前不能恢复交易",
  normal_operation: "正常运行",
  rebaseline_completed: "基线已重建",
  created: "已创建",
  submitting: "提交中",
  submitted: "已提交",
  partially_filled: "部分成交",
  filled: "已成交",
  cancel_pending: "撤单中",
  canceled: "已撤单",
  failed: "失败",
  rejected: "已拒绝",
  expired: "已过期",
  local: "本地",
  exchange: "交易所",
  paper_local: "本地模拟",
  exchange_simulated_spot: "交易所模拟现货",
  exchange_simulated_derivatives: "交易所模拟合约",
  exchange_live_reserved: "预留真实交易线路",
  guarded_live: "受保护运行",
  derivatives: "合约",
  spot: "现货",
  cross: "全仓",
  isolated: "逐仓",
  cash: "现货现金",
  buy: "买入",
  sell: "卖出",
  market: "市价",
  limit: "限价",
  long: "多头",
  short: "空头",
  flat: "空仓",
  hold: "继续持有",
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
  viewer: "只读用户",
  session: "会话",
  api_key: "API Key",
  env_fallback: "环境配置",
  yes: "是",
  no: "否",
};

const ERROR_MAP = {
  operator_auth_required: "当前操作需要先登录。",
  operator_write_auth_required: "当前会话没有写入权限。",
  operator_write_access_required: "当前角色没有执行该操作的权限。",
  operator_admin_access_required: "当前角色不是管理员，不能执行该操作。",
  operator_login_failed: "用户名或密码错误。",
  operator_session_auth_not_configured: "系统没有启用浏览器会话登录。",
  operator_auth_disabled: "当前没有启用操作员认证。",
  operator_last_admin_required: "系统至少需要保留一个启用中的管理员。",
  operator_self_disable_forbidden: "不能禁用当前登录的管理员账户。",
  operator_self_delete_forbidden: "不能删除当前登录的管理员账户。",
  rebaseline_not_supported_for_runtime_profile: "当前运行配置不支持人工重建基线。",
  runtime_profile_control_disabled: "当前运行配置仍由环境文件控制，页面内不能直接改。",
  kill_switch_active: "系统已被人工暂停，当前不允许继续交易。",
  reconciliation_halt_required: "最新对账要求暂停交易，需先人工确认。",
  reconciliation_stale: "对账结果已经过期，需要重新校验。",
  market_data_stale: "市场数据已经过期，策略不应继续发单。",
  account_snapshot_missing: "账户快照缺失，无法确认当前账户状态。",
  insufficient_quote_balance: "可用资金不足，当前不满足开仓或加仓条件。",
  insufficient_base_balance: "可用标的资产不足，当前不满足卖出条件。",
  insufficient_initial_margin: "保证金不足，当前不满足开仓条件。",
  max_open_orders_reached: "活动订单数量已达上限。",
  local_demo_no_exchange_submission: "当前是本地演示模式，不会向交易所提交订单。",
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
