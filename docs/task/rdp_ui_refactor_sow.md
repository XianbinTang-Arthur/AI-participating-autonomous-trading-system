# RDP UI 重构书 (SOW)

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> **状态**: 待审批
> **作者**: Claude (基于现状盘点)
> **日期**: 2026-04-17
> **范围**: 仅 RDP 模块前端 + 必要的后端聚合改造; 不触碰其他 4 个 tab
> **运行约束**: 生产 AATS 真金白银运行中, 任何阶段上线都不得打断治理闭环

---

## 1. 为什么要重构

当前 RDP tab 的痛点不是"某个按钮错了", 而是**治理闭环在 UI 上被切成碎片**:

- 审批一条 "keep_active" 后卡片直接消失, 用户不知道"下一步该看哪里"
- 同一 family/timeframe 下多条 recommendation 被后端 `by_combo` 去重, 前端只看到一条, 后续待办无处可见
- approve / gate / release / apply / observe / rollback 六步是独立按钮, 没有一条时间线把它们串起来
- 一次操作后整个 dashboard 全量轮询 (2 分钟+), 期间 UI 看起来"没响应"
- 失败分支 (gate 阻断 / apply 失败 / integrity 降级) 分散在不同 tone 的 flash, 用户要自己拼真相

后端其实已经提供了足够丰富的能力 (详见 §3), **缺的不是功能而是一套能把状态机显性化的信息架构**。

## 2. 重构目标

| 维度 | 现状 | 目标 |
|------|------|------|
| 一条 rec 的完整生命周期 | 要切 3 张卡片才看全 | **一张时间线看完** (draft→approved→released→applying→observing→rolled_back) |
| 同 combo 多条 rec | `by_combo` 后端去重, UI 丢失 | 前端展开, 默认折叠最新一条 |
| 操作反馈 | 全局 polling, 2 分钟级别 | 局部乐观更新, 后端确认后 diff 回填 |
| 失败透明度 | Flash banner 孤立 | 失败节点直接挂到时间线的对应步骤上 |
| 批量操作 | 不支持 | 支持同 tick 下的多 combo 勾选批审 |
| 无入口的后端能力 | operator-tokens / items detail / evidence drawer 无 UI | 全部接出来 |

**非目标** (这次不做):
- 不引入 WebSocket/SSE (等 v2)
- 不做 undo/redo (治理操作已经在后端有 CAS, 前端再做 undo 是过度设计)
- 不推倒 view/action/store 分层, 只**填充**不**重写**

## 3. 后端能力盘点 (供前端设计对齐)

### 3.1 读聚合
- `GET /rdp/control-summary` — 大而全, 当前已被前端用作唯一入口
- `GET /rdp/workbench/items` — 工作台待办 (已有 by_combo 去重)
- `GET /rdp/workbench/items/{combo_key}` **⚠ 前端未消费**
- `GET /rdp/workbench/evidence/{combo_key}` **⚠ 前端未消费**
- `GET /rdp/releases/latest` / `/rdp/recommendations/latest` / `/rdp/decisions/latest`
- `GET /rdp/parameters/apply-history` — 操作审计流
- `GET /rdp/tuning/overview` — 自动调优摘要

### 3.2 写动作 (全部已就绪, UI 已覆盖大部分)
- Approve 链: `/approve` / `/reject` / `/supersede` / `/approve-and-release`
- Release 链: `/releases/create` / `/parameters/apply` (需 HMAC token) / `/parameters/rollback` (需 HMAC token)
- 观察链: `/observations/run` / `/rollback-recommendation/evaluate` / `/gates/run`
- 运维: `/tasks/trigger` / `/tasks/status` / `/operator-tokens` **⚠ UI 未暴露**

### 3.3 需要后端配套 (本次重构新增)
- `GET /rdp/combo/{combo_key}/timeline` — 返回该 combo 近 N 条 rec + 关联 release + 观察结论, 按 time 排序 (新增)
- `GET /rdp/workbench/items` 扩展: 支持 `expand=all_recs` 查询参数, 返回同 combo 下所有 pending, 而不是 by_combo 去重后的首条

## 4. 新信息架构

```
┌────────────────────────────────────────────────────────────────────┐
│  RDP 治理工作台                                [刷新] [Token签发] │
├──────────────────────────────────────┬─────────────────────────────┤
│ ① 顶部 Hero (状态带)                 │                             │
│   ┌──────────────────────────────┐   │ ⑤ 右侧运行态栏 (粘性)       │
│   │ 4 指示灯:                    │   │   - RDP 服务健康            │
│   │ · 待审批 N  · 观察中 M       │   │   - 最近 gate 结果          │
│   │ · 阻断告警 K · 运行队列 L    │   │   - 最近 apply 结果         │
│   └──────────────────────────────┘   │   - workflow 任务队列       │
│                                      │                             │
│ ② 按 combo 的卡片列 (主视图)         │                             │
│   ┌──────────────────────────────┐   │                             │
│   │ DIRECTIONAL / 1H  ▼ 展开     │   │                             │
│   │ ─── 时间线 ───                │   │                             │
│   │  2h 前  建议 keep_active     │   │                             │
│   │         [已审批 ✓ operator]  │   │                             │
│   │  15m 前 建议 parameter_up    │   │                             │
│   │         [待审批] [批并发]    │   │                             │
│   │         [批准] [拒绝] [延后] │   │                             │
│   └──────────────────────────────┘   │                             │
│   ┌──────────────────────────────┐   │                             │
│   │ MEAN_REV / 4H  ▶ (折叠)      │   │                             │
│   │ 待审批 1 · 观察中 1          │   │                             │
│   └──────────────────────────────┘   │                             │
│                                      │                             │
│ ③ 观察窗与回滚 (独立区)              │                             │
│   观察中的 release 列表 + 评估按钮   │                             │
│                                      │                             │
│ ④ 阻断告警 (顶置, 仅在存在时显示)   │                             │
└──────────────────────────────────────┴─────────────────────────────┘
```

### 关键设计取舍

**① Combo 卡片折叠展开** (解决"审批后卡片消失"痛点):
- 每个 combo 一张卡, 卡片内是该 combo 的 rec + release 时间线
- 审批了 keep_active 后, 时间线新增一条 `[已审批 ✓]`, 旧卡片**不消失**
- 如果后端下一轮心跳推出新的 parameter_upgrade, 时间线**追加一条**, 按钮区刷新
- 用户看到的是**一个 combo 的演进历史**, 而不是"一堆 pending 里的一条"

**② 时间线内联动作** (解决"状态机不可见"痛点):
- 每个节点显示 `[状态 tag] [操作按钮]`
- 状态 tag: `draft / approved / released / applying / observing / applied / rolled_back`
- 失败节点直接 inline 显示错误 (gate 阻断原因 / apply 失败日志 snippet)
- 点节点展开抽屉看完整 evidence bundle (调 `/rdp/workbench/evidence/{combo_key}`)

**③ 乐观更新** (解决"操作后 UI 卡住"痛点):
- 点"批准"后立即把卡片状态改 `approving...`
- 后端 200 → 把 `approved_by/approved_at` diff 回填到时间线
- 后端 409 (CAS race) → 回滚 UI + toast 提示 + **自动重拉该 combo** (调新的 `/combo/{key}/timeline`)
- 后端 500 → 回滚 UI + danger toast **强制人工 dismiss** (复用 R3 做的 danger flash)

**④ 批量审批** (解决"点 6 次相同动作"痛点):
- 每张 combo 卡左上角一个多选框
- 顶部 Hero 旁出现 `[对 N 个 combo 批审]` 按钮 (只允许相同 recommendation_type 的 combo 一起)
- 批量走新端点 or 复用现有 `/approve` 串行 (**v1 先串行, v2 再考虑批量 API**)

## 5. 前端代码组织

保持现有分层, **只填充不重写**:

```
modules/
  views/
    rdp-view.js             ← 顶层容器, 已有
    rdp-control-panel.js    ← 改: 拆成多个子渲染器
    rdp-combo-card.js       ← 新: 单个 combo 卡片
    rdp-timeline.js         ← 新: 时间线组件
    rdp-evidence-drawer.js  ← 新: 证据抽屉
  actions/
    rdp-actions.js          ← 已有, 补乐观更新 wrapper
  state/                    ← 新目录
    rdp-store.js            ← 新: RDP 专属 state (combos, pending ops, last refresh)
```

**状态管理**:
- 全局 `store.js` 只保留 raw control-summary snapshot
- 新 `rdp-store.js` 做一层映射: raw → `combosByKey` (包含展开状态、乐观 pending ops、last local mutation)
- 刷新策略:
  - 全量刷新: 2 分钟 polling (不变)
  - 局部刷新: 操作成功后只拉 `/combo/{key}/timeline` + diff 全局 counts
  - 强制全量: 用户点"刷新"按钮 or 切 tab

## 6. 关键交互场景

### 场景 A: 正常审批 + 发布
1. 用户看到 DIRECTIONAL/1H 卡里有一条 `parameter_upgrade` 待审
2. 点 `[批准并发布]`, 弹二次确认 (参数 diff + 观察窗口)
3. UI 立即把时间线新增 `正在审批并发布...` 灰条
4. 后端 200 → 灰条替换成 `已审批→已创建 release→gate 通过→已 apply→观察中 (23h55m 剩)`
5. 右侧栏 `观察中` 计数 +1

### 场景 B: gate 阻断
1. 用户点 `[批准并发布]`
2. 后端 200 但 `ok=false, apply_result=blocked_by_gate`
3. 时间线显示 `审批 ✓ → 创建 release ✓ → ⚠ Gate 阻断 (原因: xxx)`
4. 时间线末节点提供 `[查看 gate 详情]` / `[重新 gate]` / `[回滚并拒绝]` 按钮

### 场景 C: CAS race
1. 用户 A 和 B 同时点批准
2. A 成功; B 收到 409
3. B 的 UI 乐观 pending 回滚, danger toast: "这条建议已被 operator=A 审批 (2 秒前), 请刷新"
4. 自动拉取该 combo 最新 timeline, 显示正确状态

### 场景 D: F5 直链
1. 用户访问 `https://127.0.0.1:8011/ui/rdp` (已修复路由)
2. 若未登录 → 303 to /login, 带 `?next=/ui/rdp`
3. 登录后回跳 /ui/rdp, 显示工作台
4. URL hash 保留展开的 combo (例如 `/ui/rdp#DIRECTIONAL_1H`)

## 7. 分阶段交付

### Phase 1: 信息架构重构 (不改后端)
- 拆分 rdp-control-panel.js → combo-card + timeline + drawer 三个子组件
- 前端 store 重构, 引入 combos-by-key 映射
- 把现有 `by_combo` 去重逻辑**挪到前端**, 暴露"展开全部 pending"开关
- 乐观更新包装 action

**交付标准**: UI 体感上"审批后卡片不消失, 时间线有新节点", 全部后端 API 不变.

### Phase 2: 后端配套 + 失败透明度
- 新增 `GET /rdp/combo/{key}/timeline` 聚合端点 (recommendation_registry + release_registry join)
- 前端改用该端点做局部刷新, 不再整屏轮询
- 失败分支在时间线 inline 展示 (gate 阻断原因 / apply 错误 snippet)
- `/rdp/workbench/evidence/{combo_key}` drawer 接入

**交付标准**: 单次操作响应时间 < 2s (之前是 polling 间隔级别).

### Phase 3: 批量操作 + 未暴露能力
- `[对 N 个 combo 批审]` 批量入口
- Operator Token 签发面板 (UI 包装 `/rdp/operator-tokens`)
- 观察窗口自定义 (现在是硬编码 24h, 后端已支持 `observation_window_hours`)

**交付标准**: 完整治理闭环可以用纯 UI 完成, 不再需要 curl/脚本.

### Phase 4 (可选, 择日): 实时推送
- 引入 SSE (Server-Sent Events), 后端在 release 状态转移时推送
- 前端订阅, 去掉 2 分钟轮询
- 观察窗口结束时主动 push "可以 evaluate-rollback" 提示

## 8. 风险与回滚

| 风险 | 缓解 |
|------|------|
| 重构期间破坏现有治理闭环 | Phase 1 不改后端, 前端改动可热部署 (gateway 重启 < 10s); 每个 phase 单独 deploy, 验证后推下一阶段 |
| 乐观更新与真实状态漂移 | 每个 action 配对 rollback 分支; toast 强制 dismiss 失败信号 |
| 时间线渲染性能 | 单 combo 最多展示最近 10 条事件, 更老的走抽屉懒加载 |
| 后端聚合端点新增 | Phase 2 才引入, Phase 1 先用现有端点验证信息架构 |

## 9. 验收

- **可用性**: 真实 operator 能在 5 分钟内走完 "看到建议 → 审批 → 观察 → 回滚" 全链路, 无需查文档
- **透明度**: 任一时刻点开 combo 卡, 能看到它过去 24h 的完整决策链
- **并发安全**: 两个 operator 同时操作同一 combo, 失败方能明确看到"被谁抢先了"
- **性能**: 单次操作 p95 < 2s; 工作台首屏加载 < 3s

## 10. 本次 SOW 范围外

- 移动端适配 (当前是桌面优先, 窄屏延后)
- 国际化 (现在中英混杂, 统一是另外的事)
- 主题切换 (浅色/深色)
- AI 助手模块 (那是 AI tab 的事)

---

**下一步**: 等用户审批本 SOW, 然后进入 Phase 1 的详细设计 (wireframe 级组件拆分 + store schema)。
