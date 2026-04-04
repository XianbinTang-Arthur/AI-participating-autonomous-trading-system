# RDP 整合 MVP 开工任务书（正式版）

## 1. 任务定位

在《RDP 与主交易系统整合方案任务书（正式版）》之后，**下一个最应该做的任务**不是一次性把全部整合做完，而是先完成一个 **MVP 级整合切片**。

这个切片只做两件最关键的事：

1. **让 RDP 稳定读取主交易系统的 live facts**
2. **让主交易系统支持加载 Active Parameter Set**

也就是说，这个任务是：

> **把“研究平台能给出结论”推进到“主系统能读事实、能吃参数”的最小闭环。**

---

## 2. 为什么这是下一个任务

当前你已经有：

- Phase 1~6 的研发骨架
- 研究 / 归因 / execution realism / governance / decision support 全链条
- 一份完整的系统整合方案

但你还没有真正打通两条最关键的连接：

### 连接 1：RDP 读取主系统事实
如果这条不稳，Phase 3 attribution 只是“能跑 demo”，不是正式整合。

### 连接 2：主系统加载 active parameter set
如果这条没有，Phase 2/5/6 的结论永远只是 artifacts，不会真正影响生产运行。

所以从工程价值和实施顺序上，这就是现在最应该做的切片。

---

## 3. 本任务的目标

本任务完成后，系统应具备以下能力：

1. RDP 可以通过**标准配置**连接主交易系统的只读 DB
2. RDP 可以稳定读取 attribution 所需 live 表
3. 主系统启动时可以从 `configs/active_parameter_sets/` 读取 active parameter set
4. 主系统可把 active parameter set 覆盖到 family/timeframe 参数上
5. 形成最小文档和验证脚本，供后续继续扩 operator 集成和 approval/apply 流程

---

## 4. 本任务只做什么，不做什么

### 4.1 本任务必须做
- live facts 接口与字段契约
- active parameter loader
- strategy 参数注入
- 最小验证脚本
- 最小运行文档

### 4.2 本任务不做
- operator UI 集成
- recommendation approval 流程
- Phase 6 自动应用参数
- orderbook / trades 级 execution realism 升级
- DB backend 化 active registry

---

## 5. 任务拆分为 2 个工作包

---

## 工作包 A：Live Facts 对接

### A.1 目标
正式固化 RDP 对主交易系统 live facts 的读取能力。

### A.2 需要覆盖的 live facts 表

必须覆盖以下表的读取契约：

- `strategy_sleeve_intents`
- `portfolio_allocation_decisions`
- `allocator_budget_snapshots`
- `reconciliation_state_snapshots`
- `strategy_execution_bundles`
- `execution_orders`
- `execution_fills`

### A.3 需要新增/修改的内容

#### 1. RDP 配置项
在 RDP 配置中增加：

- `RDP_LIVE_DATABASE_URL`
- `RDP_LIVE_DB_READONLY`
- `RDP_LIVE_DB_SCHEMA`（如果需要）
- `RDP_LIVE_DB_CONNECT_TIMEOUT_SECONDS`（可选）

#### 2. Live DB 连接模块收口
建议新增：

```text
aats/data_platform/live_facts/
  db.py
  query_adapter.py
  contracts.py
```

职责：

- `db.py`：live DB 只读连接初始化
- `query_adapter.py`：统一读取 attribution 所需表
- `contracts.py`：定义字段契约、最小必需列、时间字段映射

#### 3. 文档
新增：

```text
docs/operations/live_schema_contract_for_rdp.md
```

内容必须写清楚：
- 表名
- 主键 / 关联键
- 时间字段
- family / symbol / timeframe 字段
- 最小必需字段
- nullable 规则
- 与 attribution 模块的关系

#### 4. 验证脚本
新增：

```text
scripts/rdp_check_live_facts_connection.py
```

功能：
- 测试 live DB 连接
- 校验 7 张表是否存在
- 校验最小字段是否存在
- 可输出简单 row count 和最近时间戳

### A.4 输出

- RDP live facts 配置项
- `aats/data_platform/live_facts/`
- `docs/operations/live_schema_contract_for_rdp.md`
- `scripts/rdp_check_live_facts_connection.py`

### A.5 验收标准

1. 在标准环境下能成功连接 live DB
2. 7 张关键表均可读
3. 文档齐全
4. `rdp_run_live_attribution.py` 可在标准配置下不靠临时手动拼接运行

---

## 工作包 B：Active Parameter Set 回灌

### B.1 目标
让主系统真正能“吃到” RDP 产出的参数，而不是停留在 artifact。

### B.2 设计原则
主系统不直接读：
- `parameter_candidates.json`
- `parameter_recommendations.json`
- `recommendation_registry.json`

主系统只读一个概念：

> **active parameter set**

### B.3 需要新增的目录与文件

建议新增：

```text
configs/active_parameter_sets/
  active_parameter_registry.json
```

建议结构：

```json
{
  "generated_at": "2026-04-04T12:00:00Z",
  "active_sets": {
    "independent_15m": {
      "parameter_set_id": "ps_xxx",
      "family": "independent",
      "timeframe": "15m",
      "values": {
        "min_confirm_ticks": 3,
        "signal_edge_scale_bps": 15
      }
    },
    "independent_1h": {
      "parameter_set_id": "ps_xxx",
      "family": "independent",
      "timeframe": "1H",
      "values": {}
    },
    "directional_15m": {
      "parameter_set_id": "ps_xxx",
      "family": "directional",
      "timeframe": "15m",
      "values": {}
    },
    "directional_1h": {
      "parameter_set_id": "ps_xxx",
      "family": "directional",
      "timeframe": "1H",
      "values": {}
    }
  }
}
```

### B.4 需要新增/修改的模块

#### 1. 新增 Active Parameter Loader
新增：

```text
aats/bootstrap/active_parameters.py
```

功能：
- 读取 `configs/active_parameter_sets/active_parameter_registry.json`
- 根据 family + timeframe 返回 active 参数
- 提供默认 fallback
- 提供加载失败时的保护性行为（不中断主系统，但打印清晰 warning）

建议 API：

- `load_active_parameter_registry(path) -> dict`
- `get_active_parameters(registry, family, timeframe) -> dict`
- `merge_active_parameters(base_params, active_params) -> dict`

#### 2. 修改 settings / strategy config glue
需要在配置整合层新增：

- active parameter set 文件路径配置
- active parameter enable 开关

建议配置项：

- `AATS_ACTIVE_PARAMETER_REGISTRY_PATH`
- `AATS_ACTIVE_PARAMETERS_ENABLED=true`

#### 3. 修改 strategy family 参数注入点
需要接入 active parameters 的位置：

- independent family 参数构造入口
- directional family 参数构造入口

要求做到：
- family/timeframe 维度可以覆盖参数
- 覆盖优先级明确
- 未提供字段时 fallback 原配置

### B.5 参数优先级规则

必须明确：

```text
hardcoded defaults
  < strategy profile yaml
  < active parameter set
  < runtime emergency override（若有）
```

### B.6 需要新增的脚本

新增：

```text
scripts/apply_active_parameter_set.py
```

功能：
- 从 `current_parameter_registry.json` 中选择指定 `parameter_set_id`
- 写入 `configs/active_parameter_sets/active_parameter_registry.json`
- 支持：
  - `--parameter-set-id`
  - `--family`
  - `--timeframe`
  - `--dry-run`

注意：
- 第一版只做显式 apply
- 不做 recommendation 自动批准
- 不做自动重启主系统

### B.7 文档

新增：

```text
docs/operations/active_parameter_application.md
```

内容包括：
- active parameter registry 格式
- 如何应用某个 frozen/candidate 参数
- 如何验证主系统读取成功
- 覆盖优先级说明
- 回滚方式

### B.8 输出

- `configs/active_parameter_sets/active_parameter_registry.json`
- `aats/bootstrap/active_parameters.py`
- `scripts/apply_active_parameter_set.py`
- `docs/operations/active_parameter_application.md`

### B.9 验收标准

1. 主系统启动时能读取 active parameter registry
2. 至少 `independent_15m` 可成功覆盖参数
3. family/timeframe 找不到 active set 时不崩溃
4. `apply_active_parameter_set.py` 可成功写入 active registry

---

## 6. 本任务涉及的改动位置

### 6.1 RDP 侧
建议新增：

```text
aats/data_platform/live_facts/
  db.py
  query_adapter.py
  contracts.py
scripts/rdp_check_live_facts_connection.py
docs/operations/live_schema_contract_for_rdp.md
```

### 6.2 主系统侧
建议新增/修改：

```text
aats/bootstrap/active_parameters.py
aats/bootstrap/settings.py
configs/active_parameter_sets/active_parameter_registry.json
scripts/apply_active_parameter_set.py
docs/operations/active_parameter_application.md
```

### 6.3 Strategy family 参数接入点
需要你在实际代码里定位当前 `independent` / `directional` family 参数进入运行时的地方，并在该位置接入 active parameter merge。

---

## 7. 最小实现范围（MVP）

第一版只要求做到：

1. RDP 能用标准配置读 live facts
2. 主系统能加载 active parameter registry
3. `independent_15m` 参数可被覆盖
4. 有验证脚本与说明文档

第一版不要求：

- 4 组 family/timeframe 全部上线
- operator API
- UI 集成
- recommendation approval 工作流
- active parameter registry 数据库存储

---

## 8. 建议实施顺序

### 第 1 步
先做 live facts contract + 检查脚本

### 第 2 步
再做 active parameter registry + loader

### 第 3 步
把 `independent_15m` 先接进去

### 第 4 步
复制模式到：
- `independent_1h`
- `directional_15m`
- `directional_1h`

---

## 9. 风险与注意事项

### 9.1 不要让 active parameter 加载失败阻断主系统启动
第一版应该 fail-soft：
- 打 warning
- fallback 到原始配置

### 9.2 不要直接让主系统读取 Phase 2/5/6 artifacts
主系统只读 active registry。

### 9.3 不要在本任务里混入 approval / registry 审批逻辑
这不是当前切片的目标。

### 9.4 不要让 RDP live facts 读取变成生产系统的写依赖
保持只读。

---

## 10. 验收标准

本任务通过条件：

1. `rdp_check_live_facts_connection.py` 能成功校验 live facts
2. `docs/operations/live_schema_contract_for_rdp.md` 完成
3. `apply_active_parameter_set.py` 能成功写入 active registry
4. 主系统启动能读取 active registry
5. `independent_15m` 的 active 参数能覆盖运行参数
6. 有 `docs/operations/active_parameter_application.md`

---

## 11. 一句话总结

这个 MVP 任务的职责是：

> **先打通 RDP 与主系统之间最关键的两条线：让 RDP 能正式读取 live facts，让主系统能正式加载 active parameter set。**
