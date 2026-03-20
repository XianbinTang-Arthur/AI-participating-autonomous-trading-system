const TERM_MAP = {
  anonymous: "未登录",
  authenticated: "已登录",
  unknown: "待确认",
  none: "暂无",
  ok: "正常",
  healthy: "运行正常",
  degraded: "已降级",
  blocked: "阻断中",
  halted: "已暂停",
  ready: "允许执行",
  enabled: "已启用",
  disabled: "未启用",
  active: "当前生效",
  pending: "待处理",
  accepted: "已采纳",
  applied: "已生效",
  rejected: "已拒绝",
  expired: "已过期",
  staged: "待审批",
  staged_for_activation: "已转为待激活",
  available: "可选",
  settings_fallback: "环境文件默认策略",
  review_required: "待人工确认",
  clean: "账实一致",
  soft_mismatch: "轻度差异",
  hard_mismatch: "严重差异",
  info: "信息提示",
  manually_halted: "已暂停，待恢复",
  resume_blocked: "恢复受限",
  normal_operation: "正常运行",
  rebaseline_completed: "基线确认完成",
  created: "已创建",
  submitting: "正在报单",
  submitted: "已报单",
  partially_filled: "部分成交",
  filled: "全部成交",
  cancel_pending: "正在撤单",
  canceled: "已撤单",
  failed: "失败",
  blocked_order: "已阻断",
  rejected_order: "已拒绝",
  local: "本地",
  exchange: "交易所",
  paper_local: "本地模拟",
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
  long: "偏多",
  short: "偏空",
  flat: "空仓",
  hold: "保持当前仓位",
  enter: "建仓",
  scale_in: "加仓",
  reduce: "减仓",
  exit: "退出仓位",
  reverse: "反手",
  executed: "已执行",
  open_long: "开多",
  reduce_long: "减多",
  close_long: "平多",
  open_short: "开空",
  reduce_short: "减空",
  close_short: "平空",
  reverse_to_long: "反手做多",
  reverse_to_short: "反手做空",
  trend: "趋势",
  regime_range: "市场处于震荡区间",
  trend_aggressive: "趋势激进",
  "trend aggressive": "趋势激进",
  trend_normal: "趋势标准",
  "trend normal": "趋势标准",
  trend_strict: "趋势严格",
  "trend strict": "趋势严格",
  breakout: "突破",
  range: "震荡",
  low_volatility: "波动较低",
  range_defensive: "范围防御",
  "range defensive": "范围防御",
  high_volatility: "高波动",
  high_volatility_defensive: "高波动防御",
  "high vol defensive": "高波动防御",
  "high volatility defensive": "高波动防御",
  execution_degraded: "执行质量下降",
  execution_degraded_safe: "执行降级安全",
  "execution safe": "执行降级安全",
  uncertain: "不确定",
  operator: "操作员",
  admin: "管理员",
  viewer: "只读用户",
  observing: "持续观察",
  no_data: "暂无数据",
  conservative: "保守",
  normal: "常规",
  aggressive: "激进",
  session: "浏览器会话",
  api_key: "API 密钥",
  env_fallback: "环境文件",
  ai_auto: "AI 自动切换",
  rollback: "回滚",
  system_guard: "系统保护",
  registered_profile_only: "只允许已登记策略档位",
  reserved_not_enabled: "保留未启用",
  historical_eval_plus_shadow_guard: "历史对比加影子保护",
  absent: "暂未提供",
  system: "系统",
  yes: "是",
  no: "否",
  baseline_only: "仅按基础策略运行",
  ai_assisted: "AI 辅助决策",
  ai_decision_maker: "AI 决策者",
  ai_decision_maker_with_profile_control: "AI 决策者并控制策略档位",
  winner_engine: "系统候选引擎",
  activation_gate: "激活裁决层",
  auto_activation_executed: "自动激活已执行",
  manual_activation_executed: "手动激活已执行",
  manual_profile_activation_executed: "管理员手动切换已执行",
  winner_policy_auto_activation_executed: "优胜策略档位自动激活已执行",
  execution_outcome_recorded: "已记录执行结果",
  pending_activation_executed: "待审批策略档位已激活",
  rollback_executed: "回滚已执行",
  stable_keep_active: "继续保持当前策略档位",
  recommended_not_executed: "已推荐但未执行",
  winner_policy_recommended_not_executed: "优胜策略档位已推荐但未执行",
  auto_rollback_recommended: "建议自动回滚",
  auto_rollback_executed: "自动回滚已执行",
  observe_outcome: "观察结果",
  activate_or_reject: "激活或拒绝",
  keep_current_profile: "保持当前策略档位",
  observe_after_rollback: "观察回滚后表现",
  current_profile_active_and_conservative: "当前策略档位已生效，且策略偏保守",
  financial_safety_priority: "优先考虑资金安全",
  prioritize_financial_safety: "优先考虑资金安全",
  fee_churn_reduction: "优先减少手续费磨损",
  low_fee_churn: "优先降低手续费来回损耗",
  lower_low_edge_trading: "优先减少低净优势交易",
  execution_reliability: "优先保证执行可靠性",
  execution_errors_elevated: "近期执行报错偏多",
  high_volatility_detected: "检测到高波动环境",
  range_regime_detected: "检测到震荡或不确定市场",
  fee_churn_elevated: "手续费消耗偏高",
  trend_signal_supported: "趋势信号仍然成立",
  trend_signal_moderate: "趋势存在，但优势中等",
  trend_signal_strong: "趋势信号很强",
  trend_recovery_detected: "检测到趋势恢复",
  fallback_rule_based_recommendation: "当前建议来自规则回退，而非 AI 直接推荐",
  runtime_safety_degraded: "运行安全状态不足，优先保守处理",
  winner_engine_selected_candidate: "系统候选引擎已选出当前最优策略档位",
  ai_assist_confirms_winner: "AI 辅助意见与系统候选一致",
  ai_assist_disagrees_with_winner: "AI 辅助意见与系统候选不一致",
  ai_assist_only: "AI 当前只参与辅助解释，不直接决定策略档位切换",
  should_fallback: "建议回退到已登记策略档位",
  "profile is conservative risk level, suitable for financial safety.": "当前策略档位属于保守风险级别，更适合以资金安全为优先。",
  "profile reduces trading frequency to lower fee churn.": "这套策略档位会降低交易频率，减少手续费来回损耗。",
  "profile avoids low-edge trading to improve execution reliability.": "这套策略档位会尽量避开低净优势交易，提升执行可靠性。",
  ui_manual_activate_strategy_profile: "管理员在页面上手动切换了策略档位",
  operator_manual_profile_activation: "管理员手动切换了已注册策略档位",
  winner_selection_policy_auto_activation: "系统已自动采用当前优胜策略档位",
  diagnostic_only: "仅诊断",
  shadow_translation: "影子翻译",
  enabled_live: "允许进入实盘执行",
  provider_ready: "模型已就绪",
  provider_not_ready: "模型未就绪",
  maker_bias: "偏被动",
  taker_bias: "偏主动",
  bounded_limit_ioc: "受限限价成交",
  bounded_taker_cap: "受限主动成交",
  not_requested: "未请求",
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
  operator_self_disable_forbidden: "不能停用当前登录的管理员账号。",
  operator_self_delete_forbidden: "不能删除当前登录的管理员账号。",
  rebaseline_not_supported_for_runtime_profile: "当前运行配置不支持人工重建基线。",
  runtime_profile_control_disabled: "当前运行配置仍由环境文件控制，页面内不能直接修改。",
  kill_switch_active: "系统已被手动暂停，恢复前不会继续自动交易。",
  reconciliation_halt_required: "最新对账发现高风险差异，系统已要求暂停交易。",
  reconciliation_stale: "对账结果已经过期，需要先重新校验。",
  market_connection_down: "行情连接中断，当前不能继续依赖市场数据做交易。",
  market_data_stale: "行情快照已经过期，当前禁止继续下单。",
  account_snapshot_missing: "账户快照缺失，暂时无法确认余额、仓位和挂单状态。",
  account_state_stale: "账户状态过期，需要先刷新账户快照。",
  insufficient_quote_balance: "可用资金不足，当前不满足开仓或加仓条件。",
  insufficient_base_balance: "可卖数量不足，当前不满足卖出条件。",
  insufficient_initial_margin: "可用保证金不足，当前不满足开仓条件。",
  max_open_orders_reached: "活动委托数达到上限，暂不继续发单。",
  operator_rebaseline_required: "当前账实状态需要人工确认并重建基线后，才能继续交易。",
  rebaseline_in_progress: "基线重建进行中，完成前不要恢复交易。",
  resume_blocked: "恢复检查未通过，系统暂不允许继续自动交易。",
  investigate_state_divergence: "先排查本地状态与交易所状态为什么不一致。",
  review_and_rebaseline_if_expected: "先人工确认当前状态，确认无误后接受为新基线。",
  review_exchange_bills_and_rebaseline_if_expected: "先核对交易所账单，再在确认无误后接受为新基线。",
  halt_execution_and_investigate_state_divergence: "先保持暂停，并排查账实状态差异。",
  open_order_unsettled: "挂单未收敛",
  position_drift: "仓位漂移",
  fund_transfer: "资金划转",
  manual_activity: "疑似手工操作",
  go_cancel_on_exchange: "建议去交易所核对并撤销异常挂单",
  go_close_position_on_exchange: "建议去交易所核对并处理异常仓位",
  confirm_and_rebaseline: "确认属实后纳入新基线",
  observe_only: "先观察，不建议立即处理",
  live_submit_disabled: "当前没有开放向交易所正式报单。",
  guarded_execution_dry_run: "当前是只演练不报单模式，系统不会真正下单。",
  guarded_live_blocked_by_default: "当前运行策略档位默认禁止真实报单。",
  local_demo_no_exchange_submission: "当前是本地演示模式，不会把委托发到交易所。",
  real_market_paper_uses_local_paper_execution: "当前模式只读取真实行情，但成交仍由本地模拟完成。",
  real_money_live_not_supported: "当前版本不支持真实资金自动交易。",
  strategy_profile_open_orders_present: "当前还有活动委托，不能在委托未收敛时切换策略档位。",
  strategy_profile_pending_revision_missing: "当前没有待审批的策略档位。",
  strategy_profile_revision_missing: "找不到推荐对应的策略档位定义。",
  strategy_profile_recommendation_not_found: "找不到这条策略档位建议。",
  strategy_profile_profile_not_found: "找不到这个可切换的策略档位。",
  strategy_profile_recommendation_expired: "这条策略档位建议已经过期，不能再直接采纳。",
  strategy_profile_already_active: "推荐策略档位已经是当前生效的策略档位，不需要重复切换。",
  strategy_profile_switch_cooldown_active: "刚完成过一次策略档位切换，当前仍处于切换冷却期。",
  strategy_profile_auto_switch_disabled: "当前未开启策略档位自动切换。",
  strategy_profile_auto_switch_confidence_too_low: "这条建议的置信度还不够高，不能自动切换。",
  strategy_profile_auto_switch_same_risk_confidence_too_low: "同风险级别策略档位切换的置信度还不够高，暂不自动切换。",
  strategy_profile_auto_switch_aggressive_confidence_too_low: "切向更激进策略档位需要更高置信度，当前暂不自动切换。",
  strategy_profile_auto_switch_not_allowed: "这套策略档位不允许自动生效，只能人工审批。",
  strategy_profile_manual_approval_required: "这套策略档位要求人工审批后才能生效。",
  strategy_profile_auto_switch_requires_more_conservative_target: "自动切换只允许切向更保守的策略档位。",
  strategy_profile_auto_switch_frozen: "当前策略档位自动切换已被冻结。",
  strategy_profile_ai_assist_disagrees: "AI 辅助意见与系统候选不一致，当前先保守处理，不自动切换策略档位。",
  strategy_profile_candidate_requires_more_confirmations: "同一候选策略档位出现次数还不够，当前需要更多连续评估确认。",
  strategy_profile_min_active_duration_not_reached: "当前策略档位生效时间还不够长，先避免过早再次切换。",
  strategy_profile_score_delta_below_threshold: "候选策略档位相对当前策略档位的分数优势还不够明显，暂不自动切换。",
  strategy_profile_runtime_not_safe_to_trade: "当前运行态并不适合自动调整策略档位。",
  strategy_profile_review_required: "当前仍需人工复核，不能自动切换策略档位。",
  strategy_profile_market_data_stale: "行情快照不新鲜，不能自动切换策略档位。",
  strategy_profile_reconciliation_not_clean: "当前对账未达一致，不能自动切换策略档位。",
  operator_rejected_strategy_profile_recommendation: "管理员已拒绝这条策略档位建议。",
  ai_recommended_more_conservative_profile: "AI 建议切到更保守的策略档位，系统已自动采纳。",
  ai_recommended_same_risk_profile: "AI 建议切换到同风险级别但更匹配当前市场的策略档位，系统已自动采纳。",
  ai_recommended_more_aggressive_profile: "AI 建议切换到更积极的策略档位，系统已自动采纳。",
  ai_degraded_requires_manual_review: "AI 已降级且未开启自动回退，需要人工确认后再恢复 AI 决策链路。",
  ai_auto_downgraded: "AI 已自动降级，当前只保留基础策略决策链路。",
  output_rejected: "AI 输出结构有效，但没有通过交易语义校验。",
  ai_fallback_used: "本轮使用了回退结果，不能让 AI 直接改写基础策略。",
  ai_output_invalid: "AI 输出没有通过校验。",
  ai_confidence_below_threshold: "校准置信度低于 AI 主导模式最低门槛。",
  ai_uncertainty_above_threshold: "不确定性高于 AI 主导模式允许阈值。",
  ai_directional_edge_too_small: "方向优势不够，暂不允许 AI 直接改写基础策略。",
  ai_override_not_recommended: "AI 自己也不建议覆盖基础策略。",
  ai_not_economically_actionable: "预期净优势覆盖不了成本和噪声。",
  ai_regime_not_allowed: "当前市场状态不允许 AI 直接改写基础策略。",
  ai_open_orders_present: "当前还有活动委托，不允许 AI 改写方向。",
  ai_flat_context_requires_stronger_edge: "空仓场景下需要更强的方向优势才能开仓。",
  execution_parameter_suggestions_disabled: "执行建议功能当前关闭。",
  diagnostic_only_no_live_execution: "当前只记录建议，不允许进入真实执行。",
  shadow_translation_preview_only: "当前只生成影子翻译结果，不改写真实委托。",
  planner_boundary_disabled: "执行器边界关闭了 AI 建议下探。",
  planner_recorded_suggestion_only: "执行器只保留建议供诊断使用。",
  planner_translated_execution_preview: "执行器已生成影子翻译预览。",
  bounded_live_translation_applied: "执行器已经把建议限制性地转成真实下单字段。",
  live_translation_not_enabled: "当前没有启用实盘授权。",
  live_translation_requires_limit_cap: "只有能转成价格保护型限价保护的建议才允许进入实盘授权。",
  live_translation_requires_reference_price: "缺少参考价格，不能安全生成实盘价格保护。",
  live_translation_requires_limit_offset: "缺少有效价格偏移，不能生成实盘限价保护。",
  live_translation_requires_slippage_guard: "缺少滑点保护，不能启用受限实盘翻译。",
};

export function readableState(value, fallback = "待确认") {
  if (value === null || value === undefined || value === "") return fallback;
  return TERM_MAP[String(value).toLowerCase()] || String(value);
}

export function localizeError(value, fallback = "当前没有额外说明") {
  if (!value) return fallback;
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

export function toneForReconciliationSeverity(severity) {
  switch (String(severity || "").toLowerCase()) {
    case "clean":
      return "positive";
    case "info":
      return "info";
    case "review_required":
    case "soft_mismatch":
      return "warning";
    case "hard_mismatch":
      return "danger";
    default:
      return "neutral";
  }
}

export function tradingStatusLabel(recovery = {}) {
  if (recovery.safe_to_trade) return "可交易";
  if (recovery.halted && recovery.resume_eligible) return "待恢复";
  if (recovery.halted) return "已暂停";
  return "已阻断";
}

export function recoveryStatusLabel(recovery = {}) {
  if (recovery.safe_to_trade) return "可交易";
  if (recovery.halted && recovery.resume_eligible) return "待恢复";
  if (recovery.review_required) return "待人工确认";
  return "恢复受限";
}

export function reviewStatusLabel(reviewRequired) {
  return reviewRequired ? "待人工确认" : "无需确认";
}

export function reconciliationStatusLabel(reconciliation = {}) {
  if (reconciliation?.halt_required) return "要求停机";
  if (String(reconciliation?.severity || "").toLowerCase() === "clean") return "账实一致";
  return "持续观察";
}

export function permissionStatusLabel(canWrite) {
  return canWrite ? "可写" : "只读受限";
}

export function statusHeadline(label, fallback = "待确认") {
  const value = String(label || "").trim() || fallback;
  return `当前状态：${value}`;
}

export function operationalStatusLabel({
  health = {},
  recovery = {},
  blockers = [],
  reconciliation = null,
  readyLabel = "可交易",
} = {}) {
  if (recovery.halted && recovery.resume_eligible && !recovery.safe_to_trade) return "待恢复";
  if (health.halted || (recovery.halted && !recovery.resume_eligible)) return "已暂停";
  if ((blockers || []).length > 0) return "已阻断";
  if (reconciliation?.halt_required) return "需先完成对账";
  if (recovery.review_required) return "待人工确认";
  if (recovery.safe_to_trade === false) return "恢复受限";
  return readyLabel;
}

export function operationalStatusHeadline(options = {}) {
  return statusHeadline(operationalStatusLabel(options));
}

export function operationalStatusCopy({
  health = {},
  recovery = {},
  blockers = [],
  reconciliation = null,
  recoveryReasonText = "",
  readyCopy = "当前运行正常，可继续关注账户、对账和下一轮策略判断。",
} = {}) {
  if (recovery.halted && recovery.resume_eligible && !recovery.safe_to_trade) {
    return "当前处于手动暂停状态。确认最新对账和账户快照无误后，可直接恢复自动运行。";
  }
  if (health.halted || (recovery.halted && !recovery.resume_eligible)) {
    return "当前处于暂停状态。请先确认暂停原因和系统状态，再决定后续操作。";
  }
  if ((blockers || []).length > 0) {
    return `当前处于阻断状态。请先处理：${localizeError(blockers[0]?.blocker)}。`;
  }
  if (reconciliation?.halt_required) {
    return "当前需先完成对账。确认没有高风险差异后，再决定是否恢复自动运行。";
  }
  if (recovery.review_required) {
    return "当前仍需人工确认。请先核对交易所状态与本地记录，再决定是否接受为新基线。";
  }
  if (recovery.safe_to_trade === false) {
    return recoveryReasonText
      ? `当前处于恢复受限状态。${recoveryReasonText}`
      : "当前处于恢复受限状态。请先满足恢复条件后再恢复自动运行。";
  }
  return readyCopy;
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
