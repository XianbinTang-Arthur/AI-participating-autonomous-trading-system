# Cost Audit — 线上执行记录对账（Path C）

**日期**: 2026-04-19
**执行者**: 独立调研任务（Claude Opus 4.7）
**上下文**: H2 修复（commit `7f55176`, 2026-04-19 12:22:09 -0400）把 `execution_style=bounded_limit_ioc` 从 maker-blend 归为纯 taker。本报告核对修复后生产库 `aats_live_derivatives` 实际执行记录。
**范围**: 纯分析，只读数据库，不改生产代码。

---

## 0. TL;DR

- **生产库 `execution_orders` 总行数仅 28（含 3 条 BLOCKED）**，**全部发生在 2026-04-17**，处于 H2 修复**前 48 小时**。
  **H2 修复后至报告生成时段（2026-04-19 16:22 UTC 起）生产库无任何新订单** — 因此**无法用修复后数据直接验证 fee_resolver 新契约**。
- 仅有的 28 条订单**100% 是 BTC-USDT-SWAP / independent-primary / OKX ordType ∈ {market, ioc}**，执行 fill 全部为 taker 5.000 bps。
  对这批订单而言 H2 修复 **是 no-op**（ordType=market/ioc 两种版本的 fee_resolver 都走 taker 分支，只有 `execution_style==bounded_limit_ioc` 且 ordType 不在 {market,ioc} 时才会分支不同；本批 `execution_style` 字段**全部未落库**）。
- **实际 OKX 账单 vs fee_resolver 预测**：完美对齐，**差异 0.000 bps**（25 笔 fills 全部恰好 5.000 bps）。
- **Slippage 分布**：24 笔可对比订单，mean = -0.467 bps，stdev = 2.246 bps，p95 = +1.494 bps，max 不利 = +2.271 bps。
  总入场成本（fee 5.0 + slippage）mean = **4.533 bps**，p95 = **6.494 bps**，max = **7.271 bps**，全部落在配置阈值 `max_acceptable_cost_bps = 7.5` 以下。
- **关键 observability 缺口**：`execution_orders.raw_payload.execution_style` 顶层字段**全部为 None**；OKX 原始 `feeRate` / `execType` 字段在 adapter 里被丢弃，未存入 `execution_fills.raw_payload.fill_event`。
- **修复本身无回滚风险**（与这批订单无关），但后续真正要用到 `bounded_limit_ioc` 的策略（目前只有 independent 家族会在 passive_first_enabled 的路径上生成）**在本对账窗口内尚未实际触发过**。建议 H2 修复保留，并单独补 observability 改动（见第 7 章）。

---

## 1. 数据窗口与方法

### 1.1 数据源
- Postgres 库: `aats_live_derivatives`（走 `AATS_DB_NAME`，从 `.env.derivatives.live` 加载；用户 `admin`，端口 `127.0.0.1:5432`）
- 核心表: `public.execution_orders`、`public.execution_fills`
- 查询脚本: `scripts/research/cost_audit_01_probe_schema.py` … `cost_audit_06_slippage.py`（本任务新建，只读 SQL）

### 1.2 时间窗口
| 切片 | 范围 | 行数 |
|---|---|---|
| 全部 execution_orders | 2026-04-17 04:49:07 ~ 2026-04-17 17:51:57 +08:00 | 28 |
| 修复前同日（4-19 UTC 12:22 之前） | 2026-04-19 00:00 ~ 2026-04-19 16:22 UTC | 0 |
| 修复后（4-19 UTC 16:22 起） | 2026-04-19 16:22 UTC ~ 现在 | **0** |
| 修复前一周（4-12 ~ 4-19 UTC 16:22） | 7 天 | 28 |

**关键约束**：H2 修复后零订单。**只能用 4-17 的 28 条订单做横向对账**，无法用"修复前后对比"。任务说明的"7-14 天窗口"事实上只有 4-17 一天数据——原因是主线前几周未下单（在之前调研里已有结论：信号弱、baseline 合成 alpha 阈值未放行）。

### 1.3 方法
1. 探测 `execution_orders` / `execution_fills` schema（`cost_audit_01_probe_schema.py`）
2. 枚举 execution_style / order_type / OKX ordType / state 分布（`cost_audit_02_full_analysis.py`）
3. 检查 `raw_payload.fill_event` 与 OKX 原始字段映射（`cost_audit_03_raw_keys.py`, `cost_audit_04_fill_event.py`）
4. 对比实测 fee_bps vs fee_resolver post-fix & pre-fix 两版预测（`cost_audit_05_reconciliation.py`）
5. 用 `submission_payload.referencePrice` vs `average_fill_price` 推导 slippage（`cost_audit_06_slippage.py`）

所有 SQL 都带 `LIMIT` 或小量窗口，纯 SELECT，未对数据库做任何修改。

---

## 2. Fee 分布

### 2.1 按 execution_style（raw_payload 顶层字段）
| execution_style | count |
|---|---|
| `None` (未落库) | **28 (100%)** |

**发现**：当前所有 execution_orders 的 `raw_payload.execution_style` 都是 None。这个顶层字段的落库通路断了，但 `submission_payload.ordType` 被正常保存（market/ioc）——说明 fee_resolver 在运行时仍能基于 `order_type` 正确判定 taker，只是**事后无法从 DB 还原 execution_style 来做回溯审计**。这是一个 observability gap（见第 7.2 章建议）。

### 2.2 按 OKX 实际 ordType（从 `submission_payload.ordType`）
| OKX ordType | count | 占比 |
|---|---|---|
| `market` | 24 | 85.7% |
| `ioc` | 3 | 10.7% |
| `None` (blocked 前未填) | 1 | 3.6% |

### 2.3 按内部 `order_type` 列
| order_type | count |
|---|---|
| `market` | 28 (100%) |

**注**：`execution_orders.order_type` 列始终 `market`——说明所有 intent 在进入 submission 前都是 market 类型；当 `time_in_force=IOC` 时 adapter 在最终 `ordType` 上降级为 `ioc`。

### 2.4 按订单状态
| state | count |
|---|---|
| `FILLED` | 25 |
| `BLOCKED` | 3 (cancel_reason=`okx_close_only_without_reducible_position`) |

### 2.5 Fill-level fee_bps 分布
（推导：`abs(fee_amount) / (fill_qty × fill_price) × 10000`）

| 统计量 | 值 |
|---|---|
| count | 25 |
| min / p25 / p50 / mean / p75 / p95 / max | 全部 **5.000** |
| stdev | **0.000** |

**结论**：实际执行 fee 完美等于 OKX derivatives taker 默认 5.0 bps，零方差。

### 2.6 fills.liquidity_role 分布
| liquidity_role | count |
|---|---|
| `taker` | 25 (100%) |

### 2.7 交叉表：(execution_style, OKX ordType) → 指标
| style | ordType | n_ord | n_fill | notional (USDT) | actual_bps |
|---|---|---|---|---|---|
| None | ioc | 3 | 2 | 972.84 | 5.000 |
| None | market | 25 | 23 | 972.69 | 5.000 |
| **合计** | | **28** | **25** | **1945.53** | **5.000** |

一个 `ioc` 单被 BLOCKED（未 fill），两个 `market` 单被 BLOCKED。

---

## 3. Fee_resolver 预测 vs 实测

### 3.1 两版 fee_resolver 预测

对每个订单用两个版本的 fee_resolver 计算预测（不调用真实代码，而是在脚本里**重新实现**等价逻辑，derivative 档取 taker=5.0 / maker=1.0 bps）：

- **post-fix (H2)**: `bounded_limit_ioc ∈ taker 集合`（和 market、bounded_taker_cap、exchange 并列）
- **pre-fix**: `bounded_limit_ioc ∈ maker-blend 集合`，按 passive_bias 做加权（默认 passive_bias=0.7，对应 maker_weight=0.465）

### 3.2 结果对比表（按单元格平均）
| style | ordType | n | actual_bps | pred (post-fix) | pred (pre-fix) | diff vs post | diff vs pre |
|---|---|---|---|---|---|---|---|
| None | ioc | 3 | 5.000 | 5.000 | 5.000 | **+0.000** | **+0.000** |
| None | market | 25 | 5.000 | 5.000 | 5.000 | **+0.000** | **+0.000** |

**关键观察**：这批订单的 `execution_style` 字段本身是 None。fee_resolver 两个版本在"execution_style=None && ordType ∈ {market, ioc}"这条路径上**完全等价**：前缀版和后缀版都先命中 `ordType == "market"` / `ordType == "ioc"`（`ioc` 在 H2 前也被前缀版归到 `{bounded_limit_ioc, maker, passive}` 集合里触发 maker-blend——但只有 style 字符串是这些值时才会；`ordType` 字符串 `ioc` 不会触发这条支路）。

仔细核对 pre-fix 源码（`git show 7f55176^:aats/services/fee_resolver.py` 第 173 行）：
```python
if normalized_order_type == "market" or normalized_style in {"taker", "bounded_taker_cap", "exchange"}:
    return taker
if normalized_order_type == "limit" or normalized_style in {"bounded_limit_ioc", "maker", "passive"}:
    # maker-blend
```

对本批数据：
- 24 个 `ordType="market"` → 第一个 `if` 命中 → taker ✓
- 3 个 `ordType="ioc"` → 第一个 `if` 不匹配 `"market"`，也不匹配 `normalized_style in {...}`（因为 style 是 None / 空串）；第二个 `if` 不匹配 `"limit"`，也不匹配任何 style → 落到最后 `return taker` ✓
- 1 个 `ordType="None"`（blocked 前未填）→ 同上，落到最后 `return taker` ✓

所以两版本对本批 100% 输出 taker 5 bps。H2 修复在这批 live 数据里是真正的 no-op。

### 3.3 差异 > ±1 bps 标红条目
**零条**。全部 25 filled 订单的 actual vs predicted 差异恒为 0.000 bps。

---

## 4. OKX 账单对账

### 4.1 Observability 缺口
`execution_fills.raw_payload` 的结构是 `{"venue_fill_id": "...", "fill_event": FillEvent.model_dump()}`。`FillEvent` pydantic schema（`aats/schemas/execution.py:664`）只保留已经处理过的字段，**不保留 OKX websocket / REST 原始 payload**。
特别地，在 `okx_adapter._parse_fill_rows`（第 1965-1994 行）把 OKX 回执映射为 `ExchangeFill` 时**只取了 `fee` 和 `feeCcy`，丢掉了 `feeRate`、`execType`、`liquidity`、`rebate` 等原始 OKX 字段**。

这意味着"抽样对比 raw_payload fee vs fee_resolver 预测"**只能用 derived fee_bps**（= `abs(fee_amount)/notional×10000`），无法直接用 OKX 返回的 `feeRate` 字符串。对于 USDT 结算的 perpetual，数学上这两者等价（OKX 官方 `feeRate` 就是 `fee/(fillSz × fillPx)`），所以不影响结论。

### 4.2 抽样对比（全量 25 笔 filled）
| order_id (prefix) | symbol | side | ordType | fill_qty | fill_price | fee_amount (USDT) | derived_bps | OKX raw_feeRate |
|---|---|---|---|---|---|---|---|---|
| cla2aaf1dc | BTC-USDT-SWAP | buy | ioc | 0.07 | 75126.20 | 0.2629 | 5.000 | (not stored) |
| cl60f65c3b | BTC-USDT-SWAP | sell | market | 0.013 | 75104.30 | 0.0488 | 5.000 | (not stored) |
| cl0ca7d5a5 | BTC-USDT-SWAP | sell | market | 0.028 | 75140.10 | 0.1052 | 5.000 | (not stored) |
| cl2bba09d9 | BTC-USDT-SWAP | sell | market | 0.014 | 75127.10 | 0.0526 | 5.000 | (not stored) |
| cl4d90b159 | BTC-USDT-SWAP | sell | market | 0.007 | 75159.30 | 0.0263 | 5.000 | (not stored) |
| cl57b4e2e0 | BTC-USDT-SWAP | sell | market | 0.004 | 75179.00 | 0.0150 | 5.000 | (not stored) |
| cld123ddf7 | BTC-USDT-SWAP | sell | market | 0.001 | 75179.00 | 0.0038 | 5.000 | (not stored) |
| cl958670d1 | BTC-USDT-SWAP | sell | market | 0.001 | 75196.90 | 0.0038 | 5.000 | (not stored) |
| cl4e89835a | BTC-USDT-SWAP | sell | market | 0.001 | 75218.90 | 0.0038 | 5.000 | (not stored) |
| cl65f843d3 | BTC-USDT-SWAP | sell | market | 0.001 | 75187.60 | 0.0038 | 5.000 | (not stored) |
| cla366704f | BTC-USDT-SWAP | sell | ioc | 0.059 | 75755.40 | 0.2235 | 5.000 | (not stored) |
| cl500e87a7 | BTC-USDT-SWAP | buy | market | 0.01 | 75708.30 | 0.0379 | 5.000 | (not stored) |
| ... 其余 13 笔模式相同 ... | | | | | | | 5.000 | — |

**全部 25 笔 derived fee_bps 恒为 5.000**。这和 OKX 官方挂牌 derivatives 常规档 taker = 0.05% = 5 bps 完全一致，说明：
1. 账号**没有 VIP 等级折扣**（否则会低于 5 bps）
2. 当前**没有促销减免**
3. 没有返佣（`fee_amount` 始终为正表示付费）

### 4.3 mismatches
**无**。全量 25 笔完全匹配 fee_resolver 预测与 OKX 默认 taker 费率。

---

## 5. Slippage 与异常识别

### 5.1 Slippage 计算
`slippage_bps = (avg_fill_price - reference_price) / reference_price × 10000 × side_sign`
（buy: +1，sell: -1；正值表示对 trader 不利）

### 5.2 24 个可对比订单的 slippage 分布
| 统计量 | 值 (bps) |
|---|---|
| count | 24 |
| min | **-8.604** |
| p05 | -4.022 |
| p25 | -0.835 |
| p50 | +0.000 |
| **mean** | **-0.467** |
| p75 | +0.952 |
| p95 | +1.494 |
| max | **+2.271** |
| stdev | 2.246 |

**观察**：
- 中位数 0 — 大部分订单 ref_px ≡ avg_fill_px（特别是 lot 很小、7-15 USDT 的 "dust" reduce_only 单）。
- 最大有利 slippage -8.604 bps 来自 `clc39d89cfce4275102b41`（BTC buy market，ref=75777.0，实际 avg=75711.8 — 买价显著低于参考，意味着 ref 取样时点 market 已快速下行）。
- 最大不利 slippage +2.271 bps 来自 `cl8dbd1b02daab6c80a7fd`（BTC buy market，ref=75740.0，实际 avg=75757.2）。
- 全部 24 单都在 `maxSlippageToleranceBps=20` 的 pre-submit 保护内（pre-submit 是先看 ref 再发单的 sanity 检查；绝大部分 slippage 远小于 20）。

### 5.3 总入场成本（fee + slippage）
| 统计量 | 值 (bps) |
|---|---|
| mean | **4.533** |
| p50 | 5.000 |
| p95 | **6.494** |
| max | **7.271** |

即：即便是**最坏的情况 max 7.271 bps**，也仍在配置阈值 `strategy_hedge_independent_max_acceptable_cost_bps = 7.5` 的允许范围内，安全裕度仅 **0.23 bps** —— **紧贴上限**。

### 5.4 Drift 警告
- 因**没有 bounded_limit_ioc / bounded_taker_cap / passive_first 订单实际落地**，无法对这些 style 做 drift 扫描。fee 分布 std=0 对 {market, ioc} 两类是健康的（无 drift），但也说明样本无多样性。
- **passive_first 订单应该大部分是 post_only 或 ioc** — 本批无 post_only 订单（`ordType == "post_only"` 零条）。这和 `strategy_hedge_opportunistic_passive_first_enabled=true` 的预期不符，但 **opportunistic family 在这一时段也没下任何单**（本批 100% independent/primary）。不算 bug，是 "opportunistic 未触发" 的事实。
- **bounded_limit_ioc 的订单 ordType 应该 100% 是 ioc** — 本批 3 条 `ordType=ioc` 单的 `execution_style` 字段均为 None，无法确认内部 style 是 bounded_limit_ioc 还是其他（可能是 independent 家族内部路径用 `time_in_force=IOC` 但未在 raw_payload 顶层标记）。不算 bug 但是 observability 待补。

---

## 6. 成本现状总结与配置对照

### 6.1 真实 cost vs config 假设
| 维度 | 配置 | 实测 | 裕度 |
|---|---|---|---|
| `strategy_hedge_independent_max_acceptable_cost_bps` | 7.5 | mean 4.533, p95 6.494, max 7.271 | **p95 有 ~1.0 bps 裕度；max 仅 0.23 bps** |
| `strategy_hedge_opportunistic_max_acceptable_cost_bps` | 7.5 | — (此窗口无 opportunistic 订单) | n/a |
| OKX derivatives taker (`AATSSettings.trade_cost_derivatives_taker_fee_bps` 默认) | 5.0 | 5.000 实测 | **完美匹配** |
| `strategy_entry_min_signal_edge_bps` | 14.0 | — (无 decision 层数据包含在 execution_orders) | n/a |
| `signal_edge_scale_bps` | 20 | — | n/a |

### 6.2 建议是否调整参数
- **`max_acceptable_cost_bps = 7.5` 当前合理**，但裕度偏紧。若后续 slippage 尾部 p95 恶化（如波动率上升、BTC 突破期），可能会越线。建议监控。
- **`signal_edge_scale_bps = 20` 无法在本数据集里评估**（需要 decision 层 signal_edge 记录 + fill PnL 回溯，当前 execution_orders 不含 signal 侧信息，且订单量不足）。
- fee_resolver H2 修复后**taker 路径预测准确度 100%**，无需进一步 calibrate fee 侧；如要 recalibrate `signal_edge_scale_bps`，应该用 decision + outcome-level 数据而不是 execution-level。

---

## 7. 后续行动建议

### 7.1 是否需要修复 fee_resolver 其他分支
**不需要立即修复**。fee_resolver 在本数据集中表现完美（所有 taker 路径都正确返回 5.0 bps）。
**但还没被实盘验证的分支**：
- `execution_style == "passive_first"`（不在 fee_resolver 识别列表里，会落到最后 `return taker` —— 可能是正确的，因为 passive_first 是一个路径前缀概念而非下单时最终 style；需要核对 planner）
- `execution_style == "bounded_limit_ioc"` + `ordType != "ioc"`（H2 修复目标路径）—— 目前实盘无此组合发生过
- `execution_style == "maker"` / `"passive"` + `ordType == "limit"` 的 maker-blend —— 目前实盘无 limit 订单
- `bounded_taker_cap` / `aggressive_bounded_taker_cap` —— 目前实盘无此 style

建议：在 signal 放行、实际开始下 `bounded_limit_ioc` 单之后**重复本对账**（1-2 周窗口，至少 100+ 笔订单），再断言 fee_resolver 全分支正确。

### 7.2 Observability 建议（独立可做的工具改进，不在本任务范围内改动）
1. **`execution_orders.raw_payload` 顶层补 `execution_style`**：目前只有 `submission_payload.ordType`，丢失了决策时的 `execution_style` 语义。建议在 order_manager / adapter 提交点把 intent 的 `execution_style` 写入 raw_payload 顶层。
2. **`execution_fills.raw_payload.fill_event` 内嵌 OKX 原始 feeRate / execType**：当前 `FillEvent` schema 丢弃了 `feeRate` 等原始字段，对账只能反推。若在 `_parse_fill_rows` 保留一份 `raw_exchange_row: dict` 到 `ExchangeFill`，再透传到 `FillEvent.raw_exchange`，可以直接对比 OKX feeRate 字符串和 fee_resolver 预测。
3. **给 deploy pipeline 加 fee drift 监控告警**：可以写一个 rdp daemon，滚动 7 天扫 `execution_fills`，计算 `derived_fee_bps` 的 mean/p95，触发阈值告警（mean > 5.5 bps 或 stdev > 0.5 → warning）。

这些建议都单独提交 task（见本文件末尾 spawn 候选）。

### 7.3 是否需要重新 calibrate `signal_edge_scale_bps`
**不需要基于 cost 做 recalibrate**。H2 修复之前 cost 被低估 ~1.4 bps 对 `bounded_limit_ioc` 分支，但这批订单根本没走到那条分支（ordType 直接匹配 market/ioc）。signal_edge_scale_bps 的 calibration 应该基于 outcome-level 数据（decision → fill → close PnL），而不是本任务覆盖的 execution-level cost 对账。

### 7.4 是否需要执行层告警增强
**是**。观察到 3 条 BLOCKED 订单全部因为 `okx_close_only_without_reducible_position` —— 反映 close_only 判定和实时仓位读取之间有 race condition 或 stale cache。不在本任务范围，但**建议独立排查**。

---

## 8. 结论与签名

1. **H2 修复在本数据集内不改变任何订单的 fee 计算结果**（因为这批订单全走 ordType=market/ioc，两版 fee_resolver 都直接返回 taker）。H2 修复**没有任何回滚必要**，其预期收益（未来 `execution_style="bounded_limit_ioc"` + ordType 非 market/ioc 的路径不再低估 1.4 bps）尚无法用生产数据验证。
2. **OKX 账单 vs fee_resolver 预测**：**25 笔 filled，差异恒为 0.000 bps**。
3. **总入场成本** mean 4.533 bps / p95 6.494 bps / max 7.271 bps，**全部在 `max_acceptable_cost_bps = 7.5` 配置阈值内**，但 max 裕度仅 0.23 bps，偏紧。
4. **核心观察是数据量小** — 仅 28 单、1 个 symbol、1 个 strategy family、1 个 leg role、1 天，不足以对 fee_resolver 所有分支做全面 drift 扫描。**本报告完成对现有数据的 100% 对账；更广阔的 drift 验证需等订单量积累**。

### 后续跟踪清单
- [ ] `execution_orders.raw_payload.execution_style` 落库修复（observability）
- [ ] `execution_fills.raw_payload.fill_event` 嵌入 OKX 原始字段（observability）
- [ ] 订单量累积至 100+ 后重复对账（重点验证 `bounded_limit_ioc` / `bounded_taker_cap` / passive 分支）
- [ ] 3 笔 BLOCKED `okx_close_only_without_reducible_position` race 独立排查
- [ ] 可选：deploy pipeline 的 7 天 fee drift 告警 rdp daemon

---

## 9. 附录

### 9.1 脚本清单（本任务新建，在 `scripts/research/`）
- `cost_audit_01_probe_schema.py`   — 探测 DB 连接、table schema、时间窗口
- `cost_audit_02_full_analysis.py`  — 按 execution_style/ordType/family/state 枚举分布
- `cost_audit_03_raw_keys.py`       — 检查 raw_payload 顶层 keys 安全子集
- `cost_audit_04_fill_event.py`     — 检查 fill_event 结构与 OKX 原始字段缺口
- `cost_audit_05_reconciliation.py` — 实测 fee_bps vs fee_resolver 双版本预测
- `cost_audit_06_slippage.py`       — ref_px vs avg_fill_px 的 slippage 分布

所有脚本：纯 SELECT，无任何写操作，从 `.env.derivatives.live` 和 `.env.wsl2` 加载凭证（仅在 Python 进程内），不打印密码/API key。

### 9.2 参考 commit
- H2 修复: `7f55176` `fix(fee_resolver): bounded_limit_ioc 归 taker (P1-B step 2 cost 审计修复)`
- 源代码位置: `aats/services/fee_resolver.py:170-191` (post-fix), `git show 7f55176^:aats/services/fee_resolver.py` (pre-fix)

### 9.3 OKX 费率基准
生产账号当前对 USDT 结算 perpetual 采用 OKX 常规档（无 VIP 折扣、无促销）：
- Taker: 0.05% = **5.0 bps** ✓ 实测匹配
- Maker: 0.01% = **1.0 bps** — 本窗口无 maker 订单可验证
- `AATSSettings.trade_cost_derivatives_taker_fee_bps` 默认值与实测匹配。

---

## 10. 疑问（交给主任务决策者）

1. **修复后零数据**：本报告无法验证 H2 修复对 `bounded_limit_ioc` 分支的真实 fee 影响，因为修复后尚无新订单。是否需要安排一个"强制触发一单 bounded_limit_ioc"的验证流程（例如用最小 notional 的 reduce_only 单 + passive_first 路径）以尽快积累验证样本？还是等自然触发？
2. **`execution_style` 落库断链**：顶层 `raw_payload.execution_style` 为 None 是功能性 bug（决策→执行环节丢信息）还是设计默认（该字段本就是 planner 内部概念，不必持久化）？是否要独立修？
3. **裕度紧贴上限**：max total entry cost 7.271 vs 配置 7.5，只有 0.23 bps 裕度；如果 BTC 波动放大或流动性恶化，p95 有越线风险。是否需要在 `strategy_hedge_independent_max_acceptable_cost_bps` 上加 0.5~1.0 bps buffer，或者在 decision 层用 real-time cost 观测自动适配？
4. **BLOCKED 订单 observability**：3 笔 `okx_close_only_without_reducible_position` 是否已在 trackers / incidents 列表？是否需要提高优先级排查？

这四个问题需要产品 / 架构决策，调研任务范围之外。本报告到此为止。
