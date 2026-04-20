# AATS 知识图谱与文档重建 · 可行性方案

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> 作者：Claude（基于 2026-04-19 的仓库 snapshot）
> 目的：给 AATS 项目构建一份与代码同步演进的知识图谱，替代/补强现有过时文档，让 Claude 在新任务里能**按结构**定位到事件、服务、配置、调用链，而不是每次都靠 grep 试探。

## 1. 为什么现在值得做

- 项目已经长到 14 个 service 子包、23 个 Pydantic schema 文件、50+ NATS topic、70+ RDP 脚本、10+ `.yaml` profile 加一百多个 `configs/research_batches/*.json`。任何人（含 Claude）冷启动定位代码的成本已经远超 README 一次读完的阈值。
- 现有文档（`README.md`、`ARCHITECTURE.md`、`DEPLOYMENT.md`、`CLAUDE.md`、`AGENTS.md`、`docs/**`）里同一事实分散在多处，且提交频率明显落后于代码（`docs/design/` 只有 1 个文件，`docs/task/` 堆积大量一次性设计稿）。
- 代码形态**非常适合图谱化**：`aats/events/topics.py` 把所有 topic 定义成模块级常量、`aats/events/envelopes.py` 的 `publish_model(topic=…, payload_model=…)` 是唯一发布入口、`bus.subscribe(topics.XXX, …)` 是唯一订阅入口——这些都可以用 AST 精确抽取，无需运行时埋点就能还原 90% 的事件拓扑。

## 2. 图谱建模（本项目定制版 schema）

### 节点类型

| 节点 | 抽取源 | 关键属性 |
| --- | --- | --- |
| `Process` | `apps/{api_gateway,market_gateway,decision_engine,execution_engine}/main.py` | role、container name (aats-*) |
| `Service` | `aats/services/{14 个子包}/` | 所属 process（通过 slice gating 判断）、对外 schema |
| `Module` / `Class` / `Function` | libcst AST 扫描 `aats/`, `apps/`, `scripts/` | 签名、docstring、定义行号 |
| `Topic` | `aats/events/topics.py` 中的模块级常量 | 常量名、字符串值、criticality（从 docstring 提取）、JetStream stream |
| `Schema` (payload) | `aats/schemas/*.py` 的 `BaseModel` 子类 | 字段列表、版本、关联 topic |
| `Config` | `configs/*.yaml`、`configs/rdp_workflows/*.json`、`configs/strategy_profiles/`、`configs/research_batches/` | profile、是否 managed、关联 service |
| `EnvVar` | `.env.*` 的 key + `compose_entrypoint.py` 派生变量 | 所属 profile、被哪些 Settings 读取 |
| `Profile` | `configs/guarded_*_enabled.yaml`、`.env.spot/derivatives[.live]` | spot/derivatives × paper/live × guarded/unguarded |
| `AlphaSignal` / `Strategy` | `aats/services/strategy_engines/`、`aats/services/decision_engine/baseline.py` | reason_code、权重、激活条件 |
| `Migration` | `migrations/*.sql` + `aats/data_platform/migrations/` | 序号、涉及表 |
| `ApiEndpoint` | `aats/api/` 的 FastAPI 路由 | method、path、认证要求 |
| `Test` | `tests/{unit,integration,scenario,smoke,replay}/` | 覆盖的 module/topic/scenario |
| `Doc` | `docs/**` 的 `.md` | 是否 canonical、last_verified_commit |
| `Script` | `scripts/*.py`、`scripts/*.sh` | 入口、依赖的 profile |
| `Table` | Postgres 表（从 migrations + repo 层反推） | columns、被哪些 service 读写 |

### 关系类型

- `Process -[RUNS]-> Service`
- `Service -[PUBLISHES {criticality}]-> Topic`
- `Service -[SUBSCRIBES]-> Topic`
- `Topic -[CARRIES]-> Schema`
- `Function -[CALLS]-> Function`（跨服务调用重点标注）
- `Module -[IMPORTS]-> Module`
- `Service -[READS_CONFIG]-> Config`
- `Service -[READS_ENV]-> EnvVar`
- `Config -[APPLIES_IN]-> Profile`
- `AlphaSignal -[FEEDS]-> DecisionEngine`
- `Service -[WRITES]-> Table` / `Service -[READS]-> Table`
- `Migration -[ALTERS]-> Table`
- `Test -[COVERS]-> (Function | Topic | Scenario)`
- `Doc -[DESCRIBES]-> *`（反向索引，用于判断"某个主题有没有文档"）
- `Commit -[TOUCHES]-> *`（可选，用于新鲜度分析）

这套 schema 可以回答像"**ORDER_INTENTS 的 payload 字段是什么？谁发布？谁订阅？哪些 test 覆盖？最近谁改过？**"这种一问到底的问题。

## 3. 数据抽取管道

| 来源 | 工具 | 覆盖内容 | 可信度 |
| --- | --- | --- | --- |
| 静态 AST | **libcst**（保留注释）+ **jedi** 做跨模块 resolve | topic 定义、publish/subscribe 调用点、Pydantic schema 字段、FastAPI 路由、import 图 | 权威 |
| YAML / JSON | PyYAML + Pydantic v2（复用 `aats/bootstrap/settings.py` 里的 loader） | profile 层叠、managed config、research_batches | 权威 |
| 环境变量 | `grep -rn "os.environ\\|getattr(settings," aats/` + `.env.*` key 合并 | EnvVar → Settings 映射 | 高 |
| SQL migrations | **sqlglot** 解析 `migrations/*.sql` | 表结构与演化 | 权威 |
| git 历史 | `git log --follow --format` | 每节点最近修改时间、touch 频次 | 高 |
| OTel / Jaeger | 可选，读 `docker logs aats-jaeger` span 样本 | 运行时 `CALLS` 关系补强（AST 看不到的动态派发） | 中（样本依赖） |
| 现有 docs | 正则 + LLM 抽取元数据 | 仅用于"知识候选"，**不作为 ground truth** | 低 |

**关键原则：代码是 ground truth，docs 只是辅助候选**。当 doc 与 AST 结果冲突时标记 doc 为 "stale"，进入归档队列。

### 现有 docs 分级

- **保留并认领**：`ARCHITECTURE.md`（需大改）、`DEPLOYMENT.md`、`README.md`、`CLAUDE.md`、`AGENTS.md`、`docs/operations/*`——每篇顶部加 `last_verified_commit` 字段，由 CI 校验。
- **归档**：`docs/task/*.md`（绝大多数是一次性 slice 设计稿，完成后就成了考古资料）→ 移到 `docs/archive/task/` 并在图谱里标 `status=historical`。
- **由图谱生成**：service 卡片、topic 目录、schema 字段表、事件流向图（Mermaid）——不再手写。
- **人类原创**：设计取舍、风险说明、runbook——继续手写，但通过图谱关系挂到对应节点。

## 4. 存储与查询方案对比

| 方案 | Claude 易用性 | 维护成本 | 查询表达力 | 与本项目契合度 | 结论 |
| --- | --- | --- | --- | --- | --- |
| **(a) Neo4j / Memgraph** | 需 MCP 桥接才直用；Cypher 对 Claude 友好 | 多一个服务要维护；WSL2 Docker 栈已经很重 | 最强（图遍历、路径查询） | 中——会和现有 infra 重复 | ❌ 短期不值得 |
| **(b) SQLite + 关系表** | 直接 `sqlite3` CLI / MCP，一条 SQL 拿结果 | 极低（单文件、纯标准库） | 足够（JOIN 能表达所有关系） | 高——可以直接放 `artifacts/aats_graph.sqlite` | ✅ **推荐作为真相源** |
| **(c) Markdown + 双向链接（Obsidian 风）** | 最好——Claude 一个 Read 就拿到 | 节点一多，链接维护成本指数级增长 | 弱（靠 grep） | 中——和 `docs/` 契合，但关系查询糟糕 | ✅ **作为派生视图**（从 SQLite 生成） |
| **(d) 向量库 + 元数据** | 相似度检索强，但关系查询弱 | 需 embedding pipeline | 适合"找相似代码"而非"找调用链" | 低——本项目问题是结构化定位，不是模糊检索 | ⚠️ 仅在 RAG 阶段锦上添花 |

**推荐组合：SQLite 作唯一真相源，自动派生三类视图**：
1. 机器视图：`artifacts/aats_graph.sqlite`（Claude 通过 SQL 查）。
2. 人类视图：`docs/_generated/`（service 卡片、topic 目录等 md，git 跟踪，PR diff 可审）。
3. 可视化：`docs/_generated/diagrams/*.mermaid`（事件流、进程边界）。

## 5. Claude 接入方式

优先级从低到高：

1. **派生 markdown（零门槛）**：立即可用。Claude 在任务开始时 `Read docs/_generated/service_index.md` → 定位可疑 service → `Read docs/_generated/services/<name>.md` 拿 publish/subscribe/config 一把梭。无新基础设施。
2. **本地 CLI**：`python scripts/graph_query.py --topic ORDER_INTENTS --what publishers` 之类，直接吐 JSON。Claude 通过 bash 调。
3. **MCP server（PoC 验证后再做）**：把 SQLite 包装成一个 `aats-graph` MCP server，暴露 `query_topic / query_service / trace_event_flow` 等 tool。只有当 CLI 证明加速明显时再投入。
4. **RAG**：只在"找相似实现参考"场景才需要。本项目的核心痛点是**精确定位**而非模糊检索，向量库是后置可选项。

## 6. 新鲜度保证

- **pre-commit**（必须）：`scripts/precommit.sh`（已存在）里加一步 `python scripts/graph_rebuild.py --incremental --changed-files $(git diff --cached --name-only)`，对修改到的 `aats/events/topics.py`、`aats/schemas/*.py`、`aats/services/**` 做增量重建；若图谱 diff 太大则 block commit，强制作者确认。
- **CI 全量重建**：每次 PR 跑 `graph_rebuild.py --full`，产出 `artifacts/aats_graph.sqlite` 作为 CI artifact；若派生 markdown 有 diff，自动提交到 PR（或要求作者一起提交）。
- **必须立刻反映的变更**：topic 增删改、schema 字段变更、service 搬家、profile 增删、migration 新增。这些改动如果没有同步图谱，CI 阻塞合并。
- **允许延迟的**：docstring、注释、docs/ 文本——每日 nightly job 扫。

## 7. 分阶段路线图

### Phase 0 · MVP（约 3–5 人日）

**产出**：
- `scripts/graph_rebuild.py`——从 `aats/events/topics.py`、`aats/events/envelopes.py`、`bus.subscribe` 调用点抽出 `Topic / Service / PUBLISHES / SUBSCRIBES` 四类节点/关系，写入 `artifacts/aats_graph.sqlite`。
- `docs/_generated/topics.md`、`docs/_generated/services/*.md`（自动生成）。

**验收**：随机抽 5 个 topic，人工核对 publisher/subscriber 与 grep 结果一致率 100%；Claude 被问"谁订阅 `ORDER_INTENTS`"能在 1 次 SQL 内答对。

### Phase 1 · 覆盖 schema / config / endpoint（5–8 人日）

**产出**：扩展到 `Schema`（解析 `aats/schemas/*.py`）、`Config` / `Profile`（解析 `configs/*.yaml` 层叠规则）、`ApiEndpoint`（FastAPI 路由）。派生"profile 能力矩阵"markdown。

**验收**：能回答"`derivatives_live` profile 相比 `derivatives` 多要求哪些 env var / config 门槛？"——这是 README §2 当前靠手写维护的信息。

### Phase 2 · 调用链与测试覆盖（8–13 人日）

**产出**：加入 `Function CALLS Function`、`Test COVERS *`，覆盖 `tests/**`；为 decision_engine 的 baseline alpha 信号链路生成端到端 Mermaid 图。接入 git 历史（每节点 `last_touched_commit`）。

**验收**：对"P1.5 funding-rate 信号从特征到决策到执行的完整链路"出一张图，所有节点都能在 30 秒内跳到源码行。

### Phase 3 · MCP + 运行时补强（可选，13+ 人日）

**产出**：`aats-graph` MCP server；接入 OTel span 采样做 `CALLS` 关系补强；文档与图谱的 CI diff bot。

**验收**：Claude 在新会话中"冷启动定位代码"平均 tool call 数下降 50%+（用 Phase 0 前后任务对比基线）。

## 8. 风险清单

1. **动态派发看不到**：decision→execution 走 NATS，AST 抽不出"A 服务的函数被 B 服务消费"。缓解：先把"A publish topic X"+"B subscribe topic X"拼成虚拟边；真要精确需要 OTel 样本。
2. **图谱和代码失步**：这是最大的翻车点。必须 CI 强约束，否则迅速变回 docs 的命运。方案见 §6。
3. **Pydantic forward refs / `TYPE_CHECKING`**：libcst 单文件解析看不到 `TYPE_CHECKING` 下的真实类型。需配合 jedi 的 project-level resolver。
4. **configs 层叠语义**：`base.yaml` → `dev.yaml` / `prod.yaml` / `guarded_*.yaml` 的合并规则必须与 `aats/bootstrap/settings.py` 行为一致，否则图谱里的"某 key 在某 profile 下的值"会骗人。方案：直接复用 settings loader，不要自己 reimplement。
5. **人工写的 docs 与图谱职责边界**：如果图谱试图生成所有文档，设计取舍（为什么选 NATS 而不是 Kafka）会被丢掉。要明确划分人原创 vs 派生生成。
6. **Windows / WSL2 路径差异**：`scripts/graph_rebuild.py` 需要处理 CLAUDE.md 提到的路径规则，避免 mount 差异搞坏相对路径。

## 9. 最小可验证 PoC

**选题：decision_engine 的 baseline alpha 信号链路**（最近 8 个 commit 都在这条链路上，价值最高且边界清晰）。

**范围**（一周内可做完）：
- 节点：`aats/services/feature_engine/long_short_poller.py`、`aats/data_platform/collectors/**/funding_*.py`、`aats/services/decision_engine/baseline.py`、相关 schema（`FeatureSnapshot`, `DecisionContext`, `BaselineAssessment`）、相关 topic（`FEATURE_SNAPSHOTS`, `DECISION_CONTEXTS`, `BASELINE_ASSESSMENTS`, `POLICY_DECISIONS`）、相关 configs（`configs/strategy_profiles/` 里的 baseline 权重）。
- 抽取：只跑 libcst + YAML loader。
- 存储：单文件 `artifacts/poc_graph.sqlite`。
- 查询：写 3 条 SQL view（`publishers_of(topic)`、`config_keys_read_by(service)`、`alpha_signal_chain(signal_name)`）。

**验证方式（重要）**：找 3 个真实任务，比如"加一个新 alpha 信号 `open_interest_skew`"、"调整 funding-rate 权重到 0.15"、"排查 `BASELINE_ASSESSMENTS` 为什么某天没 publish"——分别让 Claude 在**有图谱**和**无图谱**两种条件下跑一遍，记录：首次定位到正确文件的 tool call 数、首次回答的准确率。

**PoC 通过标准**：tool call 数减少 ≥ 40%，至少 2/3 任务的准确率提升。达标则进入 Phase 1 扩量，不达标则要回头看 schema 设计是不是没抓到 Claude 实际卡住的信息类型。

## 10. 一句话收尾

**代码已经足够规整**（topic 是模块常量、publish 是单入口、service 是子包），**难点不在建图而在维持新鲜度**。先用 3-5 人日做一个只覆盖 topic+service+schema 的 MVP 和一个能被 CI 守住的 rebuild 脚本，拿 baseline alpha 信号链路做 PoC——如果 Claude 真能省 40% 的 tool call，再谈 MCP、RAG、Neo4j。
