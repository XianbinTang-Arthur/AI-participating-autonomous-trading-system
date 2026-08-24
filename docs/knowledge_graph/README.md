# AATS 系统知识图谱

> **历史快照（2026-08-22 核对）**：本目录固定反映 2026-04-21、HEAD `0ef6f1c` 附近的系统，不再承诺“当前”。其中账户数值、运行模式、拓扑、配置、表/Topic/API 和 findings 状态都可能过期。当前入口见 [`docs/README.md`](../README.md) 和 [完整代码审查说明](../code_review/README.md)。

> **原始定位（历史）**：AATS 在 2026-04-21 建立的系统知识图谱。
>
> **建立时间**：2026-04-21 autonomous session
> **维护责任**：发现和实际行为不一致时，**改本知识图谱而不是让它腐烂**。
> 每个文档顶部标注生成时间和基于的 git HEAD，帮助判断是否过时。
>
> **读者**：
> 1. 未来想接手系统的 AI agent（主要读者）
> 2. 想快速了解"这是什么"的用户本人
> 3. 想挖 latent bug / 做架构决策的任何人

---

## 怎么读这份知识图谱

### 最小阅读单元（30 分钟知道系统是什么）

1. [`01_system_topology.md`](01_system_topology.md) — 4 进程 + 基础设施容器 + 为什么这样分
2. [`02_data_flow.md`](02_data_flow.md) — market tick → decision → order → fill 的完整数据路径
3. [`03_safety_layers.md`](03_safety_layers.md) — fail-closed 架构（C2 审计的扩展版）

### 深入学习路径

4. [`04_state_machines.md`](04_state_machines.md) — OrderState / reconciliation / recovery / guard signal 的状态机
5. [`05_schema_catalog.md`](05_schema_catalog.md) — 所有 pydantic schema（"系统的名词"）
6. [`06_service_catalog.md`](06_service_catalog.md) — 所有核心 service（"系统的动词"）
7. [`07_storage_map.md`](07_storage_map.md) — PG tables + Redis keys + NATS topics

### 运维与扩展

8. [`08_configuration.md`](08_configuration.md) — 环境变量、yaml profile、settings taxonomy
9. [`09_operational_guide.md`](09_operational_guide.md) — deploy、监控、debug、诊断工具

### 审计残留

10. [`10_latent_findings.md`](10_latent_findings.md) — 整理过程中发现但没 fix 的可疑模式（等用户审批）

---

## 设计约束（为什么图谱长这样）

### 原则一：**每个文件自包含**

读者可以从任意入口点进入。开头必有 TL;DR（结论先行），中间有上下文，
结尾有"去哪找更多"链接。

### 原则二：**少用 ASCII 图，多用 mermaid**

mermaid 在 VS Code / GitHub / Claude 里都能渲染，比 ASCII art 易维护。
复杂图才用 ASCII，简单关系一律 mermaid。

### 原则三：**标注"是什么"而不是"曾经是什么"**

本图谱反映 **2026-04-21 当前代码**。历史决策放在 commit message 和 git
log，不塞进图谱。写文档时如果只能找到"这是 legacy"，明确标记
`⚠️ LEGACY`，否则默认描述的是**活着**的代码。

### 原则四：**不粉饰**

发现设计有矛盾、代码有坏味道、文档和实际不一致 → 直接写进 10_latent_findings.md。
图谱目的是"让人看懂"，不是"让系统看起来完美"。

### 原则五：**可重跑**

每份 MD 文档开头的"生成于 HEAD=XXXXXXX"可用于**重新核验**。未来 AI
agent 可以重跑相同的 grep / 查询命令确认图谱是否仍然对得上代码。

---

## AATS 是什么（3 行版本）

1. 这是 2026-04-21 的系统快照，不应从其中推断当前账户、仓位或 live 状态。
2. 当前主交易仍按 gateway/market/decision/execution 四个 slice 理解，但完整部署还包括 RDP 和 profile-specific collectors。
3. 当前有效运行模式、策略和参数必须从现场 runtime、数据库和 Settings Provenance 获取。

---

## 当前知识图谱进度（live 更新）

| # | 文件 | 状态 | 最后更新 |
|---|------|------|---------|
| 01 | [system_topology](01_system_topology.md) | ✅ 完成 | 2026-04-21 |
| 02 | [data_flow](02_data_flow.md) | ✅ 完成 | 2026-04-21 |
| 03 | [safety_layers](03_safety_layers.md) | ✅ 完成（基于 C2 审计） | 2026-04-21 |
| 04 | [state_machines](04_state_machines.md) | ✅ 完成 | 2026-04-21 |
| 05 | [schema_catalog](05_schema_catalog.md) | ✅ 完成 | 2026-04-21 |
| 06 | [service_catalog](06_service_catalog.md) | ✅ 完成 | 2026-04-21 |
| 07 | [storage_map](07_storage_map.md) | ✅ 完成 | 2026-04-21 |
| 08 | [configuration](08_configuration.md) | ✅ 完成 | 2026-04-21 |
| 09 | [operational_guide](09_operational_guide.md) | ✅ 完成 | 2026-04-21 |
| 10 | [latent_findings](10_latent_findings.md) | ✅ 20 项 findings 已录 | 2026-04-21 |

---

## 和其他文档的关系

- `docs/project_positioning.md` — 项目战略定位（图谱尊重但不取代）
- `CLAUDE.md` — 操作手册（硬约束 / 禁令，图谱参考）
- `docs/autonomous_sessions/` — 每次 autonomous 迭代的过程日志
- `docs/task/` — 具体 SOW 文档（定位修改）
- `docs/weekly_review/` — 每周复盘（自省）
- `docs/audit/` / `docs/review/` — 历史审计（可能已过时）

**本图谱与上述并存，现仅承担历史解释作用；不再承诺描述当前。**
