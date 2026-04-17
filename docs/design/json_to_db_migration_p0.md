# JSON → DB 迁移设计（P0 三项）

> 范围：把 P0 三项关键状态从"JSON 文件为真源"改为"Postgres 为真源"。JSON 降级为只读导出 / 审计副本或彻底退场。
> 本文件是书面设计，实施必须按这里的阶段顺序落地；任何偏离先改文档再改代码。

## 0. 约束与基线

- 真金白银运行环境（OKX 衍生品实盘），任何 schema / 写路径变更必须可回滚、可观测、可热切换。
- governance DB 连接走 `aats.data_platform.governance._db_util.try_governance_db()`；不可达时行为要在设计里明确（这次大部分情况下要改成"fail loud"，不再静默退化）。
- 所有 schema 变更走 `aats/data_platform/rdp_models.py` 的 ORM `create_all()` + 幂等 `_migrate_*` 函数；**不**新增 `migrations/*.sql`（现工程 `run_migrations()` 不再扫描它们）。
- 单测优先用 `tests/unit/test_operational_state_db.py` 里的 `_FakeSession` 模式（无 testcontainers 依赖），集成验证交给 WSL2 testcontainers 路径。
- 改动顺序：**P0-2 → P0-3 → P0-1**。理由：P0-2 已经有 DB 表，风险最小；P0-3 只是关掉 fallback；P0-1 最复杂，放最后。

---

## 1. P0-2：`pre_apply_gate` 落 DB 单一真源

### 1.1 现状

- 写：[pre_apply_gate.py:263](aats/data_platform/production_workflow/pre_apply_gate.py:263) `_save_gate_result`
  - 先写 JSON：`artifacts/production_workflow/gates/{gate_run_id}/pre_apply_gate_result.json`
  - 再 best-effort 写 DB：`governance.pre_apply_gate_results`（DB 异常 → `log.warning` 吞掉）
  - 再写 Markdown 报告 `pre_apply_gate_report.md`
- 读：
  - 控制台聚合 [rdp_control_summary.py:85](aats/api/rdp_control_summary.py:85) `_load_recent_gate_results`：DB 命中即返回；任何异常 → 扫 JSON 目录
  - 其他读路径：按 `recommendation_id` / `gate_run_id` 查 JSON 目录

### 1.2 目标

- DB 是**唯一真源**。JSON + Markdown 变成默认关闭的"人可读导出"。
- 提供按 `gate_run_id` / `recommendation_id` / `release_id` 的结构化查询 API，完全脱离文件系统。
- DB 写失败必须显性传播到 `run_pre_apply_gate` 调用方（gate 结果为 `error`，不允许 apply）。

### 1.3 Schema 变更（阶段 B）

表 `governance.pre_apply_gate_results` 已存在；只做加列：

| 列 | 类型 | 约束 | 说明 |
|----|------|------|------|
| `release_id` | `VARCHAR(128)` | `NULL` | 本次 gate 被哪个 parameter release 引用；`NULL` 表示尚未被引用 |

加索引：`ix_pre_apply_gate_result_release (release_id, created_at DESC)`。

迁移通过 `_migrate_pre_apply_gate_results(engine)` 幂等实现：
- 表不存在 → 跳过（由 `create_all` 在更前面建出）
- `ALTER TABLE ... ADD COLUMN IF NOT EXISTS release_id VARCHAR(128)`
- `CREATE INDEX IF NOT EXISTS ix_pre_apply_gate_result_release ...`

ORM 端：`PreApplyGateResultModel` 新增 `release_id = Column(String(128), nullable=True)` 与 `Index(...)`。

### 1.4 新增/修改 DB API（阶段 A，已完成）

统一加在 `aats/data_platform/governance/operational_state_db.py`：

- `db_record_gate_result(session, result: dict)` — 门面，委托给 `db_upsert_pre_apply_gate_result`；pre_apply_gate.py 写路径改用这个名字以凸显"落库"语义。
- `db_get_gate_result_by_run_id(session, gate_run_id) -> dict | None`
- `db_get_latest_gate_result(session, *, recommendation_id) -> dict | None` — `ORDER BY created_at DESC LIMIT 1`
- `db_list_gate_results_for_recommendation(session, *, recommendation_id, limit=20)` — 某条 recommendation 的历史
- `db_list_gate_results_for_release(session, *, release_id, limit=20)`
  - 阶段 A（兼容）：先走 `parameter_releases.gate_result_ref` 的 JOIN，不依赖新列
  - 阶段 B 起：优先按 `pre_apply_gate_results.release_id` 直查；旧行 `NULL` 则靠阶段 C backfill 的 JOIN 补
- `db_set_gate_result_release_id(session, *, gate_run_id, release_id) -> bool` — release 创建成功后由 apply flow 回填；`rowcount > 0` 表示命中

`db_upsert_pre_apply_gate_result` 调整：INSERT 包含 `release_id`；`ON CONFLICT DO UPDATE` 用 `release_id = COALESCE(EXCLUDED.release_id, governance.pre_apply_gate_results.release_id)`，保证后续 upsert 不会把已回填的 `release_id` 洗掉为 `NULL`。

### 1.5 写顺序倒置（阶段 C）

`_save_gate_result` 改写：

1. **必写**：`db_record_gate_result`（用 `try_governance_db` → `Session(engine).begin()`），DB 不可达 / 任何异常 → 抛出，不再 `log.warning` 吞掉。
2. 可选：如果 `AATS_P0_GATE_JSON_EXPORT=on`（默认 `off`），再写 JSON 副本 + Markdown 报告到 `artifacts/production_workflow/gates/{gate_run_id}/`。
3. 返回值：阶段 C 过后 `_save_gate_result` 不再一定返回目录路径；无 JSON 导出时返回 `None` 或 `gate_run_id`，调用方不再假设目录存在。

调用方 `run_pre_apply_gate` 捕获 DB 异常 → 把 gate_result 改写为 `gate_status="error"`, `allow_apply=False`, `blocking_reasons=["gate result persistence failed: <reason>"]`，整份结果仍返回给上游（上游 apply flow 靠 `allow_apply=False` 拒绝）。

Release apply 流程成功后调用 `db_set_gate_result_release_id(gate_run_id, release_id)` 回填。

### 1.6 读路径收紧（阶段 D）

- `_load_recent_gate_results`：去掉 JSON 扫描分支。DB 不可达 / 异常 → 向调用方抛出（由控制面 API 返回 503/500 或降级为"gate 模块暂不可用"提示，不伪造数据）。
- 其他基于目录的读取：一律改走 DB API。
- JSON 目录改为"人可读审计副本"，生产系统不再读它。

### 1.7 Feature flag 清单

| 变量 | 缺省 | 效果 |
|------|------|------|
| `AATS_P0_GATE_JSON_EXPORT` | `off` | `on` = 阶段 C 的 JSON + Markdown 副本继续写出 |

（注：没有 `AATS_P0_GATE_DB_REQUIRED` 这种回退开关；阶段 C 起 DB 必写。要回退只能 revert commit。）

### 1.8 回归测试

单测（`tests/unit/test_operational_state_db.py`，已落地 7 项）：

- `test_record_gate_result_is_upsert_by_run_id`
- `test_get_gate_result_by_run_id_hit_and_miss`
- `test_get_latest_gate_result_returns_most_recent_for_recommendation`
- `test_get_latest_gate_result_returns_none_when_no_history`
- `test_list_gate_results_for_recommendation_orders_desc_and_limits`
- `test_list_gate_results_for_release_joins_via_gate_result_ref`
- `test_list_gate_results_for_release_empty_when_release_missing`

阶段 C 新增（待写）：

- `test_run_pre_apply_gate_db_failure_blocks_apply` — 用 monkeypatch 让 `db_record_gate_result` 抛异常，断言返回 `allow_apply=False, gate_status="error"`
- `test_run_pre_apply_gate_json_export_flag` — flag `on` / `off` 分别断言 JSON/MD 是否出现

阶段 D：

- `test_load_recent_gate_results_raises_when_db_down`（对应 `_load_recent_gate_results` 新行为）

集成（WSL2 testcontainers）：

- Postgres 真实执行 `_migrate_pre_apply_gate_results`，断言重复调用幂等
- 一条 gate 流程完整跑：record → get_latest → set_release_id → list_for_release

### 1.9 风险 & 缓解

| 风险 | 缓解 |
|------|------|
| `ON CONFLICT` 把已 backfill 的 release_id 洗掉 | 用 `COALESCE(EXCLUDED.release_id, current)`；新单测覆盖 |
| DB 偶发不可达导致 gate 拒绝 | 由实盘监控 + 告警保障；业务上 gate 拒绝远比"允许 apply 结果丢失"安全 |
| 已有旧 JSON 目录未被新读路径覆盖 | 保留目录与文件，只是不再读；后续可清理脚本删除 |

---

## 2. P0-3：`strategy_tuning_overrides` 运行时读 DB，关文件 fallback

### 2.1 现状

- `save_strategy_tuning_overrides` ([strategy_tuning_registry.py:143](aats/data_platform/operations/strategy_tuning_registry.py:143))：把 `overrides_path(project_root)` JSON 写盘。
- `load_strategy_tuning_overrides` ([strategy_tuning_registry.py:108](aats/data_platform/operations/strategy_tuning_registry.py:108))：DB 成功 → 返回 DB；DB 不可达或异常 → 默默退化读 JSON。
- 上游 `refresh_strategy_tuning_overrides` 从 approved proposals 推导出 `combo_overrides`，仍会写 JSON。
- 消费方：`rdp_control_summary.py` `overrides = load_strategy_tuning_overrides(root)`；决策系统会把 `combo_overrides` 注入 runtime。

### 2.2 目标

- 运行时 `load_strategy_tuning_overrides` **优先读 DB**；DB 抖动（偶发不可达 / 超时 / 异常）→ 降级用进程内 **上一份已知 cached overrides**，返回结果带 `stale=True` 标志让上游可见。
- 只有两种情况真正 fail loud：
  - **cold start**：进程内无 cache 且 DB 不可达 → `raise RuntimeError`
  - **严格模式**：`AATS_P0_TUNING_FAIL_LOUD=on` 时强制关 cache 回退，任何 DB 失败直接抛
- JSON 彻底退出读路径；仅作为"人可读快照"在 `AATS_P0_TUNING_JSON_EXPORT=on` 时由 `refresh_strategy_tuning_overrides` 导出；默认关闭。
- `save_strategy_tuning_overrides` 转为 deprecated shim。
- 设计取舍：短暂 DB 抖动（几秒～几十秒）里把整条 decision loop 打断代价过大；"用上一份 overrides + stale 标志"在实盘里是更合适的降级点。cache 不跨进程共享，每个进程独立持有自己的 last-known 副本——这是可接受的，因为各进程独立驱动自己的决策。

### 2.3 变更清单

- `aats/data_platform/operations/strategy_tuning_registry.py` 加进程内 cache：
  - module-level `_LAST_OVERRIDES_CACHE: dict | None = None`
  - `_cache_overrides(payload: dict) -> None` 只在 DB 读成功后调
  - cache payload 形状：`{"combo_overrides": {...}, "loaded_at": iso8601, "source": "db"}`
- `load_strategy_tuning_overrides(project_root) -> dict`：
  - DB 可达 + 读成功 → `_cache_overrides(result)` → 返回 `{**result, "stale": False}`
  - DB 不可达 / 读失败 + 有 cache + `AATS_P0_TUNING_FAIL_LOUD` off → `log.warning("strategy tuning overrides: DB 抖动，回退到 %s 的 cached 副本", cached["loaded_at"])`，返回 `{**cached, "stale": True}`
  - DB 不可达 / 读失败 + (无 cache 或 `AATS_P0_TUNING_FAIL_LOUD=on`) → `raise RuntimeError(...)`
  - 去掉 `path.exists()` / JSON 读分支
- `refresh_strategy_tuning_overrides`：
  - 计算出 `{"combo_overrides": {...}}` 后，统一走 `db_upsert_strategy_tuning_overrides`（DB 必写）
  - 写 DB 成功后同步刷新 `_LAST_OVERRIDES_CACHE`（refresh 的调用方通常是 gateway / CLI，能立即把"刚写的"作为运行时 cache 的起点）
  - flag `AATS_P0_TUNING_JSON_EXPORT=on` 时再调 `atomic_json_write` 导出 JSON 副本
- `save_strategy_tuning_overrides`：
  - 阶段 A：保留、只写 JSON、emit `DeprecationWarning`
  - 阶段 B：空实现，只在 flag `on` 时写 JSON；否则直接 `return None`
- 消费方（`rdp_control_summary.py` 等）：保持签名不变；可选择把 `stale` 字段透出给前端做"使用 stale cache 中"的 badge 显示。

### 2.4 Feature flag

| 变量 | 缺省 | 效果 |
|------|------|------|
| `AATS_P0_TUNING_JSON_EXPORT` | `off` | `on` = `refresh_strategy_tuning_overrides` 继续导出 `strategy_tuning_overrides.json` 副本 |
| `AATS_P0_TUNING_FAIL_LOUD` | `off` | `on` = 关掉 cache 回退；任何 DB 读失败都抛 `RuntimeError`（需要严格复现 / 故障演练时开） |

### 2.5 回归测试

`tests/unit/test_strategy_tuning_registry.py`（新建或扩展）：

- `test_load_strategy_tuning_overrides_db_hit_populates_cache` — DB 读成功 → 返回结果带 `stale=False`；`_LAST_OVERRIDES_CACHE` 被刷新
- `test_load_strategy_tuning_overrides_db_flicker_returns_cached_with_stale_flag` — 先一次成功填 cache，再模拟 DB 抛 → 返回 cache 内容 + `stale=True` + 带 warning 日志
- `test_load_strategy_tuning_overrides_cold_start_db_unreachable_raises` — cache 为空 + DB 不可达 → `RuntimeError`
- `test_load_strategy_tuning_overrides_fail_loud_flag_bypasses_cache` — 即使 cache 有，`AATS_P0_TUNING_FAIL_LOUD=on` 下 DB 失败仍抛
- `test_refresh_strategy_tuning_overrides_writes_db_and_skips_file_when_flag_off`
- `test_refresh_strategy_tuning_overrides_exports_json_when_flag_on`
- `test_refresh_strategy_tuning_overrides_populates_cache_after_db_write`

现有消费方的集成测试：
- `rdp_control_summary.py`：补一条"DB 不可达 + cache 为空 → API 报 503"
- 若已有测试覆盖成功路径，留下不动

### 2.6 风险

| 风险 | 缓解 |
|------|------|
| stale cache 会延续一段错误配置（比如 operator 刚在 DB 里改过一轮 overrides，恰好 DB 抖动，旧 cache 又跑起来） | cache 由下一次 DB 成功自动覆盖；`stale=True` 让前端和日志都可见；需要"立刻生效"的变更可通过让 DB 恢复 + 下一轮 load 来刷新；实盘紧急场景可临时 `AATS_P0_TUNING_FAIL_LOUD=on` 强制 decision 停跑直到 DB 回来 |
| 4 进程各自持 cache → 一个进程看到的 stale 时长可能不同 | 可接受：每个进程独立 DB 读，cache 对齐自己的读路径；decision 进程是主要消费方，其它只读消费 |
| cold start 撞上 DB 挂 → 整条 decision loop 起不来 | 这是预期 fail-loud：没有任何 overrides 比默默跑空配置更安全；部署前的健康检查（`/healthz`）就会把这类场景挡在流量之外 |
| 旧 JSON 被 operator 手改期待生效 | 文档 + 迁移备忘：`overrides.json` 已不再读取；真值在 `governance.strategy_tuning_overrides` 表 |
| cache 被污染（比如一次 DB 返回了半成品数据） | 在 `_cache_overrides` 前做基本 shape 校验：必须有 `combo_overrides` 键、值为 dict；否则不进 cache |

---

## 3. P0-1：`recommendation_registry` 去 JSON 化

### 3.1 现状

- 真源并存：`recommendation_registry.json`（`artifacts/decision_system/`）+ `governance.recommendations` 表。
- 写：`_db_sync_recommendation` + `atomic_json_write(registry_path, ...)` best-effort 写 DB；DB 失败只打 warning。
- 状态流转（approve / reject / supersede）：`_db_update_rec_status` 做 CAS-like 更新，命中失败 `rowcount=0` 时回滚 in-memory；同时又把 JSON 重写了一遍。
- API 路径（`rdp_routes.py`）：approve/reject/supersede 都先改 in-memory → 再 `save_recommendation_registry`。

### 3.2 目标

- 单一真源：`governance.recommendations` 表。
- JSON 变只读导出。所有写路径改成"直接落表 + 结束后可选导出 JSON 副本"。
- `rdp_routes.py` approve/reject/supersede handler 里直接用 session-based DB API（失败 → HTTP 5xx），不再依赖 JSON。

### 3.3 新增/修改 API（在 `aats/data_platform/governance/recommendations_db.py`）

已有 `db_upsert_recommendation`；补：

- `db_transition_recommendation_status(session, *, recommendation_id, new_status, expected_current_status, actor, at, notes=None) -> bool`
  - `UPDATE ... SET status = :new_status, ... WHERE recommendation_id = :rid AND status = ANY(:expected)`
  - 返回 `rowcount > 0`
- `db_list_recommendations(session, *, status=None, family=None, timeframe=None, limit=50, offset=0) -> list[dict]`
- `db_get_recommendation(session, recommendation_id) -> dict | None`
- `db_count_recommendations(session, *, filters...) -> int` — 供分页

### 3.4 替换 `recommendation_registry.py`

- 加 `AATS_P0_REC_REGISTRY_MODE`：`dual`（默认，阶段 A/B）/ `db`（DB 单源，阶段 C 起默认）
- `load_recommendation_registry(project_root)`：
  - `mode=db`：从 DB `db_list_recommendations` 拼 `{"recommendations": [...]}`；DB 不可达 → 抛异常
  - `mode=dual`：先 DB；DB 失败 → 读 JSON（warning）
- `save_recommendation_registry(project_root, registry)`：
  - 遍历每条 record → `db_upsert_recommendation`（DB 必写）
  - `AATS_P0_REC_REGISTRY_JSON_EXPORT=on` 时再 `atomic_json_write` 导出
- 所有"改一条记录 + 落盘"的现有 helper 先改为按 `recommendation_id` 精准调 `db_upsert_recommendation` / `db_transition_recommendation_status`。

### 3.5 rdp_routes.py 路径

approve / reject / supersede handler：

1. 用 Session 从 DB 拿当前状态
2. 业务合法性校验（例如 reject 要求 `status in {"draft", "require_review"}`）
3. `db_transition_recommendation_status(..., expected_current_status=...)` → `False` 则 409 Conflict（状态已被其他人变更）
4. 成功后：如 flag `AATS_P0_REC_REGISTRY_JSON_EXPORT=on`，异步导出 JSON；否则不写盘

### 3.6 阶段

- 阶段 A：加新的 DB API + 单测，`recommendation_registry.py` 仍 `dual`（保持现状行为）
- 阶段 B：把 `rdp_routes.py` 写路径切到 DB-first（仍保留 JSON 导出），JSON 只是副本
- 阶段 C：`AATS_P0_REC_REGISTRY_MODE=db` 变默认；`load_recommendation_registry` 的 JSON fallback 退役
- 阶段 D：`save_recommendation_registry` 改成只在 flag `on` 时写 JSON；默认静默

### 3.7 Feature flag

| 变量 | 缺省 | 效果 |
|------|------|------|
| `AATS_P0_REC_REGISTRY_MODE` | `db`（阶段 C 起） | `dual` 可回退到双写 |
| `AATS_P0_REC_REGISTRY_JSON_EXPORT` | `off` | `on` = 继续导出 JSON 副本 |

### 3.8 回归测试

- 已有 `recommendations_db` 单测扩充：
  - `test_transition_recommendation_status_cas_hit_and_miss`
  - `test_list_recommendations_filters_and_pagination`
- `rdp_routes.py`：补 approve/reject/supersede 的 409 Conflict 路径测试
- 集成（WSL2）：真 Postgres 跑一条 draft → approve → superseded 完整流转

### 3.9 风险

| 风险 | 缓解 |
|------|------|
| JSON 被人工编辑作为热修手段 → 阶段 C 起不再生效 | 文档明示；alert 如果 `recommendation_registry.json` 的 mtime 比 DB 最新记录还新 |
| DB 不可达时 API 拒绝所有写 | 符合 fail-loud 设计；通过健康检查先发现 DB 问题 |
| 并发 approve 冲突 | `db_transition_recommendation_status` 的 `expected_current_status` 保证；409 Conflict 清晰返回 |

---

## 4. 全局回归 & 发布计划

1. 每个 P0 的阶段 A/B（schema + API + 单测）→ Windows `pytest tests/unit/ -x -q` 全绿
2. 每个 P0 的阶段 C/D（写路径倒置、读路径收紧）→ 同上 + WSL2 `pytest tests/integration/ -x -q`
3. 上实盘前：
   - `scripts/deploy.sh --skip-commit` 全流水线
   - 健康检查 `/healthz` + 冒烟一条 gate 流程 + 冒烟一次 recommendation approve
4. 文档更新：每完成一个阶段，回头更新本文件的"进度"附录（下方第 5 节）

## 5. 进度附录

- [x] P0-2 阶段 A：新增 `db_record_gate_result` / `db_get_latest_gate_result` / `db_list_gate_results_for_recommendation` / `db_list_gate_results_for_release` / `db_get_gate_result_by_run_id` / `db_set_gate_result_release_id`
- [x] P0-2 阶段 A：单测 7 项，`tests/unit/test_operational_state_db.py` 全绿（10 passed）
- [x] P0-2 阶段 B：`PreApplyGateResultModel` 加 `release_id` 列；`_migrate_pre_apply_gate_results` 幂等迁移；`db_upsert_pre_apply_gate_result` 带 `COALESCE` on conflict
- [x] P0-2 阶段 C：`_save_gate_result` 写顺序倒置；DB 必写、JSON + Markdown 仅在 `AATS_P0_GATE_JSON_EXPORT=on` 时导出；DB 异常 → `gate_status="error", allow_apply=False`（4 条新单测）
- [x] P0-2 阶段 D：`_load_recent_gate_results` 只读 DB；DB 不可达 / 异常抛出 `RuntimeError`；artifacts JSON 目录彻底退出读路径（4 条新单测）
- [x] P0-2 阶段 E：补全代码评审发现的三项阻断
  - H1：`save_release_history` 在 release upsert 的同一事务内调 `db_set_gate_result_release_id` 回填 `release_id`；未命中时打 warning 但不阻塞其它 release 落库
  - H2：`db_set_gate_result_release_id` 的 hit / miss 单测；`_FakeGateSession` 扩展 UPDATE 分支与 `rowcount` 语义
  - M2：`_FakeGateSession._store_gate` 模拟 `COALESCE(EXCLUDED.release_id, current)` 语义；新增"record 重放不能擦掉已回填 release_id"的回归单测
  - `test_operational_state_db.py` 15 passed（含原 10 + H2 两条 + M2 一条 + H1 两条）
- [x] P0-3 实施：`load_strategy_tuning_overrides` DB-主 + 进程 cache + fail-loud 开关
  - `strategy_tuning_registry.py` 新增 `_LAST_OVERRIDES_CACHE` 与 `_cache_overrides` / `_reset_overrides_cache_for_tests`
  - `load_strategy_tuning_overrides`：DB 读成功 → 刷 cache + stale=False；DB 失败 + cache → stale=True warning；cold start / `AATS_P0_TUNING_FAIL_LOUD=on` → RuntimeError
  - `refresh_strategy_tuning_overrides`：派生 overrides → 刷 cache；JSON 只在 `AATS_P0_TUNING_JSON_EXPORT=on` 时写
  - `save_strategy_tuning_overrides`：转 deprecated shim，`DeprecationWarning` + 仅在 flag on 时落盘
  - `test_strategy_tuning_overrides.py` 9 条新单测锁住上述契约
  - `test_strategy_tuning_review.py:293` 的"overrides_path 必须 truthy"断言放宽为"是 str"（JSON 默认关）
- [x] P0-1 阶段 A：新增 4 个 DB API 并用 fake session 单测覆盖
  - `db_transition_recommendation_status(session, *, recommendation_id, new_status, expected_current_status, actor, at=None, notes=None, superseded_by_recommendation_id=None) -> bool`：根据 `new_status` 自动把 actor / at 映射到 approved_* / rejected_* / superseded_* 列；CAS 未命中返回 False（供 rdp_routes 映射 409 Conflict）
  - `db_get_recommendation` — 为 rdp_routes 新写路径提供语义更直白的名字（`db_find_recommendation` 的别名）
  - `db_list_recommendations(*, status=None, family=None, timeframe=None, recommendation_type=None, limit=50, offset=0)` — 在 `db_find_recommendations` 基础上补 recommendation_type 过滤和 `limit`/`offset` 分页
  - `db_count_recommendations(*, filters...)` — 供分页 total 使用
  - `tests/unit/test_recommendations_db.py` 21 条新单测，覆盖 upsert / transition CAS hit & miss / actor 映射 / superseded_by_rec_id / get / list 过滤 / 分页 / count
  - 现有 `db_update_recommendation_status` 保留，recommendation_registry.py 的 `_db_update_rec_status` 暂不改（Phase B 再切）
- [ ] P0-1 阶段 B/C/D：rdp_routes 切 DB-first / load 去 JSON fallback / save 受 flag 控制
