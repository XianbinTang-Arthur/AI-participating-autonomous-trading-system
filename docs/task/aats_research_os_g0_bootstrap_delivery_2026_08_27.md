# AATS Research OS G0 Bootstrap 交付记录

> 文档状态：截至核对日的交付快照 / G0 工程准备已完成 / G0 人类收口未启动
> 核对日期：2026-08-27
> AATS 基线：`main@40dc6817861a1ddfd92cc8a01d2b9ce87af523aa`
> 独立工作树：`D:\文件\project\AATSResearchOS`
> 独立工程基线：`main@22a465b06cd731a27cf92154190b1b480fa84d2b`（tree `8b3460fd8317b0b1f13a0e3fa4f5d133589b6e41`）
> Gate 真值：`G0_OPEN / C01-C06 OPEN / G1_LOCKED`
> 当前状态真源：独立仓库 `governance/gates/g0/current.json` 与 `scripts/verify_g0_readiness.py`

## 1. 结论

已建立独立、无凭证、零 Runtime 依赖的 Research OS 本地 Git 骨架，并完成 G0 可由工程实施的
合同、治理模板、静态防线、合成测试和独立复审。该结果不等于 G0 正式通过：十个工作日的真人
closure 尚无已验证 PO 绑定 kickoff/日历，实名、签字、许可、预算事实、现场 IAM/remote 和可信
验签实现均未完成，因此未启动 G1、交易所连接、OKX 前瞻采集或 Binance 168 小时探针。

独立仓库已形成上述本地 root commit，但仍无 remote。本文不把“存在本地 commit”误写成“远端
隔离、分支保护或 CI 已现场验证”。

## 2. 已交付工程产物

### 2.1 独立仓库与供应链边界

- 独立 `AATSResearchOS` Git 根、`src/` 布局、Python 3.12+ 元数据和 hash-locked CI 依赖；
- Runtime dependencies 为零，无 HTTP/WebSocket、数据库、交易 SDK、账户、下单或后台 daemon；
- CI 设计为 Windows/Linux × Python 3.12/3.13/3.14，只允许固定 runner、精确 `contents: read`、
  两个固定 SHA Action；触发器、单一 job、步骤顺序和 `run` 命令均使用解析后的完整语义白名单，
  静态策略同时阻断写权限、secrets/GitHub token、环境部署和非白名单 Action；
- `.env*`、secret、AATS import/Runtime write/private endpoint 的负向扫描；
- 本地尚无 remote CI 运行证据，六组合只属于配置，不属于已执行事实。

### 2.2 领域与证据合同

- `InstrumentVersion`：显式 venue、合约类型、币种、quantity/lot/min-size unit、有效期和来源 digest；
- 金融换算固定为 `instrument-arithmetic/v1`、96 位、`ROUND_HALF_EVEN`，策略 ID 进入 version identity；
  等值 Decimal 尾随零、外部 ambient context 和长正负数量已有 literal golden/反例测试；
- `EndpointCapability` 与 18 条 OKX/Binance 技术能力草案：原生时间、update/trade ID、checksum、
  RPI、历史边界、显式 null reason；全部 row/binding 字段执行强类型、非空、枚举和完整 ID 集校验；
  固定六项反误报合同及 root/row/binding 全部进入来源合同 digest，未知或漂移 schema 失败关闭；
- `CanonicalEventEnvelope`：capability、Instrument reference、Raw lineage、原生字段和值/缺失原因、
  确定性 event ID；
- `ImmutableRawStore`：byte-exact content address、observation manifest、不可覆盖 hard-link 发布、
  POSIX 目录同步、幂等竞争、恢复复验；provider SHA-256 的 `VERIFIED` 只能由实际 Raw bytes 计算，
  序列化自报状态不能被公共 API 信任；
- 本地文件适配器只是 G0 行为参考，尤其不证明 Windows 掉电耐久、对象锁、retention 或 G1 对象
  存储能力。

### 2.3 G0 治理骨架

- 公开角色册与仓库外私有责任册 pointer；当前 PO/DO/QO/EO/PE/SEC 为 `UNVERIFIED`，IRR/DLR
  为 `UNASSIGNED`；
- 三项 License Matrix 已完整表达主体、地域、用途、保存/删除/再分发和重审条件，但结论全部
  `CONDITIONAL`，不是采集许可；
- Threat Model 覆盖资产、信任边界、T01-T20、停止/恢复和证据要求，状态仍为
  `DRAFT / UNSIGNED / C04 OPEN`；
- G0-G4 现金 base USD 96,000、reserve USD 24,000、硬上限 USD 120,000 与 29 FTE-month 上限已
  编码；真实 actual/commit/forecast 保持 `null/UNVERIFIED`，不把未知写成 0；
- Legacy Freeze 已分类并纳入本文件所在 AATS 提交；独立仓 pointer 尚未绑定该 evidence commit，
  也没有 remote enforcement digest；
- 本地 Gate 校验器硬拒绝任何 `G0_PASSED`。调用方 callback 或 JSON 自报 `VERIFIED` 不能成为
  trust root；正式收口必须先选择并独立审查具体签名、公钥、canonical message、证据/报告 schema
  与验证实现，再以单独变更接入。

## 3. 与实施任务书的实际差异

以下差异是审查后主动收紧，不是遗漏后继续冒称完成：

| 任务书原设计 | 当前实现事实 |
| --- | --- |
| Manifest 含“创建时间” | 实际为 receive/available time、content type、payload size/digest；无独立 `created_at` |
| 临时文件后 atomic rename | 实际为不可覆盖 atomic hard link；同内容竞争复核并同步目录 |
| 已定义结构化 audit record | 未实现独立 audit record 类型；当前证据为 manifest、测试和治理报告 |
| `docs/governance/` | 治理代码/状态位于仓库根 `governance/`，架构/计划/操作说明位于 `docs/` |

## 4. 验证与独立复审

本机环境为 Windows、Python 3.14；全部测试只使用合成数据和仓库内受控临时目录：

- Ruff 全仓检查通过；
- Pytest 全量 86 项通过；
- hash lock 的 `--require-hashes --dry-run --ignore-installed` 通过；
- G0 readiness：`machine_contract_state=PASS`，但 `formal_gate_state=G0_OPEN`、
  `g1_unlocked=false`，C01-C06 全部列为 OPEN；
- 独立核心、治理和文档真实性复审已执行；已修复 Decimal identity/运算不一致、伪 checksum
  VERIFIED、幂等 fsync 窗口、Instrument reference 大小写双身份、quantity magnitude 歧义、Gate
  伪证据、CI 权限/secrets 绕过、YAML trigger/run 编码绕过、`governance/`/`scripts/` 禁止能力
  扫描遗漏、来源合同弱类型/根级反误报漂移与文档真值冲突；
- 项目代码与测试未发起应用/交易所网络请求；依赖安装可能访问配置的 Python 包索引。未执行
  交易所请求、网络连通/隔离验证、服务、容器、数据库、远端 CI、Linux runner、对象存储、
  故障注入或 168h。

## 5. G0 正式收口的当前阻断

1. **C01**：IRR-01 真实姓名、资格、COI、接受、私有记录 digest、key 和独立性未验证；
2. **C02**：PO/DO/QO/EO/PE/SEC 均未在私有责任册完成实名接受；
3. **C03**：DLR-01 未任命，三项来源许可仍为 `CONDITIONAL`；只有最终 `ALLOW` 才能关闭；
4. **C04**：Threat Model 未签，network/IAM/remote/branch protection 未现场验证，可信验签实现未接入；
5. **C05**：真实预算台账和付款/承诺/预测证据缺失；
6. **C06**：本文件所在提交可固定本地分类证据，但独立仓 pointer 尚未绑定其 digest，远端技术强制
   仍未验证。

因此十个工作日只是一份待 kickoff 的组织目标，不是从 2026-08-26/27 自动开始的倒计时，也不是
“工程绿灯后自动通过”的 SLA。

## 6. G1 解锁后首个 30 日的硬顺序

正式 G0 通过后仍须先完成 G1-0：`source_id + market_family` 绑定、Instrument catalog existence/
effective-at、typed continuity state machine、五个 composite endpoint 拆分、正式不可变对象存储、
真实 fixture、egress/clock/容量/告警和远端跨平台 CI。全部 P0/P1 关闭并冻结 commit/config/catalog/
capability digest 后，才可依许可依次启动 OKX canary 和 Binance 168 小时窗口。

Day 30 只是首个 tranche 的 `CONTINUE/REMEDIATE/STOP` 评审，不等于完整 G1 通过；完整 G1 仍服从
90 日证据门，除非后续正式决议明确修改。
