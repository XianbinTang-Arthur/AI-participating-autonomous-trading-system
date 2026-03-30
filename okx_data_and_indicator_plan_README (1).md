# OKX 市场数据与技术指标接入建议（面向七条主线）

## 目的

本文整理一份面向你当前自动交易系统的接入建议，目标是回答三个问题：

1. OKX 能通过 API 拉到哪些对策略有用的数据
2. 哪些数据应该作为统一特征层来计算
3. 七条主线分别最值得消费哪些字段与技术指标

> 结论先行：
> - OKX 更适合提供**原始市场数据**（K 线、ticker、trades、order book、mark/index、funding、OI 等）
> - **EMA / RSI / ATR / KDJ / SAR / StochRSI** 这类技术指标，建议统一在你自己的系统里计算
> - 对你的系统，价值最大的是：**directional、spot_grid、dca、independent**
> - 对 **smart_arbitrage**，技术指标只能做辅助，核心仍应是 **basis / funding / cost / execution**

---

## 1. 推荐的数据分层

建议把 OKX 数据分成三层：

### 1.1 原始市场数据层

这是底座，来自 OKX API / WebSocket：

- Candlesticks / K 线（OHLCV）
- Ticker / Best bid-ask
- Trades / 成交流
- Order book / 深度
- Mark price
- Index price
- Open interest
- Funding rate

### 1.2 统一特征层

基于原始数据统一计算，不依赖交易所 App 上展示的数值：

- EMA(5/10/20/60)
- RSI(6/14/24)
- ATR
- Bollinger width
- returns / realized volatility
- trend slope
- VWAP deviation
- order book imbalance
- trade flow imbalance
- spread bps
- mark-index basis
- perp-spot basis

### 1.3 策略消费层

每条主线只使用自己真正需要的特征，不要把所有指标直接灌进所有策略。

---

## 2. 七条主线逐条建议

---

## 2.1 方向性交易主线 directional

### 最推荐接入的 OKX 原始数据

- candlesticks
- ticker / best bid-ask
- mark price
- index price
- trades
- order book
- open interest
- funding rate

### 最推荐计算的特征

#### 趋势类
- EMA5 / EMA10 / EMA20 / EMA60
- EMA 快慢线斜率
- close 相对 EMA20 / EMA60 偏离
- breakout / recent high-low breakout

#### 强弱类
- RSI(6/14/24)
- MACD histogram
- StochRSI
- trend strength / ADX（可选）

#### 波动类
- ATR
- realized volatility
- Bollinger band width
- high-low compression / expansion

#### 微观结构类
- spread bps
- top-of-book imbalance
- trade flow imbalance
- microprice deviation

#### 合约辅助类
- funding deviation
- mark-index basis
- OI change rate

### 在 directional 里的推荐用途

- 用于 `direction_bias` 计算
- 用于 `confidence` 计算
- 用于 guardrail 的“是否适合扩仓”判断
- 用于 `target_quantity` 的动态缩放

### 第一阶段优先接入

只做第一期时，建议先上这 6 个：

- EMA20 slope
- close vs EMA20 deviation
- RSI14
- ATR14
- order book imbalance
- mark-index basis

---

## 2.2 智能套利主线 smart_arbitrage

### 最推荐接入的 OKX 原始数据

- spot candlesticks
- swap / futures candlesticks
- spot ticker
- swap / futures ticker
- order books
- trades
- mark price
- index price
- funding rate
- open interest

### 最关键的特征（核心，不是辅助）

#### 核心机会特征
- spot vs perp basis
- mark vs index basis
- annualized basis
- funding carry expectation
- basis z-score
- basis persistence
- basis mean reversion speed

#### 执行特征
- spread bps
- depth at target size
- estimated impact cost
- maker / taker cost estimate
- order book asymmetry

#### 风险特征
- OI surge
- funding spike
- basis jump volatility
- cross-venue divergence（如有跨 venue 设计）

### 技术指标的角色

技术指标有帮助，但只能做辅助过滤，例如：

- ATR：过滤极端波动环境
- realized vol：过滤 basis 失真环境
- EMA slope：帮助识别趋势踩踏期

### 不建议

不要让 RSI / KDJ / SAR 直接主导套利进场。  
这条线应继续以：

- basis
- funding
- cost model
- execution feasibility

为核心。

---

## 2.3 现货网格主线 spot_grid

### 最推荐接入的 OKX 原始数据

- spot candlesticks
- ticker
- trades
- order book

### 最推荐计算的特征

#### Anchor 相关
- recent VWAP
- rolling mean / median close
- robust anchor（过滤异常点）

#### Band 宽度相关
- ATR14 / ATR20
- realized volatility
- Bollinger width

#### 偏离相关
- close vs anchor bps
- close vs EMA20 bps
- deviation z-score

#### 交易环境
- spread bps
- volume burst
- depth adequacy

### 在 spot_grid 里的推荐用途

- 用 ATR / realized vol 动态调 `band_bps`
- 用 VWAP / median 替代简单均价 anchor
- 用 spread / depth 决定是否放缓 rebalance
- 用 deviation z-score 决定库存比例调整力度

### 第一阶段最推荐增强

- ATR-driven `band_bps`
- VWAP / median anchor

---

## 2.4 定投主线 dca

### 最推荐接入的 OKX 原始数据

- spot candlesticks
- ticker
- trades

### 最推荐计算的特征

- RSI14 / RSI24
- drawdown from recent high
- close < EMA20 / EMA60
- ATR percentile
- realized volatility percentile
- volume anomaly

### 在 dca 里的推荐用途

你当前已有：
- spot-only
- 持仓上限
- 时间窗
- pullback-only
- tranche budget

可以增强为：

#### pullback-only 过滤器增强
要求同时满足：
- 从近 N 日高点回撤达到阈值
- RSI 不高
- 波动不过热
- spread / 流动性可接受

#### tranche 动态化
- 波动太高时缩小 tranche
- 回撤更深且环境平稳时放大 tranche

### 不建议

不要把 DCA 改造成低频 directional alpha 系统。  
它依旧应以：

- 时间纪律
- 预算纪律
- 风险约束

为主，技术指标只做增强过滤。

---

## 2.5 protective overlay

### 最推荐接入的 OKX 原始数据

- mark price
- index price
- candlesticks
- ticker
- order book
- funding rate

### 最推荐计算的特征

- ATR / realized volatility
- downside momentum / downside breakout
- mark-index dislocation
- spread widening
- order book fragility
- adverse funding shift

### 在 protective 里的推荐用途

用于提升 `_protective_pressure_score()` 的实盘表达能力，例如：

- 主腿盈利回撤扩大时加压
- 波动突然放大时加压
- 流动性变差时加压
- funding / basis 转逆风时加压

### 不建议

不要因为 RSI 超卖就轻易取消 protective。  
protective 的首要目标是风险管理，不是抄底。

---

## 2.6 opportunistic overlay

### 最推荐接入的 OKX 原始数据

- candlesticks
- trades
- order book
- mark price / index price
- funding rate
- open interest

### 最推荐计算的特征

- short-term reversal score
- momentum exhaustion
- RSI / StochRSI
- basis deviation
- OI + price divergence
- trade-flow imbalance
- spread / liquidity quality

### 在 opportunistic 里的推荐用途

建议把“机会分”拆成两层：

#### signal edge
- reversal
- exhaustion
- basis deviation
- directional mismatch

#### execution edge
- cost 能否覆盖
- depth 是否足够
- spread 是否可接受
- churn / fee drag 是否允许

只有两层都过线，才允许机会腿存在。

---

## 2.7 independent 双账本

### 最推荐接入的 OKX 原始数据

- candlesticks
- ticker
- trades
- order book
- mark price
- index price
- open interest
- funding rate

### 最推荐计算的特征

#### Long book
- close > EMA20 / EMA60
- EMA slope > 0
- RSI not overbought
- positive trade-flow imbalance
- positive basis context
- healthy OI expansion

#### Short book
- close < EMA20 / EMA60
- EMA slope < 0
- RSI not oversold rebound
- negative trade-flow imbalance
- adverse funding / basis
- downside OI expansion

#### 双边通用
- ATR
- realized vol
- Bollinger width
- spread / depth / slippage estimate

### 在 independent 里的推荐用途

每条 book 不要由单一指标驱动，而应把这些特征聚合成 score：

- trend
- momentum
- microstructure
- volatility-adjusted conviction
- execution quality

这与你现有 independent score 框架天然兼容。

---

## 3. 第一阶段统一特征层设计建议

如果你不想一次接太多，建议先做一个统一特征层，供七条主线共享。

### 3.1 第一批必须拉回来的原始数据

- OHLCV from candlesticks
- best bid / ask
- order book top levels
- recent trades
- mark price
- index price
- funding rate
- open interest

### 3.2 第一批统一特征

- EMA20 / EMA60
- RSI14
- ATR14
- close-vs-EMA deviation
- realized volatility
- spread bps
- order book imbalance
- trade flow imbalance
- mark-index basis
- perp-spot basis

### 3.3 第一批主线消费映射

- directional：EMA、RSI、ATR、imbalance、basis
- smart_arbitrage：basis、funding、OI、depth、impact cost
- spot_grid：ATR、anchor deviation、spread
- dca：drawdown、RSI、ATR percentile
- protective：ATR、mark-index dislocation、liquidity stress
- opportunistic：reversal score、imbalance、basis deviation
- independent：trend + momentum + microstructure 综合分

---

## 4. 工程实现建议

### 4.1 不要依赖交易所 App 指标值

不要追求与你手机 App 图上的 EMA / RSI / KDJ / SAR 完全一致。  
更重要的是：

- 算法一致
- 时间边界一致
- 是否包含未收盘 K 线一致
- 回测与实盘一致

### 4.2 生产建议：WebSocket 为主，REST 为辅

建议：

- **WebSocket**：实时滚动特征
- **REST**：启动补数、断线恢复、历史回填

### 4.3 建议统一 feature schema

建议在系统中明确区分：

- raw market data
- derived features
- strategy inputs

不要让每条策略自己单独拉数据、自己单独算指标，否则：

- 容易不一致
- 难复盘
- 难排查
- 难做 A/B 对比

---

## 5. 推荐的数据结构草案

下面给一个简化版结构草案，便于后续落代码。

```python
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class MarketRawSnapshot:
    instrument: str
    ts_ms: int

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    bid_px: Optional[Decimal]
    ask_px: Optional[Decimal]
    bid_sz: Optional[Decimal]
    ask_sz: Optional[Decimal]

    mark_px: Optional[Decimal]
    index_px: Optional[Decimal]

    funding_rate: Optional[Decimal]
    open_interest: Optional[Decimal]


@dataclass
class MarketDerivedFeatures:
    instrument: str
    ts_ms: int

    ema20: Optional[Decimal]
    ema60: Optional[Decimal]
    ema20_slope: Optional[Decimal]

    rsi14: Optional[Decimal]
    atr14: Optional[Decimal]
    realized_vol: Optional[Decimal]

    close_vs_ema20_bps: Optional[Decimal]
    spread_bps: Optional[Decimal]
    order_book_imbalance: Optional[Decimal]
    trade_flow_imbalance: Optional[Decimal]

    mark_index_basis_bps: Optional[Decimal]
    perp_spot_basis_bps: Optional[Decimal]


@dataclass
class StrategyFeatureView:
    directional_score_inputs: dict
    smart_arbitrage_inputs: dict
    spot_grid_inputs: dict
    dca_inputs: dict
    protective_inputs: dict
    opportunistic_inputs: dict
    independent_inputs: dict
```

---

## 6. 最终结论

### 帮助最大
- directional
- spot_grid
- dca
- independent

### 辅助价值明显
- protective
- opportunistic

### 不能喧宾夺主
- smart_arbitrage  
  这条必须以：
  - basis
  - funding
  - cost
  - execution
  为核心，技术指标只做辅助。

---

## 7. 最小可落地版本（建议）

如果你准备立刻动手，我建议第一期只做这几件事：

### 原始数据
- candlesticks
- best bid / ask
- order book top levels
- trades
- mark price
- index price
- funding rate
- open interest

### 特征
- EMA20 / EMA60
- RSI14
- ATR14
- spread bps
- order book imbalance
- trade flow imbalance
- mark-index basis
- perp-spot basis

### 先喂给这些主线
- directional
- smart_arbitrage
- spot_grid
- dca
- independent

protective / opportunistic 放第二期增强即可。

---

## 8. 一句话建议

**OKX API 最适合作为统一原始市场数据源；技术指标不要依赖交易所展示值，而要由你的系统统一计算，再按七条主线有选择地消费。**
