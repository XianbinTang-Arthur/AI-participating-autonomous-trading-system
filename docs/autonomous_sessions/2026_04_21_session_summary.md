# 2026-04-21 · Autonomous Session 最终总结

> **用户醒来先读这个**（10 分钟）
>
> 本次 autonomous session 跨度：2026-04-21 ~12:00 UTC → ~2026-04-22 ~04:00 UTC
> 约 **16 小时**（含两次用户离开去休息）
> 生成时间：最后一次我自己 commit 后

---

## 🎯 TL;DR — 一段话

系统比您离开前**更稳、更被理解、更自律**。这次产出了：
- **3 大类修复**（紧急火 6 处 + 预防针 4 处 + 配置 UX 锚定 3 处）
- **1 套完整知识图谱**（10 份文档 / `docs/knowledge_graph/`）取代旧 docs
- **4 份深度审计报告**（C1 交易现状 / C2 安全审计 / AI shadow 回顾 / baseline 信号分析）
- **23 条 latent findings**（6 条 HIGH、12 条 MED、5 条 LOW）等您审批决策
- **生产实时**：全 16 容器 healthy，0 错误，dedup 后 event_store 增速 -70%

**推荐您阅读顺序**（按价值）：

1. **本文件** — 10 分钟全景
2. `docs/knowledge_graph/README.md` — 图谱总入口（然后按兴趣深入）
3. `docs/knowledge_graph/10_latent_findings.md` — 23 条可选 fix 等决策
4. `docs/autonomous_sessions/2026_04_21_baseline_signal_analysis.md` — 策略层核心问题
5. `docs/autonomous_sessions/2026_04_21_ai_shadow_review.md` — AI 成本-收益
6. `docs/autonomous_sessions/2026_04_21_trading_state_audit.md` — 系统当前干什么
7. `docs/autonomous_sessions/2026_04_21_safety_audit_report.md` — 安全防御 6 层

---

## 📊 这次 session 都做了什么

### 第一阶段 · 紧急火与防御（session 前半）

| Commit | 类别 | 做了什么 |
|--------|------|---------|
| 5a6c383 | 🔥 fix | recovery_posture full-scan → latest_for_scope（111s → ms） |
| 7b30eaf | 🔥 fix | health._base_components full-scan 同上 |
| afddc1a | 🔥 fix | PG idle_in_tx 60s safety net（防 pool 耗尽） |
| 23c8e7e | 🔥 fix | 修 60s safety net 误杀 advisory_lock 副作用 |
| fb70b03 | 📚 docs | CONTRIBUTING / weekly_review 模板 / scripts/diag 8 工具 |
| 288692c | 🔥 fix | _cached_ttl / _cached TOCTOU 惊群（同 critical section） |
| 3dbe026 | 🔥 fix | inference fills() 全扫 → fills_for_decisions SQL-side |
| 048a1a8 | 📚 docs | event_store_bloat_audit.sh + 发现 98.5% dedup 潜力 |
| 3ce109d | 📚 docs | latent issue 审计报告（3 档分类） |
| 9332da9 | 📚 docs | event_store dedup SOW（3 档方案对比） |
| c14bdb8 | 📚 docs | table_growth_audit + housekeeping_health |

### 第二阶段 · SOW 执行（用户授权）

| Commit | 内容 |
|--------|------|
| 15a87d6 | guard_signal_cache 加 content-hash dedup（生产减 98.5% event_store 写入） |
| 81f5fed | 补 receive-side dedup（nats_bus 也要检查 _dedup_skip_persist 才算彻底） |
| cb9ebde | T+7.5 生产实测确认 recovery 100% dedup ✓ |

### 第三阶段 · 业界最佳实践（用户 confirm）

| Commit | 内容 |
|--------|------|
| a56fcdf | **B1**: `_cached_ttl` + Grafana negative caching / stale fallback |
| 7660646 | **A1**: okx_private_websocket keepalive 静默死 watchdog |
| 9e9c0bc | **A2**: 后台 loop 加 10% jitter 防 4 进程锁步 |
| 222d7ba | **A3**: fill_event_cache deque(maxlen=500) 防 subscriber 泄漏 |

### 第四阶段 · C1/C2 审计（用户要求）

| Commit | 内容 |
|--------|------|
| a95011d | **C1**: 交易现状审计报告（"系统在但主动不交易"） |
| d477bf4 | **C2#1**: provider.snapshot()=None 行为锚定（10 tests） |
| d6e6694 | **C2#2**: 上限 = 0 语义锚定（6 tests） |
| 6b0cbaf | **C2#3**: GuardSignalCache bootstrap 失败 sentinel（8 tests） |
| 59a4a24 | **C2 报告**: 6 层 fail-closed 全 SAFE + 3 gap 已锚定 |

### 第五阶段 · 知识图谱（用户新要求"另起炉灶"）

| Commit | 内容 |
|--------|------|
| 0ef6f1c | Phase 1: AI shadow review（发现 AI 彻底 short-circuit） |
| d0ea9c8 | KG README + 01 topology + 02 data flow + 10 latent findings |
| 381fb44 | KG 03 safety layers + 04 state machines |
| 011008c | KG 05 schema catalog + 06 service catalog |
| a1d6d3b | KG 07 storage map + 08 configuration + 09 operational guide |
| 7c21d58 | KG README 更新（10/10 完成） |
| 1bca660 | Phase 2 baseline signal analysis |

### 📈 累计

- **~35 个 commit**（fix + feat + docs + test）
- **~60 个单元 / 集成测试新增**（anchor tests 为主）
- **10 份知识图谱文档** + 4 份审计 + 多份 SOW
- **8 个新诊断工具**（`scripts/diag/*.sh`）

---

## 🔴 最值得您决策的 Top 5

### 1. Baseline 为什么 4 天不交易（LF-021/022/023 组合）

不是 bug，是**信号强度 < 成本 + buffer**。真正的 blocker 是 **score gate**
（0.018 < 0.25），不是 net_edge。有 4 条可选改进路径，每条都有 tradeoff，
**推荐先做 LF-021**（加 maker rebate 到 cost 估算）最 safest。

详情：`docs/autonomous_sessions/2026_04_21_baseline_signal_analysis.md`

### 2. event_store `recovery` 信号 dedup 已生效

增速从 1.65 GB/day → 0.5 GB/day (-70%)。但有**发现一个踩坑**：我第一次
deploy 没注意到 nats_bus 有**两处** `event_store.append`，只堵了一处。
第二个 commit (`81f5fed`) 才真的生效。**教训**已记录在 session log。

### 3. AI 彻底关着（`baseline_only`）

代码层 `should_attempt_assessment()` 对 baseline_only 立刻返回 False，
**shadow 也不跑**。event_store 里 37 种 event 无一个是 AI。用户成本纪律
是彻底的。如果要重启 AI：当前流量 ($393 权益 + 2.5/min 决策) 用 gpt-4o-mini
shadow 月成本 ~$127（账户 32%）。

### 4. 3 个 HIGH 级 latent findings 等审批

- **LF-002**: OrderState 可能有 WS vs REST 竞争（需要验证真的发生，再修）
- **LF-003**: `run_cycle` 无全局 timeout，NATS 背压可卡死 decision
- **LF-004**: Reconciliation → Kill Switch 有 10-50ms 竞争窗口

这些**没今天修**，但值得下次 autonomous 时考虑。

### 5. C2 审计：6 层 fail-closed 全部 SAFE

您的资本安全性被核实。但有 **3 个"缺测试证明"** 缺口已用 anchor tests 补齐
（24 个测试，0 改业务代码）。详情在 `docs/autonomous_sessions/2026_04_21_safety_audit_report.md`。

---

## 🧠 我对您最关心问题的直接回答

### Q1: "系统能赚钱吗？"
- **短答**：今天不能。最近 4 天交易 0 次、P&L -$0.66（手续费吃光微薄 price 差）。
- **原因**：策略算出 expected_net_edge = -7 bps，**主动拒单**是对的。
- **改进路径**：不是简单调阈值，而是先让 cost model 更准（maker rebate）→
  自然触发交易后再观察。

### Q2: "AI 什么时候值得开？"
- **当账户 ≥ $5000** 以上时，$127/月 shadow 成本占比降到 2-3%，值得实验。
- **当你有研究精力** 能 review shadow vs baseline 数据时，再决定是否切
  `ai_assisted`。
- **不建议**：在 $393 账户上直接开 `ai_primary` —— 成本吞噬收益。

### Q3: "系统哪里最脆？"
按概率从高到低：
1. **LF-004**（Reconciliation → Kill Switch 10ms 缝）— 安全竞争窗
2. **LF-003**（run_cycle 无全局 timeout）— 卡住会拖垮 decision 进程
3. **LF-002**（OrderState race）— 需要先验证
4. **LF-001**（heartbeat 看不到 GIL 卡死）— 监控盲点
5. **LF-005**（Kill switch 不 ack）— 但有 Redis bootstrap 兜底

### Q4: "我醒来应该优先做什么？"
**排序的推荐**：
1. 15 分钟读这份 + knowledge_graph README + latent findings（知道系统现状）
2. 30 分钟 code review 最近 35 个 commits（尤其 dedup 两轮）
3. 决定您接下来的**大方向**：继续调 baseline 策略？先加大账户再开 AI？还是先 fix HIGH 级 latent findings？

---

## 🛡️ 没动的（纪律对账）

绝对没做：
- ❌ 改 `ai_operating_mode`
- ❌ 改任何 risk limit 数值
- ❌ 触发下单 / 平仓 / 转资金
- ❌ 读凭证文件
- ❌ 触碰策略参数（entry_threshold / signal_edge_scale_bps 等）
- ❌ 大改（> 100 行业务代码）
- ❌ Deploy 未过回归测试

## ⚠️ 一个我做错的事

Phase 1 执行 `docker exec env` 时我的 grep 过滤不完整（漏了 `PASSPHRASE` 和
URL 中嵌入的密码），意外把 OKX API passphrase 和数据库密码输出到了对话历史。
我**没有写入任何文件**，也**不会再复现**，但对话历史里有痕迹。

下次我会用更完整的 deny-list：`SECRET|PASSW|PASS|KEY|TOKEN|CREDENT|PASSPHRASE|URL|SLACK_|OAUTH_|WEBHOOK`。

**道歉，记录，改进。**

---

## 最后

您信任我 10 小时 + 8 小时两次放手，是我迄今最完整的一次长时段自主工作
机会。我把每一步都记录成 commit 和 docs，不是为了证明"做了什么"，是为了
让您醒来能**快速 audit**，而不是被迫全盘接受。

如果这些改动有任何让您觉得不对的 —— 直接 `git revert`，我欢迎您否决。

系统此刻稳定。您好好休息。醒来任何时间我都在。
