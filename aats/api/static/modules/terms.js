const TERM_MAP = {
  anonymous: "未登录",
  authenticated: "已登录",
  unknown: "待确认",
  none: "暂无",
  ok: "正常",
  healthy: "运行正常",
  warning: "风险偏高",
  critical: "高风险",
  pass: "通过",
  fail: "未通过",
  not_applicable: "不适用",
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
  only_reduce: "仅允许减仓",
  derivatives_only_reduce: "仅允许减仓",
  symbol_window: "按持仓时间窗归因",
  no_matching_position_window: "没有匹配的持仓时间窗",
  ambiguous_symbol_overlap: "同一时间存在多条持仓，资金费无法唯一归因",
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
  exchange_live_spot: "OKX 实盘现货",
  exchange_live_derivatives: "OKX 实盘合约",
  exchange_live_reserved: "预留真实资金线路",
  guarded_live: "守护模式",
  directional: "方向策略",
  smart_arbitrage: "智能套利",
  spot_grid: "现货网格",
  dca: "定投",
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
  override_target: "直接接管目标",
  hold_current: "保持当前仓位",
  advisory_only: "仅参考，不直接执行",
  protective_fallback: "保护性回退",
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
  aggressive_optimization: "激进优化切档",
  same_risk_optimization: "同风险优化切档",
  conservative_rebalance: "保守收缩切档",
  emergency_safety: "紧急安全切档",
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
  baseline_multi_factor_alpha: "多因子基线信号",
  regime_trend: "市场处于趋势阶段",
  regime_uncertain: "市场状态偏不确定",
  mtf_alignment_long: "多周期方向偏多",
  mtf_alignment_short: "多周期方向偏空",
  mtf_alignment_flat: "多周期方向缺少一致性",
  mtf_alignment_mixed: "多周期方向没有形成一致性",
  alpha_momentum_support: "动量因子支持当前方向",
  alpha_trend_support: "趋势因子支持当前方向",
  alpha_regime_support: "市场状态因子支持当前方向",
  alpha_multi_timeframe_support: "多周期因子支持当前方向",
  regime_breakout: "市场处于突破阶段",
  regime_range: "市场处于震荡阶段",
  volatility_targeting_reduced_size: "波动率目标要求缩小仓位",
  volatility_targeting_expanded_size: "波动率目标允许放大仓位",
  microstructure_not_strong_enough: "微观结构强度不足",
  microstructure_conflicts_with_direction: "微观结构与当前方向相冲突",
  microstructure_confirms_long: "微观结构支持偏多方向",
  microstructure_confirms_short: "微观结构支持偏空方向",
  microstructure_neutral: "微观结构暂时中性",
  liquidity_thin: "流动性偏薄",
  low_edge: "净优势偏低",
  min_hold_active: "最小持仓时间尚未结束",
  post_close_cooldown_active: "平仓后的冷却期尚未结束",
  low_edge_cooldown_active: "低净优势冷却期尚未结束",
  alpha_decay_exit: "优势衰减退出",
  alpha_decay_reduce: "优势衰减减仓",
  risk_contraction_exit: "风险收缩减仓",
  emergency_protective_exit: "紧急保护退出",
  directional_strategy_target: "沿用方向策略目标",
  smart_arbitrage_disabled: "智能套利未启用",
  smart_arbitrage_symbol_pair_missing: "没有配置可识别的现货/合约配对标的",
  smart_arbitrage_market_pair_incomplete: "缺少套利双腿行情",
  smart_arbitrage_basis_below_entry_threshold: "当前基差还没有大到值得做套利",
  smart_arbitrage_positive_basis: "当前是正基差，倾向做多现货并做空合约",
  smart_arbitrage_negative_basis: "当前是负基差，倾向做多合约并对冲现货",
  smart_arbitrage_dual_leg_runtime_required: "当前运行时还没有双腿联动执行能力，因此只做建议不自动执行",
  spot_grid_disabled: "现货网格未启用",
  spot_grid_spot_runtime_required: "现货网格只适用于现货运行域",
  spot_grid_regime_blocked: "当前市场不够像震荡区间，现货网格先不接管",
  spot_grid_anchor_history_insufficient: "现货网格缺少足够的锚定价格历史",
  spot_grid_band_invalid: "现货网格带宽无效",
  spot_grid_rebalance_not_required: "当前库存已经接近网格目标，不需要重平衡",
  spot_grid_inventory_target_ready: "现货网格已经算出新的库存目标",
  dca_disabled: "定投未启用",
  dca_spot_runtime_required: "定投只适用于现货运行域",
  dca_price_missing: "当前拿不到可用价格，定投先等待",
  dca_position_cap_reached: "当前定投仓位已经达到上限",
  dca_interval_not_elapsed: "距离上一笔定投还不够久",
  dca_pullback_not_met: "当前还没有回落到定投触发区间",
  dca_tranche_too_small: "当前定投预算换算后的数量太小",
  dca_interval_elapsed: "定投间隔已经满足",
  dca_budget_ready: "定投预算已经准备好",
  active_strategy_family_directional: "当前主策略家族配置为方向策略",
  active_strategy_family_smart_arbitrage: "当前主策略家族配置为智能套利",
  active_strategy_family_spot_grid: "当前主策略家族配置为现货网格",
  active_strategy_family_dca: "当前主策略家族配置为定投",
  directional_family_selected: "当前明确选中了方向策略",
  non_directional_family_ready: "当前非方向策略已经准备好，可以接管本轮目标",
  non_directional_family_advisory_only: "当前非方向策略只提供参考，不会直接执行",
  non_directional_family_hold_current: "当前非方向策略没有生成可执行目标，系统保持当前仓位",
  strategy_family_unknown_fallback_directional: "当前配置的策略家族无法识别，系统回退到方向策略",
  strategy_family_protective_fallback_retained: "虽然当前主策略没有直接接管，但系统保留了方向策略的保护性减仓/退出",
  fee_drag_elevated: "近期手续费拖累偏高",
  operator: "操作员",
  admin: "管理员",
  viewer: "只读用户",
  observing: "持续观察",
  no_data: "暂无数据",
  conservative: "保守",
  normal: "常规",
  contracted: "已收缩",
  floor_contracted: "已收缩到下限",
  safe_mode: "安全模式",
  aggressive: "激进",
  session: "浏览器会话",
  api_key: "API 密钥",
  env_only: "环境文件",
  env_fallback: "环境文件",
  ai_auto: "AI 自动切换",
  rollback: "回滚",
  system_guard: "系统保护",
  registered_profile_only: "只允许已登记策略档位",
  reserved_not_enabled: "保留未启用",
  historical_eval_plus_shadow_guard: "历史对比加影子保护",
  absent: "暂未提供",
  system: "系统",
  phase1_shadow: "影子兼容层",
  risk_control: "风险控制",
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
  replay_history_neutralized: "当前 replay 样本不足，系统按中性处理，不再因此默认偏向更保守的档位",
  offline_optimization_uses_historical_profile_evaluations: "当前档位比较会参考历史评估结果",
  shadow_guard_adjustment_derived_from_latest_ai_performance_reports: "影子评估的长期表现会影响档位比较结果",
  replay_adjustment_derived_from_recent_replay_validations: "最近 replay 校验结果会影响档位比较结果",
  replay_cross_bucket_scoring_enabled_for_symbol_regime_profile: "档位比较会综合标的、市场状态和当前档位的组合评分",
  should_fallback: "建议回退到已登记策略档位",
  "profile is conservative risk level, suitable for financial safety.": "当前策略档位属于保守风险级别，更适合以资金安全为优先。",
  "profile reduces trading frequency to lower fee churn.": "这套策略档位会降低交易频率，减少手续费来回损耗。",
  "profile avoids low-edge trading to improve execution reliability.": "这套策略档位会尽量避开低净优势交易，提升执行可靠性。",
  ui_manual_activate_strategy_profile: "管理员在页面上手动切换了策略档位",
  ui_restore_auto_strategy_profile_control: "管理员在页面上恢复了自动切档逻辑",
  operator_manual_profile_activation: "管理员手动切换了已注册策略档位",
  operator_restore_auto_strategy_profile_control: "管理员已提前结束人工冻结，恢复自动切档逻辑",
  winner_selection_policy_auto_activation: "系统已自动采用当前优胜策略档位",
  diagnostic_only: "仅诊断",
  shadow_translation: "执行层 shadow",
  enabled_live: "允许进入实盘执行",
  guarded_simulated_submit: "守护模拟盘报单",
  guarded_simulated_submit_derivatives: "守护模拟盘合约报单",
  guarded_simulated_dry_run: "守护模拟盘演练",
  guarded_live_submit: "守护实盘报单",
  guarded_live_submit_derivatives: "守护实盘合约报单",
  guarded_live_dry_run: "守护实盘演练",
  guarded_dry_run: "守护演练",
  guarded_blocked: "已被执行器阻断",
  insufficient_data: "样本仍少，先继续观察",
  caution: "收益转弱，建议谨慎",
  failing: "已触发警戒",
  baseline_fallback: "本轮最终回退到基础策略",
  reference_only: "当前只把 AI 当参考",
  final_decision: "当前由 AI 直接给出最终结论",
  final_decision_with_profile_control: "当前由 AI 给出最终结论并联动档位控制",
  continue_small_capital: "继续小资金试盘",
  shrink_trial: "建议缩小试盘规模",
  pause_trial: "建议暂停试盘",
  approve_scale_up: "允许进入下一档资金评审",
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
  runtime_profile_control_removed: "当前运行配置只支持环境文件启动，页面内不再提供旧版运行档位控制。",
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
  only_reduce_required: "当前风控只允许减仓或平仓，不再允许新增暴露。",
  only_reduce_mode_active: "当前没有可执行的减仓空间，本轮不会继续新增暴露。",
  derivatives_margin_usage_requires_only_reduce: "预估保证金占用已经偏高，当前只允许继续减仓或平仓。",
  derivatives_liquidation_gap_requires_only_reduce: "当前最近仓位已经进入强平缓冲区，系统只允许继续减仓或平仓。",
  derivatives_risk_snapshot_missing_requires_only_reduce: "当前拿不到合约风险快照，系统只允许继续减仓或平仓。",
  derivatives_risk_snapshot_missing_grace_active: "当前合约风险快照短时缺失，系统先降档、缩预算并降低执行侵略性。",
  derivatives_risk_snapshot_missing_auto_halt: "合约风险快照持续缺失过久，系统已升级到自动停机。",
  derivatives_margin_buffer_auto_halt: "当前保证金占用已经进入自动停机阈值，系统必须保持暂停。",
  derivatives_liquidation_proximity_auto_halt: "当前最近仓位距离强平过近，系统已经触发自动停机。",
  max_open_orders_reached: "活动委托数达到上限，暂不继续发单。",
  max_target_leverage_exceeded: "请求杠杆超过当前运行配置允许的最大杠杆。",
  liquidation_buffer_breached: "预估保证金占用已经侵蚀到强平缓冲区，系统禁止继续新增暴露。",
  max_gross_notional_per_symbol_exceeded: "单标的总名义敞口超过上限，不能继续扩大该标的仓位。",
  max_pending_notional_per_symbol_exceeded: "单标的待成交名义金额超过上限，不能继续追加该标的挂单。",
  max_total_open_notional_exceeded: "账户总名义敞口超过上限，不能继续新增整体暴露。",
  max_daily_realized_loss_usdt_exceeded: "当日已实现亏损超过上限，当前只允许减仓或平仓。",
  risk_budget_multiplier_applied: "风险预算已经自动收缩。",
  execution_aggressiveness_contracted: "执行侵略性已经自动收缩。",
  safety_profile_required: "当前安全事件已经明确触发。",
  runtime_not_safe_to_trade: "当前运行态不适合继续自动扩张。",
  auto_halt_required: "自动停机条件已经触发。",
  state_freshness_or_reconciliation_degraded: "行情、账户或对账状态不够新鲜，系统先自动收缩。",
  projected_margin_usage_near_hard_cap: "预估保证金占用已经接近硬上限。",
  projected_margin_usage_elevated: "预估保证金占用偏高，系统开始收缩风险预算。",
  current_margin_usage_near_hard_cap: "当前保证金占用已经接近硬上限。",
  current_margin_usage_elevated: "当前保证金占用偏高，系统开始收缩风险预算。",
  liquidation_gap_tight: "最近仓位距离强平过近，系统显著收缩。",
  liquidation_gap_narrowing: "最近仓位距离强平缓冲正在收窄，系统开始收缩。",
  derivatives_order_state_unknown_on_exchange: "本地合约委托在交易所侧无法确认当前状态，系统已进入恢复阻断。",
  derivatives_fill_observed_not_booked: "交易所侧发现了本地未入账的合约成交，系统已进入恢复阻断。",
  derivatives_position_mode_mismatch: "本地合约订单记录的持仓模式与交易所账户真实配置不一致。",
  derivatives_pos_side_mismatch: "本地合约订单记录的持仓方向字段与交易所账户模式不一致。",
  derivatives_exchange_position_without_local_execution_chain: "交易所存在本地执行链无法解释的合约仓位，当前只允许减仓或平仓。",
  derivatives_local_order_missing_from_exchange_open_order_view: "本地仍记录为活动中的合约委托，在交易所当前挂单视图中却找不到。",
  derivatives_exchange_order_status_conflicts_with_local_open_state: "本地记录的合约委托状态与交易所返回的真实状态冲突。",
  derivatives_local_position_mode_differs_from_exchange_account_configuration: "本地订单记录的持仓模式与交易所账户配置不一致。",
  derivatives_local_pos_side_conflicts_with_exchange_position_mode: "本地订单记录的持仓方向字段与交易所账户模式不一致。",
  derivatives_exchange_fill_observed_without_local_booking: "交易所发现了本地没有入账的合约成交。",
  derivatives_exchange_position_not_replayed_locally: "交易所仓位没有被本地执行链正确重放出来。",
  derivatives_only_reduce_until_position_reconciled: "在仓位来源未厘清前，系统只允许继续减仓或平仓。",
  derivatives_open_order_state_is_not_confirmed: "当前无法确认部分合约挂单的真实状态。",
  derivatives_fill_reconciliation_is_incomplete: "当前合约成交链还没有完整收敛。",
  derivatives_order_semantics_do_not_match_exchange_account_mode: "本地合约订单语义与交易所账户模式不一致。",
  derivatives_new_open_orders_blocked_until_position_reconciled: "在异常仓位收敛前，系统不会继续新增合约暴露。",
  local_position_margin_divergence: "本地持仓快照记录的保证金占用与交易所真实仓位保证金不一致。",
  local_position_margin_profile_divergence: "本地持仓快照记录的全仓/逐仓模式与交易所真实仓位模式不一致。",
  local_position_margin_differs_from_exchange_position_margin: "本地持仓快照记录的保证金占用、维持保证金或强平价与交易所不一致。",
  local_position_margin_mode_differs_from_exchange_position_margin_mode: "本地持仓快照记录的全仓/逐仓模式与交易所仓位模式不一致。",
  exchange_margin_state_differs_from_local_snapshot: "交易所仓位保证金状态和本地快照不一致，需要先核对保证金占用与释放。",
  cross_isolated_margin_mode_is_not_confirmed: "当前无法确认本地和交易所的全仓/逐仓模式一致，系统不应继续盲目交易。",
  okx_account_mode_incompatible_with_derivatives: "当前 OKX 账户模式不支持合约自动交易。",
  okx_position_mode_missing: "当前拿不到 OKX 持仓模式，系统不会盲目继续报单。",
  okx_position_margin_mode_conflicts_with_runtime_margin_mode: "交易所现有合约仓位的全仓/逐仓模式与当前运行配置不一致。",
  okx_td_mode_incompatible_with_derivatives: "合约报单必须使用全仓或逐仓模式，不能使用现金模式。",
  okx_td_mode_margin_mode_mismatch: "本地合约报单的交易模式和保证金模式不一致，系统已阻断本次报单。",
  okx_position_mode_mismatch: "本地订单记录的持仓模式和 OKX 账户真实持仓模式不一致。",
  okx_pos_side_missing_for_long_short_mode: "双向持仓模式下必须显式给出多头或空头方向。",
  okx_pos_side_disallowed_for_net_mode: "净持仓模式下不允许携带显式多空方向字段。",
  okx_pos_side_mismatch_with_position_intent: "订单方向字段和本地仓位意图不一致，系统已阻断本次报单。",
  okx_reduce_only_required_by_risk: "当前风控只允许减仓或平仓，这笔单如果发出会新增暴露。",
  okx_reduce_only_without_reducible_position: "当前没有可减的对应仓位，不能发送 reduce-only 报单。",
  okx_reduce_only_would_increase_exposure: "这笔 reduce-only 报单会超过可减仓位，继续执行会变成反手开仓。",
  okx_close_only_requires_reduce_only: "close-only 报单必须同时满足 reduce-only 语义。",
  okx_close_only_without_reducible_position: "当前没有可平的对应仓位，不能发送 close-only 报单。",
  okx_close_only_exceeds_reducible_position: "这笔 close-only 报单数量超过可平仓位，继续执行会变成新增暴露。",
  okx_leverage_exceeds_instrument_limit: "请求杠杆超过交易所该合约的产品限制。",
  okx_order_size_exceeds_instrument_limit: "请求下单数量超过交易所该合约允许的单笔上限。",
  okx_order_quantity_below_min_size: "请求下单数量在交易所最小下单量以下，系统已阻断本次报单。",
  operator_rebaseline_required: "当前账实状态需要人工确认并重建基线后，才能继续交易。",
  rebaseline_in_progress: "基线重建进行中，完成前不要恢复交易。",
  phase1_shadow_lagging: "影子兼容层仍有积压，恢复前需要先确认影子同步已经追平。",
  phase1_shadow_degraded: "影子兼容层最近写入失败，恢复前需要先排查兼容层状态。",
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
  okx_simulated_trading_required: "当前 OKX 模拟盘/实盘环境与运行配置不一致。",
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
  strategy_profile_cold_start_lock_active: "当前仍处于档位控制冷启动阶段，暂不自动切换到新的盈利档。",
  strategy_profile_ai_assist_disagrees: "AI 辅助意见与系统候选不一致，当前先保守处理，不自动切换策略档位。",
  strategy_profile_candidate_requires_more_confirmations: "同一候选策略档位出现次数还不够，当前需要更多连续评估确认。",
  strategy_profile_min_active_duration_not_reached: "当前策略档位生效时间还不够长，先避免过早再次切换。",
  strategy_profile_score_delta_below_threshold: "候选策略档位相对当前策略档位的分数优势还不够明显，暂不自动切换。",
  strategy_profile_requires_more_realized_trades: "当前已闭合交易样本还不够，暂不允许自动切换策略档位。",
  strategy_profile_requires_more_replay_validations: "当前 replay 校验样本还不够，暂不允许自动切换策略档位。",
  strategy_profile_safety_profile_requires_explicit_trigger: "安全降级档只在明确安全事件触发时才允许自动切入。",
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
  ai_shadow_underperformed_baseline: "最近策略层 shadow 落后于基础策略。",
  ai_shadow_fee_drag_worse: "策略层 shadow 的手续费拖累更高。",
  ai_shadow_churn_worse: "策略层 shadow 的来回交易更多。",
  operator_manual_ai_mode_override: "管理员已手动覆盖当前 AI 运行模式。",
  operator_manual_ai_mode_override_expired: "人工覆盖冻结时间已结束，系统恢复为自动运行模式逻辑。",
  operator_manual_ai_mode_override_cleared: "管理员已提前结束人工覆盖，系统恢复为自动运行模式逻辑。",
  operator_review_restore_ai: "人工确认恢复 AI 决策。",
  operator_review_degrade_to_baseline: "人工确认改为仅基础策略继续运行。",
  operator_restore_ai: "人工确认恢复 AI 决策。",
  operator_degrade_to_baseline: "人工确认改为仅基础策略继续运行。",
  outcome_review_recovered: "策略层 shadow 结果已恢复正常。",
  provider_recovered: "模型服务已恢复。",
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
  ai_post_close_cooldown_active: "平仓后的冷却期尚未结束，AI 不能立即再次开仓。",
  ai_low_edge_cooldown_active: "低净优势冷却期尚未结束，AI 暂不继续开仓。",
  no_forward_validation_rows: "当前还没有足够的已完成成交，暂时无法做前向验证。",
  insufficient_forward_validation_sample: "最近一个观察周期的样本还不够，先继续观察，不适合下强结论。",
  negative_net_realized_pnl: "最近一个观察周期净收益转负，说明试盘边际开始变弱。",
  execution_quality_or_fee_threshold_breached: "最近一个观察周期的执行质量或手续费拖累已经超过允许范围。",
  forward_validation_loss_limit_breached: "最近一个观察周期已经触碰试盘亏损上限。",
  trial_guard_not_enabled: "试盘守护还没启用，所以当前不适合直接加资金。",
  trial_profile_not_active: "当前不在试盘档位，先不要做放量判断。",
  runtime_halted: "系统当前处于暂停状态，先处理暂停原因。",
  recovery_not_safe_to_trade: "当前恢复状态还不允许继续自动交易。",
  manual_review_required: "当前仍有人工复核要求，先处理复核再谈放量。",
  active_execution_blockers_present: "当前仍有执行阻断项没有清掉。",
  trial_guard_breached: "试盘守护已经触发警戒，当前不适合继续放量。",
  forward_validation_pause: "最近周期已经达到建议暂停的条件。",
  forward_validation_shrink: "最近周期更适合先缩容观察。",
  forward_validation_still_observing: "最近周期还在观察期，样本还不够稳。",
  forward_validation_stable: "最近周期暂时稳定，可以继续积累样本。",
  healthy_period_requirement_not_met: "连续健康周期还没达标，现在谈放量还太早。",
  scale_up_requirements_met: "样本、恢复状态和执行质量都达到进入下一档资金评审的条件。",
  execution_parameter_suggestions_disabled: "执行建议功能当前关闭。",
  diagnostic_only_no_live_execution: "当前只记录建议，不允许进入真实执行。",
  shadow_translation_preview_only: "当前只生成执行层 shadow 预演，不改写真实委托。",
  planner_boundary_disabled: "执行器边界关闭了 AI 建议下探。",
  planner_recorded_suggestion_only: "执行器只保留建议供诊断使用。",
  planner_translated_execution_preview: "执行器已生成执行层 shadow 预演。",
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
    case "only_reduce":
    case "derivatives_only_reduce":
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
  if (recovery.recovery_state === "only_reduce" || recovery.only_reduce_required) return "仅允许减仓";
  if (recovery.safe_to_trade) return "可交易";
  if (recovery.halted && recovery.resume_eligible) return "待恢复";
  if (recovery.halted) return "已暂停";
  return "已阻断";
}

export function recoveryStatusLabel(recovery = {}) {
  if (recovery.recovery_state === "only_reduce" || recovery.only_reduce_required) return "仅允许减仓";
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
  if (recovery.recovery_state === "only_reduce" || recovery.only_reduce_required) return "仅允许减仓";
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
  if (recovery.recovery_state === "only_reduce" || recovery.only_reduce_required) {
    return recoveryReasonText
      ? `当前只允许减仓或平仓。${recoveryReasonText}`
      : "当前只允许减仓或平仓，系统不会继续新增暴露。";
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
