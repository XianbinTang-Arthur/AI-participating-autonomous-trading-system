# Stage 9 — Dryrun 实盘阶梯检查清单

> **本文档定位**：从 Stage 8 完成之后（4 进程拓扑 + OTel + Jaeger trace 链路
> 已就绪）进入真金白银实盘前的最后一道关口。不写业务逻辑设计，不碰
> 策略参数，**只规定"跑到哪一步必须验证什么，什么情况必须立刻停"**。
>
> 本文档与 `docs/task/derivatives_overlay_rollout_runbook.md` 的区别：
> - rollout_runbook 面向 derivatives overlay 业务线的单独灰度
> - 本文档面向 **整个 4 进程系统在 OKX spot 主账户上首次真金白银运行**，
>   范围更广，阶梯更保守（1U → 10U → 100U → 1000U）

## 0. 阶梯规模定义

| 阶梯 | 名义账户余额 | 单笔最大下单 | 触发条件 | 预期观察窗口 |
| ---- | ----------- | ------------- | ------- | ------------ |
| T0 DRY | 0 U（paper adapter） | 不发单 | 任意时刻 | 24h |
| T1  | ≤ 1 USDT | ≤ 1 USDT | Stage 8 验收全绿 | ≥ 48h 连续稳定 |
| T2  | ≤ 10 USDT | ≤ 2 USDT | T1 全绿 + drift score ≤ 1 | ≥ 72h |
| T3  | ≤ 100 USDT | ≤ 10 USDT | T2 全绿 + 成交数 ≥ 50 | ≥ 7 天 |
| T4  | ≤ 1000 USDT | ≤ 50 USDT | T3 全绿 + 无 SEV2+ 告警 | ≥ 14 天 |

> **资金纪律**：每一阶梯的"最大账户余额"都是**硬上限**。超出上限必须先下调
> 再继续阶梯；不允许因为收益好而私自加仓。加仓需要单独评审（写 SOW）。

## 1. 前置硬条件（T0 → T1 升级准入）

下列每一条不满足则**禁止**把真实资金打进 OKX 账户。

### 1.1 代码完整性
- [ ] `git status` 干净（no untracked、no modified）
- [ ] `git log --oneline origin/main..HEAD` 为空（已全部 push）
- [ ] 最新 commit 通过 CI lint + pytest 全绿
- [ ] `pytest tests/unit tests/integration -q` 本地最后一次跑 → 全绿
- [ ] 所有 `TODO`/`FIXME` 都已核查（用 `grep -r "FIXME.*dryrun" .` 搜一次）

### 1.2 环境健康
- [ ] WSL2 4 进程拓扑全部 `healthy`（gateway/market/decision/execution）
- [ ] 基础设施 9/9 `healthy`（postgres/redis/nats/loki/promtail/jaeger/prometheus/redis-exporter/grafana）
- [ ] 最近 1 小时无 `telemetry_bootstrap_failed` / `event_persistence_failed`
      / `nats_handler_error` / `hot_state_store_*_failed` 日志
- [ ] Jaeger 里最近 10 分钟有 ≥ 5 条 multi-service trace（即 `nats.publish →
      nats.receive` 跨进程透传工作正常）

### 1.3 Kill switch 可用性
- [ ] `probe_kill_switch.py halt stage9_dryrun_preflight` 能成功 broadcast
- [ ] 4 个容器都在 100ms 内收到 halt 事件（`grep kill_switch` 日志）
- [ ] `probe_kill_switch.py resume` 能成功回滚
- [ ] Redis `aats:hot:system:kill_switch` 的 `set_at_ts` 更新正确

### 1.4 凭证与密钥
- [ ] OKX API key 已放到 WSL2 的 secrets store（`docs/reference` 对应
      文档，**不能**写到 repo 里）
- [ ] API key 权限**仅限现货 spot + 仅限下单/撤单/查询**；没有提现权限
- [ ] IP 白名单已设成 WSL2 宿主机公网 IP，没开全开
- [ ] `.env.wsl2` 里的 `OKX_API_KEY` 等变量是 placeholder，真实值通过
      docker secrets 或 `docker compose run -e OKX_API_KEY=$(pass okx/apikey)` 注入
- [ ] `scripts/verify_okx_keys.sh` 本地跑一次 → `permissions=read,trade`，
      `withdraw=false`

### 1.5 Active parameter set 冻结
- [ ] `aats/bootstrap/active_parameters.py` 当前激活的 profile 是明确的
      （`settings_provenance_report` 里能看到 `active_parameters` 层）
- [ ] profile 最近 24h 内没被修改（`git log state/active_parameters/`）
- [ ] profile 的 `ai_service.assessment_enabled` 是 **false**（T1 不开 AI，
      只跑 baseline 决策）
- [ ] `execution_engine.paper_mode` 是 **false**（T1 必须真发单才算真跑）
- [ ] `AATS_KILL_SWITCH_HALTED` 环境变量未设置（确保 bootstrap hydrate 能
      从 Redis 读到最新状态）

### 1.6 T0 DRY 跑 24h
- [ ] paper adapter 模式下跑满 24h 无崩溃（`docker logs --since=24h` grep
      `process_lifecycle_heartbeat_started` 有，`Traceback` 无）
- [ ] 24h 期间 ≥ 1 次完整 decision_engine.run_cycle 产出 position_target
      （Jaeger 里找 `decision_engine.run_cycle` span）
- [ ] ≥ 1 次 fill_event 路径走通（paper adapter 也会模拟 fill）
- [ ] reconciliation_service 的 24h 汇总无 `mismatch` 记录

## 2. T1（≤ 1 USDT）启动步骤

> **顺序非常重要**。每一步做完都要在日志里确认，出错立刻回退到 §7 紧急停机。

### 2.1 账户充值
1. 先在 OKX 网页手动建一个**子账户**（避免主账户混淆）
2. 充 1.5 USDT（留 0.5 USDT 缓冲给手续费 + 滑点）
3. 确认子账户 API key 权限只开了 spot trade
4. `scripts/verify_okx_keys.sh` 最后跑一次 → 返回子账户余额 ≈ 1.5 USDT

### 2.2 把凭证注入运行环境
```bash
# 在 WSL2 宿主机，不要 echo 到终端
export OKX_API_KEY="$(pass okx/t1/api_key)"
export OKX_API_SECRET="$(pass okx/t1/api_secret)"
export OKX_API_PASSPHRASE="$(pass okx/t1/passphrase)"

cd ~/aats/deploy/wsl2-dev
docker compose -f docker-compose.aats.yml --env-file .env.wsl2 \
  up -d --force-recreate
```

- [ ] 4 个容器 recreate 后 `healthy`
- [ ] `docker logs aats-execution 2>&1 | grep okx_adapter_initialized`
      显示 `account_id=<子账户 id>` 且 `permissions=["read","trade"]`

### 2.3 启动 observer pane（独立 tmux 窗口）
```bash
# pane 1: execution 日志实时
docker logs -f aats-execution 2>&1 | grep -E 'order_intent|fill_event|okx_error'

# pane 2: decision 日志实时
docker logs -f aats-decision 2>&1 | grep -E 'decision_cycle|position_target'

# pane 3: 账户余额轮询
watch -n 30 'scripts/verify_okx_keys.sh | tail -5'

# pane 4: Jaeger UI
xdg-open http://localhost:16686
```

### 2.4 首笔订单观察
- [ ] 第一笔 order_intent 出现在日志（等第一次 decision_cycle）
- [ ] okx_adapter 返回 `order_placed client_order_id=...` 且无 error
- [ ] 5 秒内出现 `fill_event` 或 `order_filled`
- [ ] Jaeger 里能看到 `execution_engine.handle_order_intent` 完整 span，
      attributes 里的 `aats.quantity` ≤ 1 USDT 等值
- [ ] portfolio_snapshot 的 `cash_equity` 下降合理（= 1 USDT - 成交金额 -
      手续费），不是负数、不是 NaN

## 3. 运行时持续监控（T1 及以上阶梯都要跑）

### 3.1 必须常驻的 dashboard 面板
- Jaeger UI：按 service 查 error rate（Duration/Error 标签）
- Grafana 面板：
  - 4 进程 heartbeat（`/tmp/aats_<role>_heartbeat` mtime）
  - `decision_cycles` metric 每分钟计数（应 ≥ 1）
  - `fill_events` metric 每小时计数（与 order_intents 的比例应 > 0.8）
  - postgres connection pool 空闲数（应保持 ≥ 2）
  - redis latency p99（应 < 10ms）
- Loki 查询 saved search：
  - `{level="error"}` 所有 error 日志
  - `{logger=~"aats\\.reconciliation.*"} |= "mismatch"` reconciliation 偏差

### 3.2 人工抽检节奏
- **T1**：每 2 小时抽检一次
- **T2**：每 4 小时抽检一次
- **T3**：每 8 小时抽检一次（可安排夜班交给 prometheus alert）
- **T4**：每 24 小时抽检一次 + alert-driven 处理

每次抽检项：
1. 账户余额与 portfolio_snapshot 对得上（偏差 < 1%）
2. 近 N 小时没有 SEV2+ 告警（定义见 §5）
3. Jaeger 里 `gateway.http.*` span 的 p99 < 500ms
4. decision_engine.run_cycle 的 p95 < 30s（单个周期不超时）

## 4. 阶梯升级条件（T_n → T_{n+1}）

下列**所有**条件满足才能升级：

### 4.1 时间窗口
- [ ] T_n 已经连续运行满足 §0 表格里的"预期观察窗口"
- [ ] 观察窗口内没有手动重启 / 手动平仓

### 4.2 财务健康
- [ ] 账户实际余额 - 初始余额 - 手续费总和 = portfolio_snapshot 的 realized+unrealized
      pnl（误差 < 1%）
- [ ] 最大回撤 < 阶梯规模的 5%（T1 = 0.05 USDT，T4 = 50 USDT）
- [ ] 没有单笔下单超过阶梯的 `单笔最大下单` 上限
- [ ] 没有手续费异常（> 期望值的 2 倍）

### 4.3 系统健康
- [ ] 近 24h 无 `kill_switch_halted` 记录（除 §1.3 主动 drill 之外）
- [ ] 近 24h 无 `reconciliation_mismatch` 记录
- [ ] 近 24h 无 `okx_rest_rate_limited` 连续 > 3 次
- [ ] 近 24h 无 `nats_handler_error` 连续 > 5 次
- [ ] 近 24h `decision_cycle_completed` 日志数 ≥ 预期数的 95%（触发周期
      每 15m / 1h，算一下应该多少次）

### 4.4 drift 检测
- [ ] `scripts/compute_drift_score.py`（见 §8 checklist-2 设计）返回
      `score ≤ 1`
- [ ] 近 24h 无 `drift_detected_above_threshold` 日志

### 4.5 人工 go/no-go 复核
- [ ] 当天 ≥ 1 次把最近 6 小时的 Jaeger trace 抽样 5 条，确认 span 链路
      完整（没有 `nats.receive` 孤儿、没有 `handler_error`）
- [ ] 当天看一次 grafana 的 fill 成功率面板，确认阶梯内 ≥ 98%

**任何一条不满足就原地观察，不要加阶梯规模，不要加仓。**

## 5. 告警严重度定义

| 级别 | 定义 | 响应 |
| ---- | ---- | ---- |
| SEV1 | 资金安全损失（余额偏差 > 1%、reconciliation mismatch、kill_switch 被第三方触发） | **立刻** halt + 人工介入 + 事后写 root cause |
| SEV2 | 单进程崩溃、连续 N 次 okx_error、nats 不通、redis 不通 | 15 分钟内 halt + 诊断 |
| SEV3 | 单次 handler_error、单次 rate_limited、单次 timeout | 记录 + 下一轮抽检时复核 |
| SEV4 | 可观测性降级（jaeger 采样丢失、grafana 面板 stale） | 下一个工作时段处理 |

SEV1 必须 **立刻** 执行 §7.1 紧急停机；禁止"再观察一会儿"。

## 6. 中断与回退机制

### 6.1 正常下阶梯
当 §4 的升级条件在下一阶梯不再满足，**必须回退到前一阶梯**：
1. `probe_kill_switch.py halt scale_down_to_T<n-1>`
2. 等待所有 open order 被交易所 filled 或 canceled
3. 人工把子账户余额提回前一阶梯的上限内（例如 T3 → T2 就把 100 USDT
   撤回到 ≤ 10 USDT）
4. 调整 profile 的 `exec.max_order_size_usdt` 参数回前阶梯值
5. `probe_kill_switch.py resume`
6. 重新进入前阶梯的观察窗口

### 6.2 永久中止
任何 SEV1 事件触发后，默认**永久中止当前阶梯**：
- 余额锁在子账户里不动
- 写事后复盘 `docs/task/dryrun_incident_YYYYMMDD.md`
- 下次尝试阶梯必须从 T0 重新开始

## 7. 紧急停机

### 7.1 最小侵入（推荐）
```bash
# 在任意 AATS 容器里跑 probe
docker exec aats-decision python /tmp/probe_kill_switch.py halt \
  emergency_$(date +%s)

# 验证所有 4 个容器都收到
for c in gateway market decision execution; do
  docker logs aats-$c 2>&1 | grep kill_switch_applied | tail -1
done
```

- execution 进程会停止处理新的 order_intent，但**不会**主动撤已发
  出的 open order
- 如果需要撤 open order：手动登录 OKX 网页，或写独立脚本调
  `okx_rest.cancel_all_orders`（不要在紧急时刻改 aats 代码）

### 7.2 强制停机（数据丢失风险）
```bash
cd ~/aats/deploy/wsl2-dev
docker compose -f docker-compose.aats.yml --env-file .env.wsl2 stop \
  --timeout 5
```

- 会打断正在 flight 的 okx request，有可能出现"订单成功但 execution 没
  记录"的 reconciliation 偏差，下次启动需要手动跑 §7.3 reconcile
- 仅在 §7.1 不奏效（container 失去响应、probe 连 NATS 超时）时使用

### 7.3 启动后 reconciliation
```bash
docker exec aats-execution python -c "
import asyncio
from aats.services.audit_service.reconciliation import ReconciliationService
# 触发一次 full refresh，比对 OKX 账户 vs 本地 portfolio
"
```

- 如果出现 `reconciliation_mismatch`，**立刻** halt（§7.1），切记不要
  trusted repair 把差异抹掉
- 手动 diff 两边的订单列表，人工判断谁对谁错，写复盘

## 8. 与 checklist-2/3/4/5 的衔接

本文档只规定了"检查什么、怎么响应"。自动化工具由后续 checklist 补齐：

- ✅ **checklist-2（设计）**：`docs/task/stage_9_abort_hooks_design.md`
  —— drift score 10 个指标的阈值表、abort hook 状态机、CLI 退出码。
- ✅ **checklist-3（drift score 纯函数 + CLI）**：
  - `aats/services/governance_engine/drift_score.py` —— `compute_drift_score`
    纯函数，给 §4.4 的 drift score ≤ 1 gate 用
  - `scripts/compute_drift_score.py` —— offline / live / mock 三种数据源
    + JSON/表格/verbose 输出 + exit code 0/2/3/4 映射
  - `tests/unit/test_stage9_drift_score.py` + `test_stage9_drift_score_cli.py`
    —— 60 个单测覆盖阈值、归一化、missing-data 规则、CLI exit code
- ✅ **checklist-4（abort hook sidecar）**：
  - `aats/services/governance_engine/abort_hooks.py` —— `AbortHookService`
    后台 sidecar，定期跑 drift score 并在命中时自动
    `kill_switch.halt(reason=stage9_abort_hook:<code>)`
  - `aats/bootstrap/settings.py` 5 个 `stage9_abort_hook_*` 字段（默认关）
  - `aats/bootstrap/config.py` `ApplicationRuntime.abort_hook_service` +
    `_apply_post_init_guards` 里与 trial_guard 一起创建
  - `tests/unit/test_stage9_abort_hooks.py` —— 26 个单测覆盖状态机、
    cooldown、回调、halt reason 编码
  - 实战开启：设置 env `AATS_STAGE9_ABORT_HOOK_ENABLED=true`（见
    `deploy/wsl2-dev/RUNBOOK.md` §9.7.6）
- ✅ **checklist-5（runbook 验证步骤 + WSL2 drill）**：
  - `deploy/wsl2-dev/RUNBOOK.md` §9.6 drift score CLI 真跑冒烟
  - `deploy/wsl2-dev/RUNBOOK.md` §9.7 AbortHookService halt drill（self-check
    + score_ge_5 / subscore_financial_2 / consecutive 三条 halt 路径）
  - `deploy/wsl2-dev/probe_abort_hook.py` 驱动脚本（对齐 probe_kill_switch.py）
- **待办（不阻塞 dryrun，checklist-4 收尾 slice 或后续专项做）**：
  - AbortHookService 的 `inputs_provider` 从 trial_guard 扩展到 portfolio /
    ledger / health_service / quality_monitor 全量采集（目前只接了
    `fee_to_notional_ratio` + `high_slippage_ratio` 两个字段）
  - Grafana alert rules / Loki saved search：把 §5 的 SEV1/SEV2 告警自动化

## 9. 阶梯升级决策日志模板

每次阶梯切换都必须在 `docs/task/dryrun_ladder_log.md` 里追加一条：

```markdown
## YYYY-MM-DD HH:MM UTC+08 — T<n> → T<n+1>

### 升级前状态
- 当前余额：<USDT>
- 观察窗口起止：<start> → <end>
- 决策周期数：<count>
- fill 数：<count>
- 最大单笔：<usdt>
- 最大回撤：<usdt> / <percent>

### §4 准入项逐条勾选
- [x] 时间窗口：<实际时长>
- [x] 财务对账：偏差 <x>%
- [x] 系统健康：<0 SEV1, 0 SEV2, N SEV3>
- [x] drift score：<score>
- [x] go/no-go 复核签字：<签字人>

### 升级后目标
- 新余额上限：<usdt>
- 新单笔上限：<usdt>
- profile 参数调整：<diff>

### 预期下一次复核时间
- <datetime>
```

## 10. 终点：T4 通过后的永久运行

T4 连续运行满 14 天且全绿后，算作正式实盘。之后的扩容、策略调参都走
正常 change management 流程（SOW → 评审 → 灰度 → 上线），不再参照本
dryrun 清单。

本文档完。
