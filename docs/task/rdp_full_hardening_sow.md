# RDP 全面硬化 SOW（统一工作说明）

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> **文档状态**：待审批（2026-04-17 起草）
> **备份基线**：git tag `pre-rdp-hardening-v1`（5cbf836）
> **目标环境**：模拟盘（paper-trading）衍生品直接上线，不经独立 staging 演练
> **主责人**：XianbinTang-Arthur + Claude Code
> **相关诊断**：本 SOW 建立在两份独立审查之上（用户审查 + Claude 审查），合并暴露问题共 8 组 / 23 条
> **接替关系**：本 SOW 是 `docs/task/rdp_production_hardening_plan.md` 的继任版本，含后者全部 10 项止血任务并扩展为三批次工程

---

## 1. 背景与问题陈述

AATS 项目的 RDP（Research Data Platform）设计目标是：**自动化研究产出 → 人工审批 → 受控发布 → 持续观察 → 快速回滚** 的闭环治理系统。实盘上线前，用户要求 RDP 能对生产参数变更提供"可审计、可追溯、可快速回滚"的强约束。

两份审查（用户 + Claude）独立指出 RDP 未达设计目标，核心病根不是单点 bug，而是**四层同时塌陷**：

| 层次 | 问题 | 代表证据 |
|------|------|---------|
| **入口层** | 生产参数变更有多条旁路，rollback 可注入，脚本无认证 | `scripts/apply_active_parameter_set.py:295-357` `apply-frozen` 动作；`scripts/rdp_*.py` 全部零认证；rollback 目标从本地 JSON 读 |
| **真源层** | db-first 名存实亡，文件/DB 双真源，DB 失败自动降级到 JSON 成功 | `recommendation_registry.py:384-408`；`release_registry.py:139`；`active_decision_registry.py` 同款；`evidence_bundle_index.py` 同款 |
| **DB 约束层** | 核心表大面积缺 FK/UQ/CHECK，业务不变量靠代码盲信 | `active_parameter_sets.parameter_set_id` 无 FK；`recommendations` 无 `UQ(round+combo)`；各表 status/severity 字段无 CHECK |
| **反馈层** | observation/rollback/effectiveness 读研究 artifacts 不读 live facts；跨进程参数下发机制根本不存在 | `observation_window.py:312-316`；`rollback_policy.py:238`；`release_effectiveness.py:281`；decision/execution 进程启动读一次 DB 后内存冻结 |

**结论**：RDP 目前是"骨架齐全、闭环未合"的半成品，不能驱动实盘参数变更。

---

## 2. 工程目标（完成后的硬约束）

完成本 SOW 全部三个批次后，RDP 必须同时满足：

1. **单一入口**：除 `/rdp/*` API 经 `pre_apply_gate` + 受限 `rollback` 之外，**没有任何代码路径能写 `active_parameter_sets`**
2. **单一真源**：所有治理状态以 PostgreSQL `governance.*` schema 为权威，文件仅为审计副本；DB 不可用时系统返回显式错误而非"JSON 成功"
3. **DB 约束兜底**：核心业务不变量由 FK/UQ/CHECK 在 DB 层强制，代码层校验作为第二道防线
4. **实盘反馈闭环**：observation/rollback/effectiveness 的判定输入来自 `execution_orders` / `execution_fills` / `strategy_sleeve_intents` / `reconciliation_events` 等 live 表
5. **跨进程参数一致**：RDP apply 成功 → NATS 事件 → decision/execution 热重载，`version epoch` 保证单调推进；观察窗口在所有消费者达到目标 version 后才开始计时
6. **健康证据充分**：daemon healthy = (daemon 心跳新鲜) AND (当前 task 心跳新鲜) AND (task 进度游标推进)
7. **测试可信**：任何 mock DB 的单测必须配对 testcontainers 集成测试；CI 守门禁止新 PR 引入 mock-only 的治理路径测试

---

## 3. 用户对 6 个关键决策点的最终选择

（2026-04-17 确认，本 SOW 全部基于此展开）

| # | 决策点 | 用户选择 | 影响范围 |
|---|--------|---------|---------|
| 1 | `apply-frozen` 动作处理 | **完全删除（严格）** | 批次 A |
| 2 | 脚本认证方案 | **直接禁用脚本只留 API（最严）** | 批次 A |
| 3 | `RDP_PRODUCTION_APPLY_ENABLED` flag | **废弃改成绑定操作员身份的 short-lived token** | 批次 A |
| 4 | 跨进程下发通道 | **用现有 NATS；消费端 reload 失败直接 kill 进程** | 批次 B |
| 5 | 批次 A 灰度 | **模拟盘衍生品环境直接上线，不经独立 staging** | 批次 A |
| 6 | DB 迁移窗口 | **允许短暂只读**（加 FK 期间） | 批次 A |

---

## 4. 三批次总体计划

### 批次 A —— 止血 + DB 硬化（工期 3-5 天）

**目标**：切断所有可绕过治理改 live 参数的路径；在 DB 层强制业务不变量。

**范围**（7 项）：

- A-0.1 Rollback 目标校验收口：从 DB 读（非 JSON），强校验"属于该 combo 已批准历史 lineage"
- A-0.2 Legacy 脚本全面禁用：所有写类 `rdp_*.py` 脚本改为打印"已迁移到 API"并 exit 2
- A-0.3 清扫"DB 失败 → JSON 成功"反模式：4 处一次性处理
- A-0.4 Gate ISO 时间解析统一：抽 `_parse_iso_datetime_utc()` 到公共模块
- A-0.5 `RDP_PRODUCTION_APPLY_ENABLED` 废弃，换成 short-lived token 签发机制
- A-0.6 `apply-frozen` 动作物理删除
- A-1 DB schema 硬化迁移（7 条 DDL + 一套数据清理 SQL）

**批次 A 完成后的硬约束**（验收标准见 §6.1）：
- `grep -r "active_parameter_sets.*update\|insert" aats/ scripts/` 后，写入点只剩 `aats/data_platform/decision_system/active_parameter_apply.py` 的受控函数
- DB mock 断开时，approval / release / rollback API 全部返回显式 5xx 错误而非 200
- 新脚本入口企图 exit 2，提示走 API

### 批次 B —— 实盘反馈闭环 + 跨进程热切换（工期 2-3 周）

**目标**：observation/rollback/effectiveness 基于 live facts；decision/execution 进程能热重载参数并保持跨进程一致。

**范围**（6 项）：

- B-2.0 **跨进程参数热切换机制**（最关键前置）：
  - `active_parameter_sets` 加 `version BIGINT NOT NULL` 列
  - RDP apply 成功后发 NATS `aats.parameters.updated` 事件
  - decision/execution 订阅该事件，从 DB 重加载，reload 失败直接 kill 进程（由 Docker restart policy 接手）
  - `/reload-parameters` 管理入口（观察窗口启动前 RDP 主动调用并验证所有消费者 version 匹配）
- B-2.1 `live_facts/release_window.py` 新模块：统一提供 release window 内的 live decision / execution / reconciliation 事实
- B-2.2 `observation_window.py` 重写：输入 `release_id + 时间窗`，输出基于 live facts 的 `live_metrics_delta`
- B-2.3 `rollback_policy.py` 重写：触发条件基于 `live_metrics_delta`
- B-2.4 `release_effectiveness.py` 重写：评分基于实现 vs 预估偏差
- B-2.5 Health 证据强化：task 级 heartbeat 独立 + 进度游标 + workflow 超时

### 批次 C —— 研究链契约 + 证据校准 + 摄取层硬伤（工期 2-3 周，部分可与 B 并行）

**目标**：让研究链真正连通；让证据可信；让摄取层不再静默故障。

**范围**（12 项）：

- C-3.1 Placeholder 参数映射清点与补齐（`directional_trend_weight` 等）
- C-3.2 Phase 2→3 数据流契约：`ReplayDecision` → `SleeveFact` 适配器
- C-3.3 Phase 4→6 evidence_bundle schema 标准化
- C-3.4 `experiment_registry` 增加 `git_commit` / `adapter_version` / `gold_dataset_version`
- C-3.5 测试方法学整改 + CI 守门
- C-4.1 Slippage estimator 历史校准管道（从 live execution_fills）
- C-4.2 核心 metrics 补齐（Sharpe / Max DD / Alpha decay / 实现-预估滑点差）
- C-4.3 Fill feasibility / execution cost 补 OKX 特性（funding 结算 / 强平惩罚 / taker rebate）
- C-5.1 Checkpoint + finish_run_item 原子化
- C-5.2 API 分页中间持久化
- C-5.3 Quality flags 持久化到 Bronze
- C-5.4 Symbol mapper 改配置驱动
- C-5.5 两个 live_query_adapter 合并

### 批次先后依赖图

```
批次 A ──────┐
             ├──→ 批次 B 必须在 A 之后（依赖 DB FK 与单一入口）
批次 C.1-C.3 │  ← 可与 B 并行
批次 C.4-C.5 ─┘  ← 独立
```

**重要纪律**：**批次 A 完成前禁止开启实盘 auto-apply**；**批次 B 完成前维持"改参数必须重启 4 进程"的现状**；**批次 B 完成前不做任何 observation 相关自动化**。

---

## 5. 关键文档交付物

本 SOW 是总纲。每批次启动前必须交付对应详细设计：

| 文档 | 状态 | 路径 |
|------|------|------|
| 本 SOW | 待审批 | `docs/task/rdp_full_hardening_sow.md` |
| 批次 A 详细设计 | 待审批 | `docs/task/rdp_hardening_batch_a_detailed_design.md` |
| 批次 B 详细设计 | 待起草（A 完成后） | `docs/task/rdp_hardening_batch_b_detailed_design.md` |
| 批次 C 详细设计 | 待起草（B 启动后） | `docs/task/rdp_hardening_batch_c_detailed_design.md` |
| 每批次收尾报告 | 待起草（各批次结束） | `docs/task/rdp_hardening_batch_{a,b,c}_closure.md` |

批次 B/C 详细设计在前一批次收尾后再起草——避免过早细化在现实中变成空转。

---

## 6. 验收标准

### 6.1 批次 A 验收

所有条目必须同时满足：

- **入口收口**：
  - [ ] `grep -rn "active_parameter_sets" aats/ scripts/` 的写入点仅限 `active_parameter_apply.py::apply_active_parameter_set` 和 `active_parameter_apply.py::rollback_active_parameter_set`
  - [ ] `scripts/apply_active_parameter_set.py` 全文件改为 exit-2 stub
  - [ ] `scripts/approve_recommendation_and_apply.py` 全文件改为 exit-2 stub
  - [ ] `scripts/rdp_apply_approved_recommendation.py` 全文件改为 exit-2 stub
  - [ ] `scripts/rdp_approve_recommendation.py` 全文件改为 exit-2 stub
  - [ ] `scripts/rdp_rollback_active_parameter_set.py` 全文件改为 exit-2 stub
  - [ ] `scripts/rdp_freeze_parameter_set.py` / `rdp_create_parameter_release.py` / `rdp_run_release_cycle.py` / `rdp_update_decision_registry.py` 同处理
  - [ ] `grep -rn "bypassed_frozen\|apply-frozen\|skip_gate=True" aats/ scripts/` 无命中
- **Rollback 收口**：
  - [ ] `rollback_active_parameter_set` 的 `to_parameter_set_id` 必须通过 `parameter_sets` 表和 `parameter_apply_history` 表双查校验
  - [ ] 不给 `to_parameter_set_id` 时从 `parameter_apply_history` 推导的前值必须走 `SELECT FOR UPDATE`
  - [ ] 单测覆盖：绕过 recommendation 的直传 id、已 deprecated 的 id、跨 family 的 id、不存在的 id（4 个用例）
- **DB 降级收口**：
  - [ ] `_db_update_rec_status`、`_db_update_release_status`、`_db_update_active_decision`、`_db_update_evidence_bundle` 在 DB 不可用时返回显式错误类型（非 None）
  - [ ] 调用方全部转为"错误则整体失败"，不再写 JSON 标记成功
  - [ ] 单测覆盖：mock DB 断开 → API 必须返回 5xx，JSON 文件保持不变
- **时区统一**：
  - [ ] 新增 `aats/data_platform/governance/_time_util.py::parse_iso_datetime_utc`
  - [ ] `gate_rules.py:111` / `gate_runtime_contract.py::_parse_iso_datetime` / 其他 ISO 解析点全部改用新工具
  - [ ] `grep -rn "_parse_iso_datetime\|fromisoformat" aats/data_platform/` 确认无遗漏
- **认证收口**：
  - [ ] `RDP_PRODUCTION_APPLY_ENABLED` 从环境变量中完全删除
  - [ ] 新增 `aats/api/rdp_apply_token.py`：签发/校验 short-lived (TTL 300s) HMAC token
  - [ ] API apply / rollback endpoint 要求 header `X-Rdp-Apply-Token`，未附 token 返回 403
- **DB 硬化**：
  - [ ] 7 条迁移 DDL 全部生产执行完成（详见批次 A 详设 §4）
  - [ ] 迁移前数据清理 SQL 产出的"孤儿记录报表"为空或用户逐条 ack
  - [ ] 迁移后 `pg_dump --schema-only` 对比预期（含 FK 名、UQ 名、CHECK 名）完全一致

### 6.2 批次 B 验收

（详见批次 B 详细设计。核心硬约束：构造一条测试 release，插入不利 live execution_fill，observation 必须触发 `rollback_recommended`；仅改研究摘要不应触发。）

### 6.3 批次 C 验收

（详见批次 C 详细设计。核心硬约束：回测与 live shadow 模式 Sharpe / slippage 偏差在阈值内；placeholder 参数全部消除。）

---

## 7. 回滚预案

### 7.1 批次 A 回滚（每项独立可回）

| 项 | 回滚方式 |
|----|---------|
| A-0.1 Rollback 校验 | `git revert` 对应 commit；DB 迁移无关 |
| A-0.2 Legacy 脚本禁用 | `git revert`；脚本恢复原功能 |
| A-0.3 DB 降级清扫 | `git revert`；行为回到"DB 失败走 JSON" |
| A-0.4 时区统一 | `git revert`；使用旧的 per-file 解析 |
| A-0.5 Token 机制 | `git revert` + 恢复 `RDP_PRODUCTION_APPLY_ENABLED` env |
| A-0.6 apply-frozen 删除 | `git revert` 恢复动作；需同时 revert A-0.2 |
| A-1 DB 迁移 | 每条 DDL 都必须有配对的 `DROP CONSTRAINT` SQL（详见批次 A 详设 §4.8）|

### 7.2 整体回滚

- **Tag 回滚**：`git reset --hard pre-rdp-hardening-v1`（仅代码）
- **DB 回滚**：执行批次 A 详设 §4.8 的完整 drop constraints SQL
- **回滚后状态**：RDP 回到批次 A 启动前，生产可立即继续旧流程运行

### 7.3 实盘灾难应急

若批次 A 上线后出现生产事故：
1. 第一时间 `docker compose stop aats-gateway` 停止所有写入路径
2. 若因 DB FK 导致 apply/rollback 失败 → 执行 §7.2 DB 回滚 SQL
3. 若因脚本禁用影响运维 → 临时恢复对应脚本的原版本（仅该文件 `git checkout pre-rdp-hardening-v1 -- scripts/<name>.py`）
4. 发布事故 postmortem 后再决定是否重试

---

## 8. 测试策略

### 8.1 批次 A 测试矩阵

| 层级 | 工具 | 覆盖范围 | 最小用例数 |
|------|------|---------|----------|
| 单测 | pytest + unittest.mock | 受控写入函数内部逻辑 | 25 |
| 集成测试 | pytest + testcontainers (PostgreSQL) | DB FK/UQ/CHECK 实际约束、rollback 目标校验走真库、DB 降级清扫 | 15 |
| 冒烟测试 | curl + docker-compose | API token 机制、legacy 脚本 exit 2、API 返回 5xx | 10 |

### 8.2 集成测试纪律（新增，长期执行）

- 任何新 PR 增加 `patch("...db_*")` 的单测必须同时提供 `tests/integration/test_*.py` 覆盖同一路径
- CI 加 grep 守门：新 PR 引入 "`bypassed_`" / "`skip_gate=True`" / "`apply-frozen`" 字符串一律拒绝
- 每批次收尾必须跑一次 WSL2 full 集成测试套件并附报告

### 8.3 实盘灰度纪律

批次 A 上线后至批次 B 完成前：
- 每次 RDP apply 操作必须人工在 4 进程重启完成后才认为"参数生效"
- 每 24h 人工对比一次 `active_parameter_sets` 与 decision 进程内存参数
- 任何偏差立即告警并冻结下一次 apply

---

## 9. 风险与应急

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| DB 迁移时加 FK 发现悬垂记录 | 中 | 迁移失败，需补数据清理 | 迁移前先跑批次 A 详设 §4.1 的 dry-run 清理脚本，输出孤儿记录清单让用户 ack |
| Legacy 脚本仍被外部 crontab 调用 | 低 | exit 2 产生告警 | 批次 A 开工前扫 `crontab -l` 与 `.env.*` 确认无调用；禁用前发 deprecation 通知 |
| Short-lived token 生成机制阻塞紧急运维 | 中 | 需临时 apply 时等待 token | 提供 `aats/api/rdp_apply_token.py::emit_emergency_token` CLI，绑定 operator 身份 + 5min TTL |
| 加 FK 期间 DB 只读（用户已同意）| 已同意 | API 短暂 503 | 选择交易低峰期（北京时间 06:00-07:00 UTC+8），提前 24h 通告；预期窗口 ≤ 5 分钟 |
| NATS 消息丢失导致跨进程 version 不一致（批次 B）| 中 | decision/execution 用旧参数做决策 | B 阶段每次 reload 需在 DB 写入 ACK，RDP 在观察窗口启动前 poll 所有消费者 ACK |
| 测试方法学整改拖累 PR 节奏（批次 C）| 中 | 迭代变慢 | C 阶段逐步收紧 CI 守门，给团队 2 周过渡期 |

---

## 10. 进度追踪

本 SOW 采用三栏状态追踪：

| 批次/阶段 | 状态 | 责任人 | 完成标志 |
|----------|------|--------|---------|
| SOW 审批 | ☐ 待审批 | 用户 | 本文档获批 |
| 批次 A 详设审批 | ☐ 待审批 | 用户 | `rdp_hardening_batch_a_detailed_design.md` 获批 |
| 批次 A 开工 | ☐ 未开工 | Claude | 按详设逐项提交 commit |
| 批次 A 收尾 | ☐ 未完成 | 双方 | 全部 §6.1 验收通过；closure 文档提交 |
| 批次 B 详设 | ☐ 未起草 | Claude | 待 A 收尾后启动 |
| 批次 B 开工/收尾 | ☐ | | |
| 批次 C 详设 | ☐ 未起草 | Claude | 待 B 启动后启动 |
| 批次 C 开工/收尾 | ☐ | | |
| 整体收尾 | ☐ | 双方 | 三批次 closure 齐备 + RDP 达到 §2 全部 7 条硬约束 |

---

## 11. 审批签字

- [ ] 用户（XianbinTang-Arthur）审阅本 SOW
- [ ] 用户审阅批次 A 详细设计
- [ ] 用户明确确认开工

在上述全部三项勾选前，Claude **不得**修改任何生产代码。

---

## 附录 A：相关审查报告归档

本 SOW 的诊断结论来自两份独立审查：

- **用户审查**（2026-04-17 会话内）：5 条 P0 + 2 条 P1，精确到文件:行号
- **Claude 审查**（同会话，两轮深挖）：8 组结构性问题，含 4 个 Agent 并行验证

两份审查交叉覆盖、相互验证，未发现互斥结论；差集是用户发现了 rollback 可注入这一关键漏洞，Claude 发现了跨进程下发机制缺失这一根本问题。合并后的问题清单即本 SOW §1 的 4 层塌陷。

## 附录 B：批次 A 成功后的示意架构

```
         ┌────────────── 操作员 ──────────────┐
         │                                    │
         ▼                                    ▼
    ┌─ API /rdp/* ─────────────────┐    禁用：所有写类 rdp_*.py
    │  - require_write_access      │           ↓
    │  - X-Rdp-Apply-Token         │       exit 2 + 提示
    │  - 环境策略（prod 严）       │
    └──────────────┬───────────────┘
                   │
                   ▼
    ┌─ pre_apply_gate ─────────────┐
    │  - evidence freshness (UTC)  │
    │  - main system health        │
    │  - reconciliation / kill sw  │
    └──────────────┬───────────────┘
                   │
                   ▼
    ┌─ release_registry.create ────┐
    │  - DB 失败 → 5xx，不降级     │
    └──────────────┬───────────────┘
                   │
                   ▼
    ┌─ active_parameter_apply ─────┐
    │  - DB FK 兜底合法性          │
    │  - SELECT FOR UPDATE 排他    │
    │  - rollback 校验 lineage     │
    └──────────────┬───────────────┘
                   │
                   ▼
              governance.*
         （唯一真源，FK/UQ/CHECK 齐备）
                   │
                   ▼
         （批次 B 接管跨进程热切换）
```
