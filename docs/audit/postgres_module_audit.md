# Postgres 模块审查报告

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


**审查日期**: 2026-04-13
**审查范围**: Postgres 数据库层（Docker 配置、连接池、ORM 模型、仓储层、迁移系统、备份、监控）
**基准**: Stage 9 dryrun checklist §3.1 + 项目架构需求

---

## 1. 审查范围与文件清单

### 核心基础设施
| 文件 | 行数 | 说明 |
|------|------|------|
| `aats/storage/session.py` | 328 | 连接池、advisory lock、迁移系统、schema 验证 |
| `aats/storage/sqlalchemy_models.py` | 1256 | 全部 ORM 模型定义（35+ 表） |
| `aats/storage/base.py` | ~80 | OptimisticLockError、SaveResult |
| `aats/data_platform/db.py` | 76 | 研究数据平台独立连接池 |

### 仓储层（24 个 Postgres Repository 实现）
| 文件 | 职责 |
|------|------|
| `execution_repo_postgres.py` | OrderState 持久化 |
| `execution_fill_repo_v2_postgres.py` | FillEvent UPSERT + 去重 |
| `execution_order_repo_postgres.py` | ExecutionOrder 状态历史 |
| `execution_command_repo_postgres.py` | 执行命令队列 |
| `portfolio_repo_postgres.py` | PortfolioSnapshot + Listener |
| `ledger_repo_postgres.py` | 复式记账 + advisory lock |
| `reservation_repo_postgres.py` | 资金预留 + FOR UPDATE 行锁 |
| `outbox_repo_postgres.py` | EventEnvelope Outbox |
| `command_outbox_repo_postgres.py` | CommandOutbox V2 |
| `event_store_postgres.py` | hot/archive 双表 |
| `audit_repo_postgres.py` | 决策审计记录 |
| `fill_outcome_repo_postgres.py` | 成交结果 |
| `funding_fee_repo_postgres.py` | 资金费率 |
| `sleeve_pnl_repo_postgres.py` | 策略袖套 PnL |
| `obligation_repo_postgres.py` | 订单义务 |
| `lot_repo_postgres.py` | 持仓 Lot |
| `reconciliation_repo_postgres.py` | 对账报告 |
| `strategy_sleeve_repo_postgres.py` | 策略袖套 |
| `strategy_runtime_repo_postgres.py` | 策略运行时 |
| `strategy_profile_repo_postgres.py` | 策略配置版本 |
| `exit_execution_repo_postgres.py` | 退出执行链 |
| `execution_repo_converged_postgres.py` | 收敛仓储 |
| `inbox_repo_postgres.py` | 外部事件收件箱 |
| `operator_repo_postgres.py` | 运营用户 |

### 部署配置
| 文件 | 说明 |
|------|------|
| `deploy/wsl2-dev/docker-compose.yml` | Postgres 16-alpine 容器定义 |
| `deploy/wsl2-dev/initdb/create-databases.sh` | 5 库初始化脚本 |
| `deploy/wsl2-dev/scripts/backup_postgres.sh` | pg_dump 备份 |
| `deploy/wsl2-dev/grafana/.../aats_operations.json` | Postgres Connections 面板 |
| `deploy/wsl2-dev/grafana/.../rules.yml` | 告警规则（检查覆盖） |

### 测试
| 文件 | 说明 |
|------|------|
| `tests/unit/test_database_runtime_guard.py` | advisory lock 单元测试 |
| `tests/unit/test_scoped_runtime_lock_key.py` | lock key 派生逻辑 |
| `tests/unit/test_process_role_settings.py` | 进程角色配置 |
| `tests/integration/test_phase1_shadow_postgres.py` | 影子模式集成测试 |
| `tests/integration/test_execution_outbox_postgres.py` | Outbox 持久化集成 |
| `tests/integration/test_task58_financial_convergence_postgres.py` | 财务收敛 |
| + 4 more integration test files | |

---

## 2. 需求检查清单

### 2.1 连接池与资源管理

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| C-1 | 连接池大小合理 | ✅ PASS | pool_size=10, max_overflow=20 → 单进程 max 30；4 进程×30=120 < max_connections=200 |
| C-2 | pool_pre_ping 启用 | ✅ PASS | 所有 engine（AATS core + RDP）均设置 pool_pre_ping=True |
| C-3 | pool_timeout 配置 | ✅ PASS | AATS core=30s；RDP 使用 SQLAlchemy 默认 30s |
| C-4 | expire_on_commit=False | ✅ PASS | 两处 sessionmaker 均设置，防止提交后意外 lazy load |
| C-5 | session 上下文管理 | ✅ PASS | 24 个仓储全部使用 `with session_factory() as session:` 模式 |
| C-6 | engine.dispose() 清理 | ✅ PASS | DatabaseRuntime.dispose() 先释放 advisory lock 再 dispose engine |
| C-7 | RDP 连接隔离 | ✅ PASS | data_platform/db.py 独立 pool（size=5, overflow=10），与 AATS core 不共享 |

### 2.2 数据精度与完整性

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| D-1 | 金融列使用 Numeric(36,18) | ✅ PASS | DECIMAL_36_18 统一类型，~99 列覆盖所有金额/数量/价格 |
| D-2 | 启动时 schema 验证 | ✅ PASS | validate_runtime_schema() 查 information_schema 比对精度，不匹配则 raise |
| D-3 | 乐观并发控制 | ✅ PASS | OrderStateModel、StrategyExecutionBundleModel 使用 row_version + version_id_col |
| D-4 | 外键约束 | ✅ PASS | 关键关联有 ForeignKey（ledger_entries→journals→accounts, fills→orders, settlements→fills 等） |
| D-5 | 唯一约束 | ✅ PASS | dedupe_key UNIQUE、source+venue_fill_id UNIQUE、journal source UNIQUE 等 |
| D-6 | 时间戳 timezone-aware | ✅ PASS | 所有 DateTime 列使用 DateTime(timezone=True) |

### 2.3 并发安全

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| L-1 | 单实例运行保护 | ✅ PASS | pg_try_advisory_lock 按 process_role 派生独立 lock_key |
| L-2 | 多进程不互斥 | ✅ PASS | SHA256(base_key\|host\|port\|db\|schema\|role) 确保 4 角色独立 |
| L-3 | 行级锁 | ✅ PASS | reservation_repo 使用 `with_for_update=True`，ledger_repo 使用事务级 advisory lock |
| L-4 | UPSERT 幂等 | ✅ PASS | execution_fill_repo_v2 使用 `on_conflict_do_nothing()` |
| L-5 | Outbox 模式 | ✅ PASS | EventOutbox + CommandOutbox 双 outbox，状态跟踪、重试计数、错误截断 |

### 2.4 Docker 配置

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| K-1 | 内存调优 | ✅ PASS | shared_buffers=768MB, work_mem=64MB, maintenance_work_mem=512MB, effective_cache_size=1536MB |
| K-2 | WAL 配置 | ✅ PASS | wal_level=replica, max_wal_size=1GB, checkpoint_timeout=10min |
| K-3 | 慢查询日志 | ✅ PASS | log_min_duration_statement=500ms |
| K-4 | 连接上限 | ✅ PASS | max_connections=200 > 120（4 进程最大并发） |
| K-5 | 健康检查 | ✅ PASS | pg_isready, interval=10s, retries=5, start_period=20s |
| K-6 | 内存限制 | ✅ PASS | deploy.resources.limits.memory=2560M |
| K-7 | 数据持久化 | ✅ PASS | postgres_data named volume + initdb mount |

### 2.5 环境隔离

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| E-1 | 多库隔离 | ✅ PASS | 5 库：aats_spot, aats_derivatives, aats_live_spot, aats_live_derivatives, aats_research |
| E-2 | initdb 幂等 | ✅ PASS | 检查 pg_database 后再 CREATE，首次初始化限定 |
| E-3 | 备份脚本 | ✅ PASS | pg_dump -Fc + 14 天保留 + partial→rename 安全写入 |

### 2.6 ORM 与索引设计

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| I-1 | 复合索引覆盖常用查询 | ✅ PASS | scope(product_type, margin_mode) 复合索引覆盖所有 scope 过滤 |
| I-2 | 时间戳索引 | ✅ PASS | created_at、ingestion_timestamp 等热查询字段均有索引 |
| I-3 | Outbox 状态索引 | ✅ PASS | (status, created_at) 支持 FIFO 消费 |
| I-4 | 唯一索引去重 | ✅ PASS | dedupe_key UNIQUE、source+venue_fill_id UNIQUE 等 |

### 2.7 监控 (Stage 9 §3.1)

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| M-1 | Postgres Connections 面板 | ✅ PASS | Grafana 面板直查 pg_stat_activity，显示 idle/active 连接数 |
| M-2 | idle connections ≥ 2 可视化 | ✅ PASS | 面板描述标注 Stage 9 需求 |
| M-3 | Postgres 专用告警 | ⚠️ NOTE | 无直接 Postgres 告警规则——见 §3 讨论 |

---

## 3. 发现项

### 无需修复的发现项

经审查，Postgres 模块**无需立即修复的缺陷**。以下两项为信息级备注：

---

#### P-1 (INFO): 遗留迁移函数为死代码

**位置**: `aats/storage/session.py` 第 216-244 行 `apply_current_migrations()`

**现状**: 该函数扫描 `{project_root}/migrations/*.sql` 并逐文件执行。但在 commit `b688d0a`（2026-04-10）中，全部 22 个 .sql 迁移文件已被删除，schema 创建改用 ORM `create_all()`。`migrations/` 目录现在为空（仅含空的 `research/` 子目录）。

**影响**: 函数调用为 no-op（空 glob → 空循环），**不影响运行时行为**。但保留死代码可能误导维护者以为仍需管理 .sql 文件。

**建议**: 低优先级。可在后续清理中移除该函数及相关的 `_ensure_schema_migrations_table()`、`_applied_migration_checksums()`、`applied_migrations()` 函数，或加注释说明其历史用途。不影响 Stage 9 dryrun。

---

#### P-2 (INFO): Postgres 监控依赖间接路径

**现状**:
- Grafana 面板通过直接 SQL 查询 `pg_stat_activity` 显示连接状态 → **满足 §3.1 可视化需求**
- 告警规则 (`rules.yml`) 无 Postgres 专用规则 → 连接池耗尽等场景通过 SEV3 错误率告警间接覆盖
- 无 Prometheus postgres_exporter → 缺少 query 延迟分布、cache hit rate 等深度指标

**影响**: 对 Stage 9 dryrun checklist **足够**——§3.1 只要求 "Postgres idle connections ≥ 2" 的可视化验证，面板已满足。但从长期运营角度，专用 exporter + 直接告警更稳健。

**建议**: 中期（Stage 10+）考虑添加 postgres_exporter 和连接池告警规则。当前不阻塞 Stage 9。

---

## 4. 架构亮点

### 4.1 三层仓储模式统一
24 个 Postgres 仓储全部遵循相同模式：
```
构造器: __init__(session_factory: sessionmaker[Session])
会话管理: with self.session_factory() as session: ... session.commit()
双方法: method() + method_in_session() 支持外部事务协调
```
无 session 泄漏风险，错误自动回滚。

### 4.2 金融精度全链路守护
```
定义层: DECIMAL_36_18 = Numeric(36, 18)  → 所有金额列
验证层: validate_runtime_schema()        → 启动时查 information_schema 比对
仓储层: on_conflict_do_nothing()         → 幂等写入
并发层: row_version + version_id_col     → 乐观锁防止覆盖
```

### 4.3 多进程 Advisory Lock 精密隔离
```python
seed = f"{base_key}|{driver}|{host}|{port}|{database}|{schema}|{role}"
digest = SHA256(seed)
lock_key = int.from_bytes(digest[:8], signed=False) & ((1<<63)-1)
```
确保 gateway/market/decision/execution 四角色各自持有唯一锁，互不阻塞。

### 4.4 Outbox 双管道保证投递
- **EventOutbox**: EventEnvelope 级别，PENDING→PUBLISHED/FAILED 状态机
- **CommandOutbox**: 执行命令级别，独立重试计数 + 错误截断
- 两者均有 `(status, created_at)` 索引支持高效 FIFO 消费

---

## 5. 审查结论

| 维度 | 评估 |
|------|------|
| 连接池与资源管理 | ✅ 生产就绪 |
| 数据精度与完整性 | ✅ 全链路守护 |
| 并发安全 | ✅ Advisory lock + OCC + 行锁 |
| Docker 配置 | ✅ 内存/WAL/慢查询全调优 |
| 环境隔离 | ✅ 5 库分离 + 幂等初始化 |
| ORM 与索引 | ✅ 35+ 表、复合索引覆盖热路径 |
| 仓储层 | ✅ 24 个仓储模式统一 |
| 备份 | ✅ pg_dump + 14 天保留 |
| 监控 | ✅ 满足 Stage 9（信息级改进空间） |
| 测试 | ✅ 单元 + 集成覆盖核心路径 |

**总结**: Postgres 模块架构成熟、实现规范，**满足 Stage 9 dryrun 全部需求**。两项信息级备注（遗留死代码、监控深度）均不影响功能正确性或安全性，可纳入中期改进计划。

---

*审查人: Claude (AI Audit)*
*审查方法: 源码逐行审读 + 配置交叉验证 + Stage 9 checklist 逐项核对*
