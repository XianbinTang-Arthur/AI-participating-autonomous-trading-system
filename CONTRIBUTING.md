# CONTRIBUTING — AATS 项目工作纪律

> 本文件约束**所有贡献者**（含 AI agent）在本仓库的工作行为。
> 用户（XianbinTang-Arthur）已授权 AI agent 自主迭代；以下纪律是 AI agent 的**自律底线**，不得违反。

---

## 一、硬约束（违反 = 立即回滚 + 红字记录）

### 凭证 & 秘密
- **绝不读**任何包含凭证的文件：`.env.*.live` / `.env.wsl2` / 任何 `credentials_dir` 里的文件、API key、password、token
- **绝不 echo / print / log** 凭证、密码、token（即便脱敏也不）
- **绝不 commit** 凭证相关的文件

### 实盘资金 & 仓位
- AI agent **绝不通过手工命令、临时脚本、API 调用或数据库写入**直接触发下单、平仓、资金转移、提现；真实交易只能由已部署 runtime 在已配置的风控、kill switch、执行网关和审计链内产生
- `ai_operating_mode` / `ai_execution_suggestion_mode` / `ai_provider` 可以被修改为全面启用 AI，但必须满足：用户明确授权、bounded task、SOW、相关测试、可回滚路径、运行范围不扩大、风控与执行硬门不被绕过
- AI 可在 `ai_decision_maker` 与 `enabled_live` 配置下参与或主导决策/执行建议，但不得绕过 symbol/venue/family 范围、risk engine、kill switch、truth chain、release/promotion gate 或人工明确冻结
- **绝不改** Kill switch / recovery policy 语义字段
- **绝不改** Risk engine 的硬约束（max_symbol_notional、only_reduce 触发条件等）

### 数据层
- **绝不用** `rsync` 同步代码到 WSL2（会让 WSL 侧 git dirty）
- **绝不** drop / truncate / rename 任何 Postgres 表或列
- **绝不跑** unverified migration
- **绝不** 禁用 pre-commit hook（`--no-verify`）

---

## 二、软约束（违反 = 必须在 weekly review 红字记录并解释）

### Git & Commits
- 所有 commit 必须按以下格式（中文 OK）：
  ```
  <type>(<scope>): <subject>
  
  ## 假设 / 证据
  ...
  ## 修复 / 改动
  ...
  ## 预期效果
  ...
  ## 验证方法
  ...
  ## Rollback 路径
  ...
  ```
- 不 `git add -A`，按文件精确加
- 不在一个 commit 里混两个不相关的改动

### 部署
- 只有跑过 **相关 regression 测试** 才能 `bash scripts/deploy.sh --skip-commit`
- 部署后必须做 **before/after 实测**（日志、PG 状态、UI 体感），不许空说"应该好了"
- 高风险部署 pause 等用户决策

### 代码审查
- audit agent 的发现**必须**人工核实后才能动手
- "文档说已完成但代码未验证"的 SOW，**必须先读代码证实**，再做 / 不做的决策
- 遇到"假阳性 / 真阴性"要在 weekly review 里提到

### 文档
- 所有**自主**的重大决策写进 `docs/autonomous_sessions/YYYY_MM_DD_<slot>.md`
- 每周至少一次 weekly review 到 `docs/weekly_review/YYYY-MM-DD.md`（按 `_template.md`）
- 工作节奏 "假设 / 效果 / 验证 / rollback" 格式贯穿

---

## 三、建议做法（best practice, 不强制）

- 大改动前启动 Plan subagent 写出 plan，不要盲目直接动手
- 复杂 bug 用 Explore subagent 做代码审查，但**结论必须自己核实**
- 性能类改动提供量化数据（before P95=X, after P95=Y）
- 架构级重构先列"可逆点" / "不可逆点"，把不可逆的做得极慢
- 借鉴业界经验（SQLAlchemy 官方 / Grafana OSS / Jane Street blog 等）但**必须证明适合我们的 stage**，不盲搬

---

## 四、Python / 代码规范

- SQLAlchemy 2.0，`sessionmaker(expire_on_commit=False, future=True)`
- JSON 列访问用 `.as_string()`，JSONB 列 `.astext` 或 `.as_string()`
- `with session_factory() as session:` 标准 context manager
- 所有 repo 方法加 `_for_scope` 变体（如果涉及跨 scope 可能全扫）
- 避免 `select(Model).order_by(...)` 无 WHERE + 无 LIMIT

## 五、语言

- 与用户沟通：**中文**
- 前端文案：**UTF-8 中文**
- 代码注释、commit message：中英混用 OK
- 本文件：中文

---

## 六、信任机制

用户对 AI agent 的信任 = `max(0, 历史信任 - 违规成本) + 每次交付的 earned trust`。

"earned trust" 来源：
- 诚实承认错误（主动写到 weekly review "我做错了什么"一节）
- 关键决策 pause 等用户确认
- 每次"修好了"附实测数据

"违规成本"最高的事：
- 瞒报错误
- 触发实盘资金操作
- 空说"修好了"但生产还卡

**最终原则**：宁可做得慢一点、少一点，不要一次犯错摧毁用户的信任。

---

_本文件首次起草：2026-04-21 by Claude agent during 8h autonomous session._
_用户审查签收：TBD_
