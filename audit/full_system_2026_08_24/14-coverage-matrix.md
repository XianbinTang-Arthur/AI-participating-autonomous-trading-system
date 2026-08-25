# 14 审查覆盖矩阵

## 覆盖结论

本次对资金、启动、部署、数据库、研究、API 和 UI 关键路径做了深审，但**没有逐行审完仓库全部代码**。逐 tracked 文件状态保存在 [review-coverage.csv](review-coverage.csv)。任何不在 `FULLY REVIEWED` 的文件都不得被描述为“已经完整审查”。

| 状态 | 文件数 | tracked 占比 | 定义 |
|---|---:|---:|---|
| FULLY REVIEWED | 53 | 2.63% | 本次完整读取/核对；仅对冻结基线有效 |
| PARTIALLY REVIEWED | 31 | 1.54% | 审了关键区段、调用方或测试，未逐行封板 |
| DISCOVERED BUT NOT YET REVIEWED | 1,935 | 95.84% | 已清点但未达到逐行标准 |
| 合计 | 2,019 | 100% | Git tracked 基线 |

低比例是刻意保持真实性的结果：运行 4,135 个单元测试、全文检索或导入模块不等于逐行 code review。

## 专题覆盖

| 专题 | 覆盖 | 证据 |
|---|---|---|
| 基线/约束/入口 | 高 | 根说明、AGENTS/CLAUDE、main entries、Compose、deploy |
| 配置合成 | 中高 | settings/managed profile/Compose 关键路径；秘密 env 未读 |
| 资金执行/恢复 | 中高 | OrderManager/command/OKX/portfolio/recovery 关键路径与测试交叉 |
| 量化/研究/回测 | 中 | independent replay、Research Factory、成本模型；其他 family 未逐行 |
| 数据库/迁移 | 中 | ORM 元数据、root migrations、RDP create_all/Batch B；全部 SQL 未逐行 |
| API/前端 | 中 | 193 routes 清点，auth/RDP/UI关键路径；全部字段契约未逐项 |
| 安全 | 中 | auth、network、headers、凭证边界；无历史 secret/CVE 扫描 |
| 可访问性 | 中低 | 静态 keyboard/dialog/motion；无真实辅助技术测试 |
| 性能/容量 | 中低 | 静态预算和只读采样；无压测/EXPLAIN |
| 文档真实性 | 低到中 | 现行入口与相关声明交叉；730 个 docs 未逐份纠错 |

## 生成/第三方/排除项

| 范围 | 状态 | 理由 |
|---|---|---|
| 本目录的 17 份报告、README、AUDIT_STATE、CSV | GENERATED | 本次审计产物，不属于冻结 tracked 基线 |
| `test-tmp/`、`integration-collect-tmp/` | GENERATED / IRRELEVANT | pytest 临时产物，已由本目录 `.gitignore` 排除 |
| `.venv/`、工具缓存 | THIRD-PARTY/VENDOR / EXCLUDED | 非 Git tracked；只读取版本/运行测试，不审第三方源码 |
| `.git/`、Docker volumes、数据库物理文件 | EXCLUDED | 非应用源代码；避免破坏状态 |
| `.env.*` | EXCLUDED | 凭证安全边界；未读取或显示 |
| 当前余额、持仓、订单、成交、策略收益 | EXCLUDED | 未获准查询 live 资金状态，也非静态代码证据 |

## 已完整读取的类型

CSV 中 `FULLY REVIEWED` 主要包括：现行根约束与入口文档、主进程 main、所有 Compose overlay、部署/同步/entrypoint、process lifecycle、配置 profile 辅助、交易 session、根迁移、RDP DB入口、selected research dataset/DSL/fill simulator、UI shell/login/auth primitives。

## 部分读取的关键文件

包括大型 composition root/settings、NATS、OKX adapter、OrderManager、execution command、portfolio handler、RDP routes/models、Research Factory runner、backtest harness、生产 scoring、主 UI bundle/CSS。报告中的精确结论只依赖已引用区段和调用链；未引用部分仍是未知。

## 续审规则

1. 先校验 HEAD/status；有漂移则受影响文件降级。
2. 从 CSV 的 P1 相关 partial 文件开始，不要按目录顺序机械阅读。
3. 每个文件升级为 FULLY REVIEWED 前必须读完整文件、主要调用方、下游副作用和相关测试。
4. 发现问题必须标 VERIFIED/INFERRED/UNKNOWN，并记录 exact location。
5. 每次续审更新 CSV、AUDIT_STATE 和风险登记簿；不得只更新执行摘要。
