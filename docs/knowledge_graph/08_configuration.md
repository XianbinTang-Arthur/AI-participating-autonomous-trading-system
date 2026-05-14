# 08 · 配置体系

> **生成于 HEAD=待更新** · 2026-04-21
> **内容**：AATS 的所有配置来源 / 优先级 / 核心字段

---

## TL;DR

配置分 **4 层**，按优先级从低到高：

1. **Pydantic 默认值** (`aats/bootstrap/settings.py` 里的字段默认) — 1144 行，
   定义所有可配字段和默认值
2. **YAML profile** (`configs/strategy_profiles/*.yaml`) — 按 profile 覆盖部分字段
3. **Docker env_file** (`.env.wsl2`, `.env.derivatives.live`) — 每个进程启动时
   加载（`AATS_*` 前缀）
4. **Runtime override**（`_manual_operating_mode_override`）— 极少数场景下用户
   通过 API 临时覆盖

Pydantic 解析顺序：环境变量 > env_file > defaults。YAML 通过 `load_managed_profile_values` 提前合并进 env。

---

## 文件清单

### `aats/bootstrap/settings.py` — 核心 schema

`class AATSSettings(BaseSettings)` 定义了**所有**可配字段。~400 个字段分组：

- **Identity / profile**: `profile`, `mode`, `default_symbol`, `allowed_symbols`
- **AI**: `ai_operating_mode`, `ai_provider`, `ai_model_name`, `ai_shadow_mode_enabled`
- **Trading**: `trading_product_type`, `margin_mode`, `derivatives_position_mode`, `default_order_qty`
- **Risk limits**: `max_abs_position_qty`, `max_notional_per_symbol`, `max_gross_notional_per_symbol`, `max_pending_notional_per_symbol`, `max_total_open_notional`, `max_target_leverage`, `max_open_orders`
- **Derivatives safety**: `derivatives_auto_halt_margin_usage_fraction` (default 0.85), `derivatives_only_reduce_trigger_margin_fraction` (0.75), `derivatives_auto_halt_liquidation_gap_fraction` (0.05)
- **OKX**: `okx_rest_url`, `okx_ws_urls`, `okx_timeout_seconds` (15), `okx_account_refresh_interval_seconds`, `okx_execution_sync_interval_seconds`, `okx_market_reconnect_delay_seconds`
- **Infrastructure**: `database_url`, `nats_url`, `hot_state_backend`, `hot_state_redis_url`
- **Strategy**: `strategy_family_active`, `strategy_family_*_enabled`, `strategy_family_*_shadow_mode_enabled`, `strategy_family_*_live_execution_enabled`
- **Event store**: `event_bus_backend`, `nats_stream_name`, hot_event_retention_days
- **Operator**: `operator_admin_username`, `operator_session_cookie_name`

### `configs/strategy_profiles/*.yaml` — Profile 覆盖

```
configs/
├── base.yaml                           # 通用默认
├── templates/                          # 模板（仅参考）
├── strategy_profiles/
│   ├── derivatives_live.yaml          ← 当前运行的 profile
│   ├── derivatives.yaml               # 衍生品通用
│   ├── spot_live.yaml                 # 现货实盘
│   ├── spot.yaml                      # 现货通用
│   └── ...
```

**加载**: `aats/bootstrap/managed_profiles.py` 里 `load_managed_profile_values`
按 profile 名找到对应 yaml，把字段合并进 env-style 字典，再喂给 Pydantic。

### `.env.*` 文件（凭证）

```
.env.wsl2              ← 基础设施凭证（Postgres / Redis 密码）
.env.derivatives.live  ← 衍生品实盘（含 OKX API 凭证）
```

**⚠️ 禁令**: 任何 AI agent / automated 工具都**不准读这些文件**（见 CLAUDE.md）。
本 session 里我曾意外 grep 输出包含 env 密码 —— 已记录并强化过滤词表。

---

## 当前实盘（`derivatives_live`）关键字段

| 字段 | 值 | 来源 |
|------|-----|------|
| `profile` | derivatives_live | docker env |
| `ai_operating_mode` | baseline_only | yaml |
| `trading_product_type` | derivatives | yaml |
| `margin_mode` | cross | yaml |
| `default_symbol` | BTC-USDT-SWAP | env |
| `allowed_symbols` | ("BTC-USDT-SWAP",) | env |
| `strategy_family_active` | independent | yaml |
| `strategy_family_independent_live_execution_enabled` | true | yaml |
| `max_abs_position_qty` | 0.1 | env |
| `max_notional_per_symbol` | 10000 | env |
| `max_total_open_notional` | 10000 | env |
| `max_target_leverage` | 20 | env |
| `default_target_leverage` | 10 | env |
| `derivatives_position_mode` | hedge | env |
| `max_open_orders` | 5 | env |
| `default_order_qty` | 0.001 | env |
| `initial_usdt_balance` | paper only | 仅 local paper/demo 与 real-market paper 本地账本初始化；OKX 实盘忽略 |
| `auto_halt_margin_fraction` | 0.75 | env |
| `only_reduce_trigger_fraction` | 0.65 | env |
| `auto_halt_liquidation_gap_fraction` | 0.10 | env |

实际账户权益：**~$393.73**（2026-04-21 采样）。

---

## 修改配置的安全路径

### 想改一个字段？按优先级优先考虑

| 想做什么 | 改哪 | 重启？ |
|---------|------|-------|
| 临时实验 | 改 `.env.derivatives.live`（本地） | 要 deploy 重启 |
| 永久改变 | 改 `configs/strategy_profiles/derivatives_live.yaml` | 要 deploy 重启 |
| 全局默认 | 改 `aats/bootstrap/settings.py` 的 Field 默认 | 要 deploy 重启 |
| 运行时临时覆盖 AI 模式 | API `/ai/operating_mode/override` | 即时 |

### ⚠️ 谨慎字段（改一个可能破整个系统）

- `ai_operating_mode` — 决定是否花 OpenAI 钱
- `strategy_family_active` — 改了所有 decision 走新策略
- `*_live_execution_enabled` — 关掉即系统不再下单
- 任何 `max_*` 风险上限 — 改大 = 资金暴露放大
- `derivatives_position_mode` — `hedge` vs `net` 不能随便切（有仓位时尤其危险）
- `default_target_leverage` — 影响所有新单

### 测试 config 改动的正确姿势

1. 本地单测全过：`python -m pytest tests/unit/ -x -q`
2. 本地 integration 测：`wsl ... pytest tests/integration/`
3. Staging 或 dry-run 先跑（AATS 当前没有显式 staging，只能 live mode 小心）
4. `scripts/deploy.sh --skip-commit`
5. 观察 30 min：`bash scripts/diag/pg_connection_health.sh` 等

---

## 配置热坑

### LF-相关（已记录）

- **LF-20260421-011**: `max_gross/pending/total_open_notional=0` 禁用检查而非硬拒
- **LF-20260421-017**: `AATS_PROCESS_ROLE` 打错字静默降级

### 其他观察

- **pydantic `spot_cash_runtime_requires_unit_leverage` 验证** — spot+cash 模式
  `max_target_leverage` 必须 = 1；运行时切 product_type 前要同时改 leverage
- **YAML profile 覆盖不是合并** — profile 的字段直接覆盖 env，所以 yaml 写了
  的字段 env 改无效（需要重复写）
- **`AATS_INITIAL_USDT_BALANCE`** — 仅 local paper/demo 与 real-market paper
  可作为本地账本启动值；OKX account-read 实盘可用余额必须来自交易所账户快照和本地 obligation

---

## 参考

- 运维 runbook：[09_operational_guide.md](09_operational_guide.md)
- 启动流程： `CLAUDE.md` 和 `scripts/deploy.sh`
