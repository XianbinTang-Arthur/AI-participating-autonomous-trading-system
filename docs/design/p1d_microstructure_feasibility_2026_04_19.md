# P1-D Microstructure 可行性调研报告 (2026-04-19)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

**状态**: 纯调研产出 / 决策文档
**Scope**: OKX BTC-USDT-SWAP 15m horizon 的 microstructure 预测特征
**调研边界**: 不动生产代码、不采集真实数据、不发 OKX API 请求；只读公开文档、项目源码、学术文献
**作者**: P1-D 立项调研 agent
**前置文档**:
- `docs/review/fast_impulse_candidate_selection_2026_04_19.md` — P1-A fast_impulse 5 候选 + ROC(5) baseline 全部 R² < 0，slope 全负
- `docs/review/h4_fix_validation_2026_04_19.md` — H4 方向门控修复验证（附录 §A: raw vs bar）
- `docs/research/fade_strategy_investigation_proposal_2026_04_19.md` — P1-C FADE CONDITIONAL-GO，raw-level R² = 0.00000
- `docs/design/archived/p1a_dual_channel_chase_failed_path_2026_04_19.md` — CHASE + FADE 双双证伪，OHLC 衍生特征路径关闭

---

## TL;DR — 决策建议

**立项 GO，但 Phase 1 走"最便宜的 3 个信号"试水，Phase 2 再决定是否上重型 orderbook。**

### 核心判断

1. **技术可行性：GO。** OKX WebSocket 公开频道足够覆盖所需 5 类特征的 4 类（orderbook、trades、OI、funding/basis），liquidation 已在采；缺的是 AATS 从未落盘 L2 books 和逐笔 trades。`books` 频道 400 levels 无 VIP 门槛，`trades-all` 频道零 VIP 零认证，**没有被 OKX API tier 锁死**。
2. **经济可行性：GO，但 L2 订阅 CPU/存储不可忽略。** 粗估 BTC-USDT-SWAP 单 symbol L2 books 60-200 MB/day bronze raw，经 15m aggregation 压到 silver 表后 <10 MB/day。Phase 1（bbo-tbt + trades + OI delta）可承受在现 `aats-market` 进程内跑；Phase 2（`books` 全档 + 多 symbol lead-lag）**需要独立 collector 进程**，不能挤占主 market。
3. **统计可行性：有限乐观。** 学术界在股票市场 OFI（Cont-Kukanov 2014）在 high-frequency 下 R² 可达 65%（同期），更保守的跨窗口外推 R² ~1%。Crypto 市场公开文献报告 taker buy/sell ratio、OI imbalance、orderbook imbalance 在 BTC 分钟级均有**非零但弱**的 predictive power（典型 R² 0.005-0.03 @ 5-15min horizon）。**跨 asset lead-lag** (ETH → BTC 短时) 证据较弱，高频下近似双向因果。**Liquidation cascade** 更偏 volatility nowcasting 而非 directional prediction。
4. **信号 >> 成本 的安全余量不乐观。** 当前衍生品 taker 5 bps + slip ~1 bps = 6 bps。学术文献报告的 microstructure edge 最高 R² @ 15min 约 0.02-0.03，对应 realized signed_return 的可解释部分期望 ~6-10 bps（gross）— 扣 6 bps cost 后 margin **只有 0-4 bps**。这与 P1-C FADE "gross 必须 >> 6 bps 才有 4 bps 安全 margin" 的结论一致。
5. **最大风险：和 P1-A/P1-C 一样，BTC 15m 本质是均值回归 + noise。** Microstructure 信号在 **30-120s horizon** 证据最强（Cont-Kukanov 的经典 setup 是 100ms - 60s），**15m 是被稀释的 edge**。如果 P1-D 直接瞄准 15m 决策粒度，可能重蹈 P1-A "选错时间尺度" 覆辙。建议 Phase 1 同步做 60s / 5m / 15m 多 horizon 回归，用最强 horizon 做 score，聚合到 15m 决策触发。

### 我的三个个人判断

> **A. 最有前景的 3 个特征 （按 ROI 排序）**
>
> 1. **OI delta + sign(ΔP) 联合（whale detection 代理）** — 最便宜，AATS 已在订 `open-interest`，只差一个 15m 聚合表和 regression。**预期 R² 0.01-0.02**，是 Phase 1 优先候选。
> 2. **Trade flow aggression（taker buy/sell ratio）** — 中等成本（`trades-all` 订阅 + buffer/flush），需要新 collector 进程。**预期 R² 0.01-0.025**，学术文献（Anastasopoulos-Gradojevic 2025）明确支持。
> 3. **Top-5 orderbook imbalance from `books5`** — 中成本，`books5` 100ms 推送，`bbo-tbt` 10ms 推送。**预期 R² 0.015-0.03**，但 15m 聚合会大幅稀释。
>
> **B. 最不值得做的两个**
>
> - **跨品种 lead-lag (ETH → BTC)** — 公开文献高频下双向因果，且 BTC 本身是 leader。要开 ETH-USDT-SWAP 的全套订阅 + 表 + 回归，边际 R² 可能 < 0.005。**放到 Phase 2 末尾或直接 drop**。
> - **Liquidation cascade 预测** — 已在采 raw 数据，但 cascade 事件稀疏（BTC 日均 5-20 次显著事件），15m 回归样本稀薄。价值主要在 volatility regime gate（不是 direction）。
>
> **C. 若 P1-D 失败，兜底 = γ 路径 (funding/basis 非定向)**
>
> Funding rate 和 basis (mark - index) 在 crypto 已有较厚证据做 carry trade。即使 15m directional edge 不存在，funding anomaly triggered carry 在日级有可靠 Sharpe。**NO-GO 不是死路**。

---

## § 1. 特征清单 & 文献证据

### 1.1 Feature 1 — Orderbook Imbalance (OBI)

#### 数学定义

**Top-K level imbalance**:

```
OBI_K(t) = (Σ_{i=1..K} bid_size_i(t) − Σ_{i=1..K} ask_size_i(t))
          / (Σ_{i=1..K} bid_size_i(t) + Σ_{i=1..K} ask_size_i(t))
```

常用 K ∈ {1 (best bid/ask 即 bbo), 5, 10, 20}。

**Weighted OBI** (考虑价格距离 mid):

```
OBI_w(t) = Σ_{i=1..K} [w_i · (bid_size_i − ask_size_i)]
        / Σ_{i=1..K} [w_i · (bid_size_i + ask_size_i)]
w_i = 1 / (1 + |px_i − mid| / tick)
```

**Cont-Kukanov OFI** (Order Flow Imbalance, 增量信号):

```
OFI(t) = Σ_k e_n
e_n = +q_n^{bid}  若 buy 侧新挂单
    = −q_n^{bid}  若 buy 侧撤单 / 价格下降
    = −q_n^{ask}  若 ask 侧新挂单
    = +q_n^{ask}  若 ask 侧撤单 / 价格上升
```

OFI 是 books-l2-tbt increment 的累加积分，需要 tick-by-tick。

#### 学术证据

| 论文 | 市场 | Horizon | 效应 | R² |
|---|---|---|---|---|
| Cont, Kukanov, Stoikov (2014) — *The Price Impact of Order Book Events* | NASDAQ equities | 1s - 1min | OFI 对 price change 线性解释 R² ≈ 0.65 (同期) | 0.65 |
| Dean Markwick blog (2022) — *Order Flow Imbalance* | CoinBase BTC-USD | 1s - 5min | OFI 在 100ms-30s 内 Pearson ~0.4-0.6 | — |
| Kolm, Turiel, Westray (2023) — *Deep Order Flow Imbalance* | 115 NASDAQ stocks, 10 日 | 1-10 min ahead | Multi-level OFI 5s horizon R² ≈ 1-3% | 0.01-0.03 |
| Explainable Patterns in Cryptocurrency Microstructure (arxiv 2602.00776) | Binance 多 symbol | 短时 return | Top-of-book + trade flow 特征 universal 预测力 | ~0.01-0.02 |
| Nowcasting Bitcoin's crash risk with order imbalance (PMC 10040314) | Binance BTC | 1 hour | Order flow imbalance 与 crash 相关性显著 | — |

#### BTC 特定场景适用性

**+** OBI 在 crypto market 被多项研究证实有 predictive power  
**+** OKX `books` 频道 400 levels 无 VIP 门槛，初始全量快照 + 100ms incremental 足以构建 top-K 或 weighted OBI  
**−** Top-K OBI 的 predictive power 在 horizon > 5 min 后急剧衰减；15m 聚合后的 mean imbalance 预计 R² 0.005-0.015  
**−** BTC-USDT-SWAP 在 OKX 订单深度较集中，bbo imbalance 震荡剧烈（10ms 级别跳变），需做 EMA 平滑

#### 可行性评分：★★★★☆

---

### 1.2 Feature 2 — Trade Flow Aggression (Taker buy/sell)

#### 数学定义

**Taker ratio**:

```
TR(t; Δ) = taker_buy_volume(t-Δ, t)
         / (taker_buy_volume(t-Δ, t) + taker_sell_volume(t-Δ, t))
```

Δ ∈ {1min, 5min, 15min}。TR > 0.5 表示 net buying aggression。

**Log-ratio (符号+幅度)**:

```
TFI(t; Δ) = log(taker_buy_volume / taker_sell_volume)
```

**Large-order detection (whale proxy)**:

```
whale_flag(trade) = 1  if trade.size > μ + 3σ (rolling 1h)
whale_direction(t; Δ) = sign(Σ whale_trades.signed_size in (t-Δ, t))
```

**VPIN (Volume-Synchronized Probability of Informed Trading)**:

Easley-Lopez de Prado-O'Hara 2012；复杂、需 bucket-equal-volume 重采样，暂缓入选。

#### 学术证据

| 论文 | 市场 | 效应 |
|---|---|---|
| Anastasopoulos, Gradojevic (2025) — *Order Flow and Cryptocurrency Returns* (EFMA) | BTC, ETH | Taker-side order flow 对 short-term return 有 monotonic 效应 |
| Bitcoin wild moves: Evidence from order flow toxicity and price jumps (ScienceDirect 2025) | BTC | VPIN 显著预测 future price jumps, 有 positive serial correlation |
| Dean Markwick (2022) | CoinBase BTC | 分析 trade sign imbalance 预测 mid-price change（同期 R² ~ 0.3-0.5，lag-1 衰减到 0.1） |
| Explainable Patterns in Cryptocurrency Microstructure (arxiv) | Binance 多 symbol | Trade flow + book features 具有跨 asset 一致性 |

#### BTC 特定场景适用性

**+** OKX `trades-all` 频道零 VIP、零认证，数据结构简洁（ts, px, sz, side）  
**+** Taker buy/sell 信号在 BTC 分钟级有公开证据；15m 聚合 R² 预期 0.01-0.025  
**+** Whale detection（size > threshold）是很便宜的加成信号  
**−** 需要构建 15m rolling buckets，大量 tick-by-tick 入库，吞吐量 BTC-USDT-SWAP 约 20-100 msg/s 不太大但要落 silver  
**−** Taker ratio 是 noisy 的，需要 EMA 平滑；vol 加权版本更稳定

#### 可行性评分：★★★★★

---

### 1.3 Feature 3 — OI Delta + Funding Anomaly

#### 数学定义

**OI delta (已在 AATS 内存 state)**:

```
OI_delta(t) = (OI(t) − OI_EMA(t; period=20))
            / OI_EMA(t; period=20)
```

**OI-price divergence signal**:

```
OI_direction = sign(OI_delta) · sign(ΔP_rolling_5min)
  = +1  : 价涨 OI 涨 (新多头入场)
  = −1  : 价涨 OI 跌 (空头平仓反弹)
  等等
```

**Funding z-score**:

```
funding_z(t) = (funding_rate(t) − μ_funding_rolling_7d)
             / σ_funding_rolling_7d
```

**Funding premium mean reversion**:

```
funding_deviation = |funding_rate(t)| − |median_funding_rolling_30d|
```

#### 学术证据

| 论文/来源 | 市场 | 效应 |
|---|---|---|
| BitMEX 2025 Q3 Derivatives Report | 全 crypto 衍生品 | Funding 有 "gravitational pull to 0.01%"，极端 funding 短命 |
| Ackerer, Hugonnier, Jermann (Wharton working paper) — *Perpetual Futures Pricing* | Theory + empirical | Perpetual 定价锚定 funding，套利链 enforced |
| Deribit Insights — *Perpetual Swap Funding* | 综述 | Funding 高 → crowded positioning → reversal 概率上升（方向不是 1-1） |
| CryptoQuant / Amberdata blog analyses | BTC perpetual | Extreme funding / OI spike 与 short-term volatility 正相关，direction 弱 |
| *Bitcoin wild moves: order flow toxicity and price jumps* | BTC | OI + order flow 联合预测 price jumps |

**BraveNewCoin 测试**：孤立 funding rate 作为 directional predictor **不 robust**；作为 volatility / volume regime filter 更可靠。

#### BTC 特定场景适用性

**+** AATS 已订 `funding-rate` 和 `open-interest` 频道，数据已入 MarketSnapshot，**0 新订阅成本**  
**+** OI delta state 已在 `oi_state.py` 实现，只缺 15m 聚合 + regression  
**+** Funding rate 每 1 分钟推送，8h 结算；结算前的 funding premium 偏离可作为 event-driven signal  
**−** Funding alone directional R² 历史经验普遍 < 0.005  
**−** OI delta 在 regime transition（趋势入场 vs 平仓出场）语义不同，需要和 price direction 联合才能 disambiguate  
**+** 这个特征的 marginal cost 几乎为 0，即使 R² 小也值得做

#### 可行性评分：★★★★★ （因为已免费获得）

---

### 1.4 Feature 4 — Volume Profile z-score（相对历史同时段）

#### 数学定义

**Seasonal volume z-score**:

```
expected_volume(weekday, hour_of_day) =
    rolling_mean(volume | (dow, hod), 4 weeks)

volume_z(t) = (volume_15m(t) − expected_volume(dow(t), hod(t)))
            / rolling_std(volume | (dow, hod), 4 weeks)
```

**Unusual volume spike**:

```
spike_flag = 1  if volume_z > 2.0
```

**Vol-weighted TFI (交互特征)**:

```
vol_weighted_TFI = TFI(t; 15min) · volume_z(t)
```

#### 学术证据

| 论文 | 效应 |
|---|---|
| Deep Learning for VWAP Execution in Crypto Markets (arxiv 2502.13722) | BTC, ETH, ADA 有稳定 intraday + intraweek volume seasonality |
| Bayesian Analysis of Bitcoin Volatility (MDPI 2025) | Minute-level BTC/USDT 显示清晰 daily + weekly seasonal patterns |
| *The Rhythm of Liquidity: Temporal Patterns in Market Depth* (amberdata blog) | Order book 深度 + volume 有 predictable seasonality |
| Forecasting Intraday Volume (arxiv 2505.08180) | 2025 年 equity 市场论文，ML 模型显著优于 historical-average baseline |

#### BTC 特定场景适用性

**+** 不需要新订阅，直接用 `silver.market_swap_candles_15m.vol` 做历史窗口聚合  
**+** 跨赛季 (dow × hod) z-score 捕捉 "非常规成交量" 信号，与其它特征交互有意义  
**−** 仅 volume alone 的 directional R² 极低（volume 是 volatility proxy, 不是 direction）  
**+** 但作为 **interaction feature**（和 TFI / OBI 乘积）可能放大 signal-to-noise  
**−** 需要至少 4 周历史才能估季节均值，冷启动慢

#### 可行性评分：★★★☆☆ （作为辅助交互特征）

---

### 1.5 Feature 5 — 跨品种 Lead-lag (ETH/SOL → BTC)

#### 数学定义

**Lead-lag correlation**:

```
ρ(BTC_t, ETH_{t−lag}) for lag ∈ {−60s, −30s, −15s, 0, +15s, ...}
```

**Shocks spillover**:

```
shock_BTC(t) = Σ_{i ∈ {ETH, SOL, altcoin_basket}}
              β_i · return_i(t-Δ) · idx_vol_i(t-Δ)
```

**Granger causality F-stat** (formal test)

#### 学术证据

| 论文 | 市场 | 结果 |
|---|---|---|
| Sifat, Mohamad (2019) — *Lead-Lag relationship between Bitcoin and Ethereum* | BTC, ETH hourly + daily | 2017-2018 largely bi-directional causality |
| *A high-frequency GMM-VAR approach* (DSFE 2025) | Multi-crypto | **BTC 是主 shock transmitter**; ETH 为次级 |
| Cross-cryptocurrency return predictability (JEDC 2024) | Minute-level多 coin | BTC 预测其他 coin，**反向 weaker** |
| Applying time delay convergent cross mapping (ScienceDirect 2025) | BTC series | Regime-dependent lead-lag |

**关键反向发现**：多项 2024+ 论文指出 **BTC 是主 driver**，ETH → BTC 的 predictive power 在分钟级**较弱**且 regime-dependent。

#### BTC 特定场景适用性

**−** 最弱的候选；BTC 本身是 leader  
**−** 需要开 ETH-USDT-SWAP 全套订阅（ticker, books, trades, OI, funding）→ 订阅数 ×2  
**−** Bronze/Silver 表都需要为 ETH 复制一份 schema  
**−** Rate limit 压力：每 symbol 约 5 条订阅，2 symbol 共 10 条，仍远在 480/h 之下，但 data throughput 翻倍  
**+** 若真的存在有限 edge（如 ETH 在某些 news 事件后先动），regime-conditional 使用有价值  
**+** 相比放弃，ETH 的数据摄取在 Phase 2 后期可作 stretch goal；**不作为 Phase 1 必需**

#### 可行性评分：★★☆☆☆ （推迟到 Phase 2 末尾或 drop）

---

### 1.6 Feature 6 (可选) — Liquidation Cascade

#### 数学定义

**Liquidation intensity**:

```
liq_intensity(t; Δ) = Σ |liq.bk_loss| for liq.ts ∈ (t-Δ, t)
```

**Directional liq imbalance**:

```
liq_imbalance(t; Δ) = Σ_long_liq − Σ_short_liq
                    / Σ_long_liq + Σ_short_liq + ε
```

#### 学术证据

| 来源 | 结果 |
|---|---|
| Glassnode — *Liquidation Heatmaps* | Liquidation clusters ID 高波动 zone, **不预测方向** |
| CoinGlass 实战分析 | Cascade 后 short-term 反转概率高（类 FADE 信号），但事件稀疏 |
| P1-C 报告 §7 regime slice | 本 AATS 数据显示 breakout regime 下 n=10 不足以拟合 |

#### BTC 特定场景适用性

**+** AATS 已在采 `staging.raw_liquidations`（`OKXLiquidationsWSClient`），免费得到  
**+** 可做 volatility regime gate，配合其他特征使用  
**−** 事件稀疏：BTC 日均 5-20 次显著 cascade，15m bar 级 n 样本不足  
**−** Directional predictor 文献几乎全负面；主要是 volatility nowcasting

#### 可行性评分：★★★☆☆ (作为 regime gate / interaction 特征，不作 stand-alone predictor)

---

## § 2. 数据源调研

### 2.1 OKX WebSocket 公开频道清单

| 频道 | 层级/粒度 | Push 频率 | VIP 要求 | 认证 | AATS 已订? | Phase 1 目标 |
|---|---|---|---|---|---|---|
| `tickers` | best bid/ask/last | 每次变动 | 无 | 否 | **是** | reuse |
| `bbo-tbt` | best bid/ask tbt | 10ms | 无 | 否 | 否 | Phase 1 加订 |
| `books5` | top 5 levels snapshot | 100ms | 无 | 否 | 否 | Phase 1 加订 |
| `books` | 400 levels incremental | 100ms | **无** | **否** | 否 | Phase 2 evaluate |
| `books-l2-tbt` | 400 levels tbt | 10ms | **VIP 5** | 是 | 否 | 不可用（VIP0） |
| `books50-l2-tbt` | 50 levels tbt | 10ms | **VIP 4** | 是 | 否 | 不可用（VIP0） |
| `trades-all` | 逐笔成交 | 每笔 | 无 | 否 | 否 | Phase 1 加订 |
| `funding-rate` | 每分钟 | 变动时 | 无 | 否 | **是** | reuse |
| `open-interest` | 3s | 3s | 无 | 否 | **是** | reuse |
| `mark-price` | 约 100ms | 变动时 | 无 | 否 | **是** | reuse |
| `liquidation-orders` | 事件 | 实时 | 无 | 否 | **是** (独立 collector) | reuse |

### 2.2 REST 辅助端点

| Endpoint | 用途 | Rate limit |
|---|---|---|
| `/api/v5/market/trades` | 历史近 N 笔 | 20 req / 2s |
| `/api/v5/public/liquidation-orders` | 7-day history | 40 req / 2s |
| `/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader` | 大户多空比 5m/1h | AATS 已用 `LongShortRatioPoller` 5min 轮询 |

### 2.3 Rate limit 统计

OKX v5 公开 WebSocket：
- 每 connection 480 subscribe+unsubscribe+login / hour (足够)
- 3 requests/s per IP 建立 ws 连接
- "连接 30s 无推送自动断开"

**容量判断**：Phase 1 新增 3 个订阅（bbo-tbt, books5, trades-all），每 symbol 5 订阅，远在 limits 之下；单 IP 可持 3 个 public connections，current `market_gateway` 用 2 个（public + business），Phase 1 可共用或加第 3 个 dedicated。

### 2.4 历史数据 retention

- `/api/v5/market/trades` 只返回最近 N 笔（没有时间范围 query）→ **历史回填不可行，只能 WS 实时落库**
- `/api/v5/public/liquidation-orders` 7 天 → AATS 已走 WS 实时落 staging
- `/api/v5/market/books-full` / depth：没有历史版本
- Candles/funding：OKX 提供 `/api/v5/market/history-candles` 可回填，AATS 已在用

**结论**：对 trades、orderbook、OI 这三类 tick-level 特征，**历史数据无法回填**，只能从 P1-D Phase 1 启动那一刻开始累积。
这决定了 Phase 1 时间表（见 §6）必须给**数据累积 3-4 周**让首轮回归有 ≥ 8000 raw sample。

### 2.5 AATS 当前采集 gap

| 数据 | 需要 | 现状 | Gap |
|---|---|---|---|
| Top-of-book imbalance | 历史序列 | 内存 ticker best bid/ask，**未落 bronze** | 需要新 bronze 表 |
| Top-5 orderbook | `books5` 订阅 + 落盘 | 未订阅 | 新订阅 + bronze 表 |
| Trade tape | `trades-all` 订阅 + 落盘 | 未订阅（`recent_trades=[]` 硬编码） | 新订阅 + staging + bronze 表 |
| OI delta | 15m 聚合表 | in-memory state，**未落库** | 新 silver 表 |
| Funding anomaly | 15m 聚合 z-score | 已落 bronze/silver | 复用，只加 rolling z-score silver view |
| Liquidation cascade | 15m 聚合 | staging.raw_liquidations 已采 | 新 silver 表 |
| Cross-asset ETH | 全套 | 未订阅 ETH | Phase 2 再决定 |

---

## § 3. RDP Silver 表设计草案

所有表前缀 `silver.market_microstructure_*`；主键 `(symbol, ts)` where `ts` 是 15m bar 起点。
后续 Gold 层 as-of join 到 `market_swap_replay_bars_15m` 时做对齐。

### 3.1 `silver.market_orderbook_metrics_15m`

```sql
CREATE TABLE IF NOT EXISTS silver.market_orderbook_metrics_15m (
    symbol                  TEXT NOT NULL,
    ts                      TIMESTAMPTZ NOT NULL,

    -- BBO level (from bbo-tbt)
    bbo_imbalance_mean      NUMERIC(12, 8),        -- avg of (bs-as)/(bs+as)
    bbo_imbalance_std       NUMERIC(12, 8),
    bbo_imbalance_last      NUMERIC(12, 8),        -- value at bar close

    -- Top-5 level (from books5)
    top5_bid_depth_usd      NUMERIC(24, 8),
    top5_ask_depth_usd      NUMERIC(24, 8),
    top5_imbalance_mean     NUMERIC(12, 8),
    top5_imbalance_ema      NUMERIC(12, 8),
    top5_weighted_imbalance NUMERIC(12, 8),

    -- Spread metrics
    spread_bps_mean         NUMERIC(12, 4),
    spread_bps_max          NUMERIC(12, 4),

    -- Sample counts (for QA)
    bbo_samples_n           INTEGER,
    books5_samples_n        INTEGER,

    ingest_run_id           UUID NOT NULL,
    dataset_version         TEXT NOT NULL,
    quality_flags           TEXT[] NOT NULL DEFAULT '{}',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
```

**Row size 估算**：~140 bytes/row × 96 rows/day/symbol ≈ 13.5 KB/day/symbol → 30 天 ~400 KB, 365 天 ~5 MB。

**聚合公式（Bronze → Silver 15m ETL）**：

```python
for bar_start in (every 15m):
    bar_end = bar_start + 15min
    bbo_samples = SELECT * FROM bronze.market_orderbook_bbo
                  WHERE symbol=X AND ts >= bar_start AND ts < bar_end
    bbo_imbalance_mean = mean( (bs - as) / (bs + as) for each bbo_sample )
    bbo_imbalance_std  = stddev( ... )
    bbo_imbalance_last = bbo_samples.tail(1).imbalance

    books5_samples = SELECT * FROM bronze.market_orderbook_books5
                     WHERE symbol=X AND ts >= bar_start AND ts < bar_end
    top5_imbalance_mean = mean( sum_bid - sum_ask / sum_total )
    top5_weighted_imbalance = weighted by 1/(|px - mid| / tick)

    INSERT ... ON CONFLICT (symbol, ts) DO UPDATE ...
```

### 3.2 `silver.market_trade_flow_15m`

```sql
CREATE TABLE IF NOT EXISTS silver.market_trade_flow_15m (
    symbol                  TEXT NOT NULL,
    ts                      TIMESTAMPTZ NOT NULL,

    -- Volume
    total_volume_ccy        NUMERIC(28, 10),
    buy_volume_ccy          NUMERIC(28, 10),
    sell_volume_ccy         NUMERIC(28, 10),
    trade_count             INTEGER,

    -- Taker ratio
    taker_buy_ratio         NUMERIC(12, 8),       -- buy_vol / (buy+sell)
    trade_flow_imbalance    NUMERIC(12, 8),       -- (buy - sell) / (buy + sell)
    log_tfi                 NUMERIC(12, 8),       -- log(buy/sell)

    -- Size distribution
    mean_trade_size         NUMERIC(18, 8),
    p95_trade_size          NUMERIC(18, 8),
    max_trade_size          NUMERIC(18, 8),

    -- Whale detection
    whale_buy_volume_ccy    NUMERIC(28, 10),      -- trades > size_threshold (rolling p99)
    whale_sell_volume_ccy   NUMERIC(28, 10),
    whale_direction         NUMERIC(12, 8),       -- (whale_buy - whale_sell) / total

    -- Aggressiveness
    vwap                    NUMERIC(20, 10),
    vwap_minus_mid_bps      NUMERIC(12, 4),       -- 正 = taker buy 主导

    ingest_run_id           UUID NOT NULL,
    dataset_version         TEXT NOT NULL,
    quality_flags           TEXT[] NOT NULL DEFAULT '{}',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
```

**Row size**：~160 bytes/row × 96/day ≈ 15 KB/day/symbol → 30d ~450 KB。

Bronze side `bronze.market_trades` 建议结构：

```sql
CREATE TABLE IF NOT EXISTS bronze.market_trades (
    symbol    TEXT NOT NULL,
    ts        TIMESTAMPTZ NOT NULL,
    trade_id  TEXT NOT NULL,
    px        NUMERIC(20, 10) NOT NULL,
    sz        NUMERIC(28, 10) NOT NULL,
    side      TEXT NOT NULL,  -- 'buy' or 'sell' (taker side per OKX)
    raw_payload JSONB,
    ingest_run_id UUID NOT NULL,
    PRIMARY KEY (symbol, ts, trade_id)
);
```

BTC-USDT-SWAP 平均 ~30 trades/s @ normal regime → 2.6M trades/day。Bronze row ~80 bytes → **~200 MB/day/symbol raw**。15m aggregation 后 silver 只有 15KB/day。

### 3.3 `silver.market_oi_funding_metrics_15m`

```sql
CREATE TABLE IF NOT EXISTS silver.market_oi_funding_metrics_15m (
    symbol                  TEXT NOT NULL,
    ts                      TIMESTAMPTZ NOT NULL,

    -- OI
    oi_open                 NUMERIC(28, 10),
    oi_close                NUMERIC(28, 10),
    oi_delta                NUMERIC(18, 10),      -- (close - open) / open
    oi_ema                  NUMERIC(28, 10),      -- EMA(20) closing
    oi_delta_vs_ema         NUMERIC(18, 10),

    -- Price-OI divergence
    price_change_bps        NUMERIC(12, 4),
    oi_price_regime         TEXT,                 -- 'trend_long', 'trend_short', 'short_cover', 'long_cover', 'mixed'

    -- Funding
    funding_rate_current    NUMERIC(18, 12),
    funding_rate_next_est   NUMERIC(18, 12),
    funding_z_score_7d      NUMERIC(12, 6),
    funding_deviation_30d   NUMERIC(18, 12),      -- |current| - |rolling median|
    minutes_to_next_funding INTEGER,

    -- Mark / basis
    mark_price              NUMERIC(20, 10),
    basis_bps               NUMERIC(12, 4),       -- (mark - mid) / mid * 10000

    ingest_run_id           UUID NOT NULL,
    dataset_version         TEXT NOT NULL,
    quality_flags           TEXT[] NOT NULL DEFAULT '{}',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
```

**Row size**：~180 bytes × 96/day ≈ 17 KB/day → 30d ~500 KB。

### 3.4 `silver.market_volume_profile_15m`

```sql
CREATE TABLE IF NOT EXISTS silver.market_volume_profile_15m (
    symbol                  TEXT NOT NULL,
    ts                      TIMESTAMPTZ NOT NULL,

    -- 本 bar 的 volume
    volume_ccy              NUMERIC(28, 10),
    trade_count             INTEGER,

    -- Seasonal baseline (per dow × hod)
    expected_volume_ccy     NUMERIC(28, 10),
    volume_z_score          NUMERIC(12, 6),
    volume_spike_flag       BOOLEAN,

    -- Interaction with tfi (for regression convenience)
    vol_weighted_tfi        NUMERIC(14, 8),

    ingest_run_id           UUID NOT NULL,
    dataset_version         TEXT NOT NULL,
    quality_flags           TEXT[] NOT NULL DEFAULT '{}',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
```

**Row size**: ~120 bytes × 96/day ≈ 11 KB/day → 400 KB/30d。

### 3.5 `silver.market_liquidation_metrics_15m`

```sql
CREATE TABLE IF NOT EXISTS silver.market_liquidation_metrics_15m (
    symbol                  TEXT NOT NULL,
    ts                      TIMESTAMPTZ NOT NULL,

    long_liq_count          INTEGER,
    short_liq_count         INTEGER,
    long_liq_notional_usd   NUMERIC(28, 10),
    short_liq_notional_usd  NUMERIC(28, 10),
    liq_imbalance           NUMERIC(12, 8),       -- (long - short) / (long + short)
    max_single_liq_usd      NUMERIC(28, 10),

    cascade_flag            BOOLEAN,              -- N > threshold
    intensity_7d_z          NUMERIC(12, 6),

    ingest_run_id           UUID NOT NULL,
    dataset_version         TEXT NOT NULL,
    quality_flags           TEXT[] NOT NULL DEFAULT '{}',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (symbol, ts)
);
```

**Row size**：~150 bytes × 96/day ≈ 14 KB/day → 400 KB/30d。

### 3.6 存储总量 (30 天, 单 symbol BTC-USDT-SWAP)

| 层 | 每天 | 每 30 天 | 每 365 天 |
|---|---|---|---|
| `bronze.market_trades` | **~200 MB** | ~6 GB | ~70 GB |
| `bronze.market_orderbook_bbo` | ~30 MB | ~900 MB | ~10 GB |
| `bronze.market_orderbook_books5` | ~40 MB | ~1.2 GB | ~14 GB |
| `silver.market_*` (5 张表合计) | ~70 KB | ~2 MB | ~25 MB |
| `gold` extensions | ~20 KB | ~600 KB | ~7 MB |

**总结**：Silver/Gold 完全可忽略；**主要存储开销在 Bronze 层 trades + L2**。30 天 ~8 GB, 365 天 ~90 GB 单 symbol。

**优化策略**：
- Bronze trades 滚动 30-60 天 retention（留够 30d 回归 + 15d 缓冲）
- Bronze L2 滚动 14 天（Silver 已有聚合无需长期 raw 保留）
- 若开 ETH，再加一倍

---

## § 4. 摄取管道设计

### 4.1 新 Collector: `MicrostructureWSClient`

参照 `OKXLiquidationsWSClient` 模板，独立于 `aats/data_platform/collectors/microstructure_ws_collector.py`（或拆三个 collector）：
- 专职订阅 `trades-all`, `books5`, `bbo-tbt`
- 不共用 `market_gateway` 的主 ws 连接（避免挤占主交易 snapshot 带宽）
- 独立 NATS 话题 (可选) 或直接写 DB

**写 Bronze 策略**：
- `trades-all`: buffer (500 rows, 5s 任一达到 flush) → `bronze.market_trades`
- `bbo-tbt`: EMA 平滑后 sampled 每 1s 落一行（10ms 粒度 DB 抗不住）→ `bronze.market_orderbook_bbo`
- `books5`: snapshot 每次变动入库，但限流 500ms sampling → `bronze.market_orderbook_books5`

### 4.2 Silver ETL

- 15m boundary 触发器：cron `*/15` 或 AATS 已有的 RDP daily_ingest 同步
- ETL 函数 `build_silver_microstructure_15m(symbol, bar_start, bar_end)`
  - 读 bronze 3 张表
  - 计算所有聚合
  - UPSERT silver
- 幂等：同 (symbol, ts) 重跑结果一致（windowing pure function）

### 4.3 容错设计

| 故障 | 检测 | 响应 |
|---|---|---|
| OKX WS 断线 | heartbeat timeout | auto reconnect，backoff 1s → 30s cap |
| OKX 推送 stale (> 30s 无数据) | keepalive poll | force reconnect |
| DB 写失败 | batch rollback | buffer 保留 60s 重试，失败后弃 + 记 audit |
| Silver ETL 失败 | run_registry | quality_flags 打 `etl_failed`，不阻塞下游 |
| Bar 缺数据 (WS 宕 10min) | gap detector | silver row.quality_flags=['gap_filled_with_nulls']，供 replay 跳过 |

### 4.4 资源估算

| 资源 | 峰值 | 平均 |
|---|---|---|
| BTC trades 吞吐 | 200 msg/s | 30 msg/s |
| BTC books5 吞吐 | 10 msg/s (限流后) | 5 msg/s |
| BTC bbo-tbt 吞吐 | 100 msg/s (限流后) | 20 msg/s |
| CPU (collector 进程) | ~15% 1 core (normal regime) | ~5% |
| CPU (silver ETL 每 15min) | ~10% 1 core × 10s | idle |
| 网络 (WS inbound) | ~200 KB/s | ~50 KB/s |
| DB write IOPS | ~100/s | ~20/s |

**判断**：Phase 1 可以在现有 aats-market 容器内跑（资源充足）；
Phase 2 若加 ETH + books full 400 levels，需要独立 `aats-microstructure-collector` 容器。

---

## § 5. 统计可行性先验

**不跑真实回归**，基于文献和推理给出期望值。

### 5.1 Expected R² by feature × horizon

| Feature | 60s | 5min | 15min | 1h |
|---|---|---|---|---|
| OFI / OBI (weighted top-5) | **0.05-0.15** | 0.02-0.05 | **0.008-0.02** | <0.005 |
| Taker buy/sell ratio | 0.03-0.08 | 0.015-0.04 | **0.008-0.02** | <0.005 |
| OI delta × price sign | 0.01-0.03 | **0.015-0.04** | **0.01-0.02** | <0.005 |
| Funding anomaly | <0.005 | <0.005 | <0.005 | 0.005-0.01 |
| Volume profile z alone | <0.003 | <0.003 | <0.003 | <0.003 |
| Liquidation intensity | 0.01-0.03 (事件稀疏) | 0.02-0.05 | 0.01-0.02 | <0.005 |
| Cross-asset lead-lag | 0.005-0.01 | <0.005 | <0.005 | <0.005 |
| **Combined model (4-5 features)** | **0.1-0.25** | **0.05-0.1** | **0.025-0.06** | <0.01 |

### 5.2 在 OHLC alpha=0 市场下仍可能 work 的机制

1. **Microstructure noise ≠ OHLC noise**：15m OHLC 的 close-to-close return 把 bar 内所有信号平均掉；但 bar 内的 orderbook pressure → aggressor flow → 下一 bar 价格的传导链条**仅部分**反映在 close 价。
2. **大单冲击后的恢复速度**：OBI 在 trade 后几秒恢复 mid 的快慢，直接预测 next-bar 方向（Cont-Kukanov 的核心机制）。这在 OHLC 上看不到。
3. **OI + price 联合信号**：OHLC 只看价格不看持仓；OI 告诉你"谁在入场"。
4. **事件 +/- 窗口**：Funding 结算 ±10min、大 liquidation ±5min 是**非持续信号**，不在持续 OHLC 特征中。

### 5.3 "新闻 +/- 60min 窗口" 特化的特征

这些特征在 **event window** 内预测力远高，非 event 时接近 0：
- Liquidation cascade
- Funding settlement moment
- Large whale trade impact decay

应作为 **event-triggered sub-strategy**，而非 persistent signal。

### 5.4 期望 PnL margin 量级

假设 combined 15m R² = 0.03 → gross signed_return 期望 |y| ≈ 10-15 bps。
扣 6 bps cost → net 4-9 bps。
Win rate 预期 52-58%。
Sharpe @ daily: 1.0-2.0 range（若 R² 稳定）。

**这是乐观估计**。P1-C FADE 的教训告诉我们：raw-level R² 比 bar-level 可能稀释 5-10×。若 Phase 1 真 R² 只有 0.005，net margin 会 ≤ 0。

---

## § 6. 实施路线图

### 6.1 总体时间表 (6-8 周)

```
W0  [已 done]    P1-A CHASE 归档、P1-C FADE CONDITIONAL-GO、P1-D 立项批准
W1-W2            Phase 1 数据管道: collector + bronze/silver/gold (5 张表)
W3               Phase 1 数据累积监控 (需要至少 3 周数据 → 跨 W3-W5)
W3-W4            Phase 2 特征计算器 + 单元测试 + 首轮 R² 回归 (3 核心特征)
W5-W6            Phase 2 剩余 2 特征 + regime 切片 + cross-window 鲁棒性
W7               Phase 3 综合回归 + cost-adjusted PnL 模拟
W8               Phase 3 GO / CONDITIONAL-GO / NO-GO 决策报告
```

### 6.2 Phase 分解

#### **Phase 1A — 数据管道立基 (W1-W2, 2 weeks)**

**目标**：采集 3 个新频道 + 落 bronze 表；silver ETL 跑通。

**交付物**:
1. `aats/data_platform/collectors/microstructure_ws_collector.py` — 新 WS collector
2. `bronze.market_trades`, `bronze.market_orderbook_bbo`, `bronze.market_orderbook_books5` 表
3. `silver.market_orderbook_metrics_15m`, `silver.market_trade_flow_15m`, `silver.market_oi_funding_metrics_15m`, `silver.market_volume_profile_15m`, `silver.market_liquidation_metrics_15m`
4. `scripts/rdp_build_microstructure_silver.py` — 从 bronze 建 silver 15m 聚合
5. 新 migration `aats/data_platform/migrations/batch_b_04_microstructure.sql`
6. 单元测试覆盖率 ≥ 80% (collector, aggregation, schema)

**工期**：2 人周。
**里程碑验收**：
- [ ] 连续 48h 无间断采集 BTC-USDT-SWAP 3 个频道
- [ ] Silver 表每 15min 有新 row，quality_flags 无 'etl_failed'
- [ ] Bronze `market_trades` row count ≈ 预期（OKX ~2M/day）
- [ ] 所有 unit tests 通过

**退出条件**：连续 7 天数据完整率 ≥ 99%。

---

#### **Phase 1B — 数据累积等待 + 冷启动检查 (W3, 1 week)**

**目标**：等数据够长做 regression；并行启动 Phase 2A 前置工作。

**里程碑**：到 W3 end 累积 3 周 BTC-USDT-SWAP 15m silver rows (~2000 rows) 的 50%（即 1 周）。

---

#### **Phase 2A — 特征计算 + 3 核心特征首轮回归 (W3-W4, 2 weeks)**

**目标**：在现有 AATS research DB 上跑 OI delta、TFI、OBI 三个特征的 15m horizon 回归。

**交付物**:
1. `scripts/research/microstructure_regression_phase1.py` — OLS + robustness + regime 切片
2. `docs/review/microstructure_features_phase1_regression_2026_MM_DD.md` — 3 特征结果报告
3. 多 horizon 扫描 (60s / 5m / 15m / 1h)

**工期**：1.5 人周。

**里程碑验收**：
- [ ] 3 个特征各自 raw-level R² 报告出来
- [ ] 至少 1 个特征 raw-level 15m R² ≥ 0.01 **且** sign 稳定 (first vs second half 一致)
- [ ] OHLC baseline 对照组跑一遍 (确认 R² = 0)

**退出条件**：
- 3 特征全部 raw-level R² < 0.005 → **提前 NO-GO**，转 Phase 3 γ 方向
- 至少 1 个特征 R² ≥ 0.01 → 继续 Phase 2B

---

#### **Phase 2B — 剩余特征 + 交互 + 鲁棒性 (W5-W6, 2 weeks)**

**目标**：加入 volume profile、liquidation、funding anomaly；研究特征交互；多窗口稳健性。

**交付物**:
1. 剩余 2-3 特征的独立回归
2. 交互特征（vol-weighted TFI, OI×OBI, etc.）
3. Cross-window 稳健性矩阵（至少 2 个不重叠 30d 窗口或 30d + 60d）
4. Regime slice（range/trend/uncertain/breakout/high-vol/low-vol）
5. `docs/review/microstructure_features_phase2_2026_MM_DD.md`

**工期**：2 人周。

**里程碑验收**：
- [ ] 多 feature 联合 R² ≥ 0.02 raw-level
- [ ] 至少一个 regime slice 下 cost-adjusted net > 2 bps @ n ≥ 100
- [ ] Bonferroni-corrected significance 通过（考虑 6 特征 × 4 horizon = 24 tests，需要 p < 0.002 单个 test）

---

#### **Phase 3 — 综合决策 (W7-W8, 2 weeks)**

**目标**：建模 full portfolio view，与 independent family 集成路径设计，最终 GO/NO-GO。

**交付物**:
1. 综合回归 (所有特征 + interactions + regime gates) 报告
2. PnL 模拟（多 threshold + cost-adjusted）
3. P1-D 落地实施方案：路径 A（新 microstructure sleeve）vs 路径 B（嵌入 independent 的新 features）
4. 最终决策报告 `docs/design/p1d_go_nogo_decision_2026_MM_DD.md`

**工期**：2 人周。

**里程碑验收**：见 §8。

### 6.3 工期总结

| Phase | 人周 | 关键路径 |
|---|---|---|
| 1A | 2 | 新 collector + 5 silver tables |
| 1B | 1 | 等数据（闲时做 2A 前置） |
| 2A | 1.5 | 3 核心特征首轮 R² |
| 2B | 2 | 剩余特征 + 鲁棒性 |
| 3 | 2 | 综合决策 |
| **合计** | **8.5** | 跨 6-8 周日历 |

---

## § 7. 风险清单

### 7.1 OKX WebSocket 稳定性

- OKX 历史上有零星 outage（2022-2023 有多次 ws cluster 故障）
- AATS 现有 `OKXWebSocketConsumerBase` 有 reconnect/ack timeout/market_data stale detect → **可复用**
- Phase 1 collector 如独立部署，要监控独立的 connected metric 和 last_message_ts

**缓解**：复用现有 keepalive 框架；接 Prometheus metric `microstructure_ws_connected`、`microstructure_ws_stale_seconds`、`bronze_row_count_last_15min`。

### 7.2 数据采集成本 vs 潜在 edge

Phase 1 成本粗估（每月）：
- 独立 collector 容器 CPU: ~0.2 core × 730h = $5-10 (本地 WSL2 无成本)
- DB 存储增量: 8 GB/month (Bronze) × 6 个月 = 48 GB → $1-3 EBS (本地无成本)
- 开发人力: 已在 W1-W8 计划内

**判断**：本地 WSL2 部署下直接成本几乎为 0；**真实成本是 8 周工期 + 可能证伪的机会成本**。与 P1-C 花 1 天证伪 FADE 相比，P1-D 若证伪会花掉 8 周 — 需要中途 gate (Phase 2A end) 提前 NO-GO。

### 7.3 过度工程风险

**具体风险**：做完 6 类特征后全部 R² < 0.005，返回 P1-C 同样状态。

**缓解**:
- Phase 2A 提前 gate：3 核心特征任一达标才进 Phase 2B
- 每周 status update 给用户，不搞闭门造车 8 周

### 7.4 与现有 market 进程的耦合

**风险**：新 collector 挤占 NATS / DB / network，影响主交易 snapshot 链路。

**缓解**:
- 新 collector 独立 DB connection pool
- 不发 NATS（Bronze 写 DB 即可，Silver ETL 从 DB 读）
- Phase 1 用现有 aats-market 容器；Phase 2 若 ETH 接入则拆独立容器 `aats-microstructure-collector`

### 7.5 OKX API 策略变化

**风险**：OKX 未来把 `books` 频道也加 VIP 门槛（历史上已经做过：books-l2-tbt 从 free 改 VIP5）。

**缓解**:
- Phase 1 主要靠 `books5` (top-5) 和 `bbo-tbt` (top-1)，不依赖 full-depth books
- 若 OKX 变动，top-5 级别 OBI 仍能保留；只损失 Phase 2 的深度扩展
- 定期检查 OKX `docs-v5/log_en` 变更日志

### 7.6 Regime drift / 样本代表性

- 本调研期 BTC 74-76k 震荡偏涨；Phase 1 数据累积期的 regime 未知
- 若 Phase 2A 只赶上窄区间，R² 可能偏高（过拟合 regime）

**缓解**:
- 等至少 4 周数据再做 Phase 2A 一轮
- Phase 2B 强制 2 个不重叠窗口的鲁棒性检验
- Phase 3 保留 1-2 周做"新窗口外样本"验证

### 7.7 和 P1-C FADE 调研重叠

- P1-C 在 30 天数据累积后可复跑 (`event_store` retention + Path B)
- P1-D Phase 1 积累的 silver 数据也可喂 FADE 特征
- **协同**：P1-D Phase 2 的特征工程 produce 的 `silver.market_*` 表可作为 FADE 调研新 X variable，无缝复用

---

## § 8. 成功 / 失败判据 (Gate)

### 8.1 W8 决策门槛

| 判定 | 条件 |
|---|---|
| **GO** | 至少 1 个 feature 或 feature combo 在 raw-level 15m horizon 上 **R² ≥ 0.01 且 slope sign 稳定** (两个不重叠 30d 窗口) 且 **cost-adjusted net > 2 bps @ n ≥ 500**。 |
| **CONDITIONAL-GO** | R² 在 0.005-0.01，或 regime-specific subset 强但 global 弱。建议 microstructure sleeve 上线为 "observation-only"（不开仓，只打 score），再观察 2 周样本外。 |
| **NO-GO** | 全部 feature combos raw-level R² < 0.005，或 R² 达标但 cost-adjusted net ≤ 0。转 §8.3 兜底。 |

### 8.2 硬数值 threshold

- R² (raw-level, 15m, primary feature combo) ≥ 0.010
- Slope sign consistent across 2 non-overlapping 30d windows
- Pearson r ≥ 0.10
- Win rate (cost-adjusted) ≥ 52%
- Mean net_bps (cost=6) > 2 bps @ n ≥ 500
- Sharpe (daily) ≥ 0.5
- Bonferroni-corrected p-value (accounting for 6 features × 4 horizons × 2 legs = 48 tests) < 0.05

### 8.3 NO-GO 兜底 — γ 路径 (funding/basis 非定向)

若 W8 NO-GO：

- **γ-A (funding carry)**: 利用 funding rate 大偏离时做 **non-directional pair trade**（Open Interest hedged）。日级收益 2-5 bps × N days，累积 Sharpe 高于 directional 策略。工期估 2-3 周。
- **γ-B (basis arbitrage)**: Spot-perp basis convergence trade。需要现货数据源（OKX 现货 or 其他交易所）。工期 3-4 周。
- **γ-C (funding event-driven)**: 每 8h funding 结算前后 ±15min 窗口的价格偏差 + 恢复。事件稀疏，需要 6+ 月数据。

γ 路径是 "非定向 → 稳健 carry 型策略"，与 P1-A/P1-C/P1-D "directional alpha search" 路径正交，即使 directional alpha 全军覆没，γ 仍可独立盈利。

### 8.4 与 Path B (event_store retention) 协同

Path B (扩 `event_store` 到 14 天 + archive) 若并行启动，P1-C FADE 可在 30 天数据后复跑。

**时间线协同**：
- Phase 1 (P1-D W1-W2): 同步启动 Path B retention
- Phase 2A (P1-D W3-W4): event_store 已 14 天，还不够 FADE 复跑
- Phase 2B (P1-D W5-W6): event_store 30+ 天，可启动 **P1-C FADE 复跑**（独立 spawn task）
- Phase 3 (P1-D W7-W8): 若 P1-D GO + P1-C 也 GO → 决策哪个优先上线；若 P1-D NO-GO + P1-C 复跑仍 NO-GO → 同时 fail over 到 γ 路径

两条路径**并行不阻塞**，Phase 2B 时有 "四象限决策"（每条路径 GO / NO-GO 组合）。

---

## § 9. 疑问 / 需求澄清

**本调研无法靠文档自决的问题**：

1. **AATS 实盘 OKX VIP 级别是什么？** 当前 `.env.derivatives.live` 配置 taker_fee=5 bps 看起来是 VIP0 或 VIP1。若账户累积到 VIP3+，可考虑 `books` full depth 订阅策略（现已可用）+ 如果有 VIP4/5 可加 `books50-l2-tbt`。**需要用户确认**当前 VIP 级别。

2. **现有 aats-market 容器能否承载新 ws collector？** Phase 1 粗估 CPU +15%、network +200KB/s。需要在 docker stats 上看当前容器实际负载有多少空间。**需要实际部署一次 stub collector 做 baseline。**

3. **`aats-market` 和新 microstructure collector 的 NATS 隔离策略？** 当前 `aats-market` 发 `market.snapshot.*` topic。若新 collector 同进程不发 NATS（只写 DB），是安全的；若独立容器要发，是否会污染 gateway 订阅？**需要和 slice refactor 设计对齐。**

4. **OKX BTC-USDT-SWAP 单 symbol 是否需要扩到 ETH/SOL 多 symbol？** 本调研倾向于 **Phase 1 只做 BTC**，Phase 2B 末尾决定。但若用户明确要做 BTC + ETH，从 W1 起就要双 symbol 订阅，存储和工期 ×1.5。**需要用户决策。**

5. **Path B (event_store retention 扩至 14d) 是否批准启动？** 与 P1-D 并行最能减少未来复跑阻塞。**需要 ops approval。**

6. **Phase 2A gate fail 时用户是否愿意提前 NO-GO 终止？** 若用户坚持"必须跑完全部 Phase"，要明确这点，避免 Phase 1 结束时出现"已经花了 3 周沉没成本，还要再花 5 周"的双方观感差异。**需要 commit 决策协议。**

7. **冷启动数据累积期（W1-W3）有没有更快方式？** OKX 历史 trades/books 不可回填，但是否可以从第三方数据源（Kaiko、Tardis.dev）买历史数据加速？**需要成本评估。**

8. **15m 决策粒度是否可以灵活调整为 5m？** 如果 microstructure feature 在 5m horizon R² 显著高于 15m（文献支持），Phase 2 结果可能建议把 decision trigger 从 15m 改到 5m，需要和 decision_engine / strategy_engines 团队对齐改造成本。

---

## 附录 A. 与 Path B (event_store retention) 的协同

Path B 的要点（来自 P1-C 报告 §10.4）：
- 扩 `event_store.strategy.baseline_assessment` 等 topic 的 retention 到 35 天
- 或实现 archive pipeline 把 old events 移到冷存储可查

**协同策略**:

| 周次 | P1-D | Path B |
|---|---|---|
| W1 | Phase 1A: collector + tables | retention patch merge |
| W2 | Phase 1A continue | 开始累积（已有数据不回填） |
| W3 | Phase 1B 等数据 | W3 end: 21 天数据可用 |
| W4 | Phase 2A regression | 28 天数据可用 |
| W5 | Phase 2B | **30 天 → P1-C 可复跑 spawn** |
| W6 | Phase 2B | P1-C spawn 完成 |
| W7 | Phase 3 | — |
| W8 | 综合决策 | 包含 P1-D + P1-C 双结论 |

---

## 附录 B. 调研方法说明

**本调研 NOT 做**：
- 没有采集任何 OKX 实时数据
- 没有对生产代码做任何修改
- 没有 commit 任何 aats/** 文件
- 没有读取 .env 等凭证文件内容
- 没有发起任何 OKX REST / WebSocket 请求

**本调研 DID 做**：
- 读 4 份前置文档
- 查 AATS 现有代码结构（market_gateway, feature_engine, data_platform, schemas）
- 通过 WebSearch 获取 OKX 公开 channel 规格、VIP 要求
- 通过 WebSearch 汇总 9 篇学术文献/业界报告关于 microstructure predictability
- 产出本决策文档（Silver schema DDL 草稿、pseudo-code 未跑）

**确定的 facts (来自项目代码)**:
- AATS 现订 OKX WS channels: `tickers`, `candle15m`, `candle1H`, `mark-price`, `funding-rate`, `open-interest`, `liquidation-orders`（见 `aats/services/market_gateway/okx_websocket.py:568-592`）
- `recent_trades=[]` 硬编码空（`aats/services/market_gateway/okx_normalizer.py:462`）
- `orderbook_depth` 只有 top-of-book 1 level（同文件 L446-449）
- `oi_state.py` 有 20-period EMA，60-snapshot rolling window
- RDP 现有 Bronze/Silver/Gold 表主要是 candles + funding + liquidations（见 `aats/data_platform/rdp_models.py`）

---

## 附录 C. 参考文献

**学术 / 工业报告**:

1. Cont, R., Kukanov, A., Stoikov, S. (2014). *The Price Impact of Order Book Events*. Journal of Financial Econometrics. — OFI 开山作。
2. Kolm, P., Turiel, J., Westray, N. (2023). *Deep Order Flow Imbalance: Extracting Alpha at Multiple Horizons*. — https://arxiv.org/abs/2011.10230
3. Anastasopoulos, A., Gradojevic, N. (2025). *Order Flow and Cryptocurrency Returns*. EFMA 2025 Annual Meeting. — http://www.efmaefm.org/0EFMAMEETINGS/EFMA%20ANNUAL%20MEETINGS/2025-Greece/papers/OrderFlowpaper.pdf
4. Sifat, I., Mohamad, A. (2019). *Lead-Lag Relationship between Bitcoin and Ethereum: Evidence from Hourly and Daily Data*. Research in International Business and Finance 50. — https://www.sciencedirect.com/science/article/abs/pii/S0275531919300522
5. *Bitcoin wild moves: Evidence from order flow toxicity and price jumps* (2025). ScienceDirect. — https://www.sciencedirect.com/science/article/pii/S0275531925004192
6. *Deep Learning for VWAP Execution in Crypto Markets: Beyond the Volume Curve* (2025). arXiv 2502.13722. — https://arxiv.org/abs/2502.13722
7. *Nowcasting Bitcoin's crash risk with order imbalance*. PMC 10040314. — https://pmc.ncbi.nlm.nih.gov/articles/PMC10040314/
8. *A high-frequency GMM-VAR approach to crypto lead-lag* (2025). DSFE. — https://www.aimspress.com/aimspress-data/dsfe/2025/3/PDF/DSFE-05-03-017.pdf
9. Markwick, D. (2022). *Order Flow Imbalance — A High Frequency Trading Signal*. — https://dm13450.github.io/2022/02/02/Order-Flow-Imbalance.html
10. *Explainable Patterns in Cryptocurrency Microstructure*. arXiv 2602.00776. — https://arxiv.org/html/2602.00776v1

**OKX 官方 / 业界参考**:

11. OKX API Guide v5 — https://www.okx.com/docs-v5/en/
12. *OKX Will Change Subscription Rules for TBT Order Book Channels on WebSocket API* — https://okxsupport.zendesk.com/hc/en-us/articles/6575472744845
13. OKX Taker Buy/Sell Ratio 解释 — https://www.okx.com/learn/taker-buy-sell-ratio
14. BitMEX 2025 Q3 Derivatives Report (funding structure) — https://www.bitmex.com/blog/2025q3-derivatives-report
15. Deribit Insights — Perpetual Swap Funding — https://insights.deribit.com/education/perpetual-swap-funding/
16. Glassnode — Pressure Points: Liquidation Heatmaps & Market Bias — https://insights.glassnode.com/liquidation-heatmaps/
17. The Rhythm of Liquidity: Temporal Patterns in Market Depth (Amberdata) — https://blog.amberdata.io/the-rhythm-of-liquidity-temporal-patterns-in-market-depth

---

## 附录 D. 签署

- **调研发起**: 用户批准 P1-D 立项 (2026-04-19)
- **前置证据**: P1-A CHASE 证伪 + P1-C FADE CONDITIONAL-GO (均为同日)
- **调研边界**: 读文档 + 读源码 + WebSearch 文献；零代码改动、零数据采集
- **决策建议**: **GO Phase 1**，W3 gate 做 Phase 2A 准入决定，W8 最终 GO/NO-GO
- **最短可能失败路径**: W4 end（Phase 2A 结束时）若 3 核心特征 R² 全 < 0.005，**提前 NO-GO**，沉没 2.5 人周 + 基础数据管道（可复用）
- **最长成功路径**: W8 完整跑完 GO → W9+ 进入路径 A（microstructure sleeve）或路径 B（integrated feature）实施
