# AATS Research OS G0 Bootstrap 实施任务书

> **2026-08-29 局部范围失效说明**：本文中 Binance 的能力合同、许可或 G1 前提已经由
> [`来源准入与收益优先级纠偏`](aats_research_os_source_access_and_profit_priority_correction_2026_08_29.md)
> 替代。独立仓库已改为 OKX-only source contract v2；本文原文仅用于说明 bootstrap 当时的实施范围。

> 文档状态：历史实施基线；当前实现事实以交付记录和独立仓库代码为准
> 最后核对：2026-08-27（AATS 基线 `40dc6817861a1ddfd92cc8a01d2b9ce87af523aa`，分支 `main`）
> 决策来源：[`G0 立项决议记录`](aats_research_os_g0_decision_record_2026_08_26.md)
> 执行计划：[`G0 收口与 G1 启动计划`](aats_research_os_g0_closure_and_g1_kickoff_plan_2026_08_26.md)
> 当前交付：[`G0 Bootstrap 交付记录`](aats_research_os_g0_bootstrap_delivery_2026_08_27.md)
> 运行边界：不读取 `.env*`，不启动 AATS/交易所服务，不采集或持久化第三方市场数据
> 授权边界：不授权正式 G1、live、真实资金、下单、Runtime 参数写入、付费订阅或外部付款

## 1. 当前行为与目标

实施开始时目标路径 `D:\文件\project\AATSResearchOS` 不存在。该句是 2026-08-26 起始快照，
不是当前状态；当前已建立独立本地仓库及 root commit `22a465b06cd731a27cf92154190b1b480fa84d2b`，
但仍无 remote 或正式 G0 通过。起始时 AATS 的 Research OS 只有决议、
主计划和下一步计划，没有独立仓库、代码、CI、治理台账或 G0 Closure Packet。

本任务的业务目标是把 G0 中可由工程执行的事项变成可复核产物，同时保持所有人类责任条件真实
开放。完成后应存在：

1. 独立本地 Git 仓库及无凭证、无 Runtime 写路径的 Python 骨架；
2. InstrumentVersion、Canonical Event、不可变 Raw 的最小合成数据实现；
3. Threat Model、License Matrix、预算台账、角色册模板、Legacy Freeze 和 Closure Packet；
4. 自动测试证明确定性、不可覆盖、时间语义和禁止依赖；
5. 明确列出尚未签字的人类条件，不把模板或工程检查写成 G0 已通过。

## 2. 范围与非范围

### 2.1 本轮范围

- 创建 `AATSResearchOS` 独立仓库、`main` 分支和独立配置；
- 建立 `src/` 布局、最小 CI、lint/test 配置、secret scan 和禁止依赖检查；
- 建立 G0 治理和架构文档；
- 用标准库实现不可变领域合同与本地 content-addressed raw store；
- 使用合成 fixtures 和临时目录测试；
- 在 AATS 中形成 Legacy Freeze backlog 分类和本任务交付记录；
- 对全部产物执行代码审查和静态验证。

### 2.2 明确非范围

- 不创建或读取 OKX/Binance API key；
- 不发起 WebSocket/REST 市场数据采集；
- 不创建长期对象存储、云资源、数据库或付费数据订阅；
- 不导入 Legacy mutable 数据；
- 不实现策略、特征、回测、候选、参数发布或 UI；
- 不修改 AATS Runtime、部署脚本、profile、数据库或现有采集器；
- 不把 AI、模板、未签许可意见或代码测试当作人类 G0 签字。

## 3. 模块职责与领域模型

### 3.1 包职责

| 模块 | 职责 | 禁止职责 |
| --- | --- | --- |
| `domain.instrument` | 版本化合约、单位、有效区间与金融恒等式 | 查询账户或下单 |
| `domain.events` | Canonical Event Envelope、来源原生能力和四类时间 | 猜测缺失时间/序列 |
| `storage.immutable_raw` | 内容寻址、manifest、原子发布、恢复校验 | 覆盖或就地修订 raw |
| `governance` 文档 | 角色、许可、预算、威胁、Freeze、Gate 证据 | 代替人类签字 |
| `tests` | 合成黄金样例、攻击测试和确定性验证 | 访问网络或真实凭证 |

### 3.2 核心领域对象

- `InstrumentVersion`：venue、instrument、base/quote/settle、linear/inverse、contract value、
  contract value currency、tick、lot、min size、有效区间、来源 schema 和版本；
- `EndpointCapability`：source/endpoint/channel、原生时间、sequence/update ID、snapshot/delta、
  provider checksum 能力；
- `CanonicalEventEnvelope`：event ID、source、instrument version、event type、event/publish/
  receive/available time、sequence、payload 和 raw digest；
- `RawObjectManifest`：schema、source/endpoint、receive/available 时间、content type、payload digest、
  manifest digest、大小和可选 provider checksum；当前没有独立 `created_at` 字段。

## 4. 输入、输出与接口

### 4.1 输入

- 合成 JSON payload；
- 显式 timezone-aware UTC 时间；
- 已验证的 InstrumentVersion；
- 调用者提供的 source/endpoint capability；
- 本地临时目录中的 raw bytes。

### 4.2 输出

- 不可变领域对象；
- canonical JSON bytes 与 SHA-256 fingerprint；
- content-addressed raw object；
- 与对象一一对应的 canonical manifest；
- 恢复后逐字节和摘要一致的 payload。

### 4.3 失败接口

所有失败使用稳定异常类型与 reason code。未知时间语义、naive datetime、无效 Decimal、重叠
InstrumentVersion、digest 冲突、既有路径内容不一致和 manifest 不匹配必须失败关闭。

## 5. 数据库、约束与一致性

G0 不创建数据库。控制面数据库留到正式 G1 且需单独 ADR。当前 raw store 使用本地文件模拟未来对象
存储合同：

- object key 由 SHA-256 决定；
- object 与 manifest 都采用 create-if-absent；
- 同 digest、同内容重复写是幂等成功；
- 同路径、不同内容为完整性错误；
- 临时文件写完并 `fsync` 后，以不可覆盖的原子 hard link 发布；同内容竞争路径也复核并同步目录；
- Windows 没有通过本适配器证明目录 fsync/掉电耐久性，因此它只属于 G0 行为参考；
- manifest 只有在 raw 对象稳定后发布；
- 恢复必须重新计算 object 和 manifest digest；
- 不支持 delete、overwrite 或 in-place correction。

## 6. 事务、并发与生命周期

本地实现以单主机多进程安全为目标：

```text
NEW
-> RAW_TEMP_WRITTEN
-> RAW_PUBLISHED
-> MANIFEST_PUBLISHED
-> VERIFIED
```

重复写以内容身份幂等；并发 create 由排他创建和最终内容复核解决。崩溃留下的临时文件不进入目录
真源，后续恢复工具只能清理超过 retention 的临时对象，且该清理能力不在本轮实现。

## 7. 权限、认证与数据安全

- 仓库不包含 `.env`、API key、账户、下单或 Runtime credential；
- 禁止依赖、导入或出现 `aats` Runtime、交易执行、active parameter 和 live secret 路径；
- G0 代码没有网络客户端和后台 daemon；
- CI 使用最小权限，只读源码，不能访问 secrets 或部署环境；
- 角色实名登记使用仓库外私有签字册；仓库只保存角色 ID、状态、digest 和非敏感证明；
- `IRR-01`、`DLR-01`、`SEC-01` 的模板不能预填虚构姓名或签字。

## 8. 错误处理与幂等

- 校验错误不得部分发布 manifest；
- 重试不能改变 content digest；
- 缺失 source-native 字段保留 `null` 和稳定 reason code，不合成 sequence/exchange time；
- 已存在 raw object 只有在逐字节一致时才算幂等；
- manifest 与 payload 不一致时恢复失败，不尝试“修复”源对象；
- 所有异常信息不得包含 payload、secret 或本机私有路径之外的敏感信息。

## 9. 缓存与性能

G0 不引入缓存。验收只要求：

- 1 MiB 合成 payload 的写入、重复写和恢复可完成；
- fingerprint 对相同 input/config 稳定；
- 内容寻址避免同 payload 重复占用；
- 性能数字只作为本机基线，不外推为 168 小时采集容量。

## 10. 日志、监控与审计

本轮没有实现独立的结构化 audit record 类型或监控服务；当前可审计证据来自 content-addressed
manifest、测试输出和治理报告。未来 G1 若实现 audit record，至少应记录 operation、object digest、
manifest digest、source、schema、result、reason code 和 UTC time；不得记录完整 raw payload 或凭证。

未来 G1 的 collector continuity、route health、gap 和 24h rollover 指标保留在计划中，不在 G0 用
合成测试伪装为现场证据。

## 11. 测试策略

至少覆盖：

- InstrumentVersion 金融单位与有效期；
- linear/inverse 合约名义价值黄金样例；
- timezone-aware、事件时间顺序和 source capability；
- 缺失原生字段不合成；
- canonical JSON/fingerprint 与字典顺序无关；
- raw create、重复幂等、冲突、manifest、恢复与篡改检测；
- 并发同内容写入；
- secret pattern、禁止依赖和网络调用不存在；
- 文档本地链接、G0 状态与未签条件一致。

## 12. 迁移、回滚与兼容

- 现有 AATS 不引用新仓库，因此回滚是停止使用并保留审计证据；
- 不删除或修改 Legacy 数据；
- 不复制现有 101 张 RDP 表；
- 未来迁移必须通过只读 crosswalk 和 Migration Manifest；
- 新仓库若未通过 G0，只保留为隔离 bootstrap，不进入 AATS 构建或部署链。

## 13. 配置与环境隔离

- Python 目标：3.12+；
- G0 runtime dependencies 为零，测试/lint 依赖单独声明；
- 所有路径由调用者显式传入，不使用 AATS `.env` 或默认数据库；
- 测试只使用临时目录；
- 不创建 Compose、WSL2 service、计划任务或开机自启项；
- 任何网络接入配置只可在 G0 PASS 后进入正式 G1 变更。

## 14. 代码组织与依赖

```text
AATSResearchOS/
  src/aats_research_os/domain/
  src/aats_research_os/storage/
  tests/unit/
  tests/architecture/
  docs/architecture/
  governance/
  docs/operations/
  .github/workflows/
```

优先标准库 `dataclasses`、`datetime`、`decimal`、`enum`、`hashlib`、`json`、`pathlib`。不引入
Kafka、Ray、Iceberg、数据库驱动、交易所 SDK 或 Web 框架。

## 15. 文档与操作说明

新仓库 README 必须首先说明：G0 bootstrap、无 live、无采集、无 G0 PASS。治理目录必须区分：

- 已批准决策；
- 未签模板；
- 工程检查；
- 人类 gate 结论；
- 运行时未知项。

G0 不提供采集运行手册；只提供本地测试和 Closure Packet 组装说明。

## 16. 部署与验收

本轮没有部署。验收命令只能执行格式、lint、unit、architecture、secret/forbidden dependency 和文档
检查。以下全部通过只表示 bootstrap 工程完成，不表示 G0 PASS：

- 独立 Git 根和分支存在；
- AATS 与 Research OS 没有 Git nesting、共享写路径或依赖；
- 合成测试、lint、类型/构建检查通过；
- 没有 secret、网络采集器、Runtime 写客户端；
- G0 文档齐全且签字状态真实；
- 独立代码审查无 P0/P1。

## 17. Gate、停止线与完成定义

### 17.1 本任务完成

工程产物、测试和文档通过，并在 AATS 交付记录中明确：

- `IRR-01` 是否已实名；
- Owner/PE/SEC 是否已在私有册接受职责；
- OKX/Binance 许可是否获人类签字；
- G0 Gate 是 `OPEN`、`PASSED` 或 `NO-GO`；
- 正式 G1 是否仍锁定。

### 17.2 G0 仍不可关闭的条件

模板、AI 审查或代码通过不能替代：

- 独立人类 `IRR-01` 任命和冲突披露；
- `DLR-01` 对实际实体、地域和用途的书面结论；
- PO/DO/QO/EO/PE/SEC 的私有责任接受；
- PO/DO/QO/EO/PE/IRR/DLR 对同一 Closure Packet digest 的角色签字，C04 另有 SEC 签字。

未满足时，必须停止在 G0 bootstrap；不得启动 OKX 前瞻采集、Binance 168 小时探针或正式 G1。
