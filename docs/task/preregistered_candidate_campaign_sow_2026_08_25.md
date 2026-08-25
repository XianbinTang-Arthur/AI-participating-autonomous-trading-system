# 新经济假设预注册与 Development Campaign SOW

> 文档状态：已实施任务书
> 最后核对：2026-08-25（起始 HEAD `175d4afa90db24cbe208dbff5adaa2dad311a85b`；
> 研究实现 `410e3a40c910f07f0722704a25cf14e1fb376c91`；部署修复 `66be4f5c4fbb180e2a286ff7b6d3844b3064ea9f`）
> 核对范围：Research Factory 新候选预注册、v2 development batch、完整 campaign 统计
> 运行边界：只读取 Gold development 数据；不读取 test/holdout，不写运行参数，不提交订单，
> 不启动 live profile，不产生资金资格或盈利承诺。

## 1. 业务目标与边界

现有 v2 replay plan 只能由历史候选反向生成，不能证明一个全新假设在看到结果前已经固定。
本任务新增严格的 campaign 预注册入口，把经济机制、可证伪条件、Factor DSL、市场窗口、成本和
试验族规模在运行前固化，再复用现有 development 与统计门禁实际评估。

本任务不以“找到正收益”为验收条件；真实验收是所有预注册候选和失败项均被完整记录，结果
不能被事后删除、改参数或打开 holdout。若本轮没有候选通过，正确输出是淘汰并停止进入 P2。

## 2. 模块职责与领域模型

新增 `PreregisteredCampaignSpec`、`PreregisteredHypothesisSpec` 和预注册 plan builder：

- campaign spec 固定共同数据窗口、分段、成本与研究 profile；
- hypothesis spec 固定机制、假设、失效条件、容量约束、持有周期和 Factor DSL；
- builder 生成 campaign manifest、严格 proposal、hypothesis card 与可被现有 batch/campaign
  消费的 plan；
- v2 batch 负责真实 Gold development 实验；campaign evaluator 继续负责 trial count、重复
  假设、walk-forward、bootstrap、Holm 与 DSR。

## 3. 输入与输出接口

输入是受版本控制的 `configs/research_campaigns/*.json`，必须包含固定 `campaign_id`、注册时间、
市场范围、dataset version、分段比例、fee/slippage/funding 成本和全部假设卡。

输出位于显式 research artifact root 下：

- `campaign_manifest.json`；
- `proposals/<hypothesis_id>.json`；
- `hypothesis_cards/<hypothesis_id>.json`；
- `plans/<plan_id>.json`；
- 后续既有 experiment 与 campaign evidence。

所有输出不可覆盖；CLI 不读取 `.env`，生成阶段不连接数据库。

## 4. 数据库 Schema、表、索引与约束

本任务不新增或修改数据库 schema、表、索引、约束和 migration。实际 experiment 继续通过现有
只读 `GoldReplayDataSource` 访问 Gold replay bars。预注册和统计输出只写 artifact 文件。

## 5. 事务、一致性与并发

所有候选必须先整体通过结构、路径、数值、时间和 Factor DSL 校验，随后才写任何 artifact。
每个 plan 绑定 campaign manifest、proposal 与 hypothesis card 的 SHA-256。既有同名文件内容
相同视为幂等，内容不同失败关闭；并发写入不允许 last-writer-wins。

## 6. 授权、认证与数据安全

生成 CLI 无网络、数据库和交易所权限。运行 CLI 只从调用环境读取既有 `RDP_DATABASE_URL`，
不得读取或输出 `.env`。artifact 拒绝密码、token、连接串、运行参数写入、订单或 live 授权字段。

## 7. 错误处理与幂等

未知 key、重复 hypothesis ID、重复 Factor 签名、非有限成本、比例不为 1、时间无时区、窗口倒置、
路径越界、SHA 漂移、proposal/card/manifest 缺失或内容不一致均失败关闭。运行失败仍计入完整
trial count；结构失败不得生成伪完整 campaign。

## 8. 状态转换与生命周期

```text
CONFIG_VALIDATED -> PREREGISTERED -> DEVELOPMENT_RUN
DEVELOPMENT_RUN -> EXPERIMENT_FAILED | EVIDENCE_VALIDATED
EVIDENCE_VALIDATED -> STATISTICS_FAIL | STATISTICS_PASS
STATISTICS_PASS -> P2_ELIGIBLE_FOR_L2_REQUEST_ONLY
```

任何状态都保持 `capital_eligible=false`；`STATISTICS_PASS` 也不授权模拟订单、参数变更或实盘。

## 9. 缓存与性能

预注册候选为十级规模，校验和 SHA 计算有界。实际 experiment 复用现有 Gold 查询和内存因子
计算，不引入新缓存。campaign bootstrap 继续使用确定性 seed，候选规模不得通过重复参数网格
无限扩张。

## 10. 日志、监控与审计

CLI 只输出 campaign ID、plan 数、输出位置和 SHA，不打印收益序列、数据库 URL 或原始 payload。
manifest 固定输入配置 SHA、计划引用、成本、注册时间、授权边界；后续 evidence 固定所有失败项。

## 11. 测试策略

单元测试覆盖严格 schema、重复假设、确定性 plan ID、不可覆盖、SHA 篡改、历史 plan 兼容、
funding 成本进入 experiment 与 hypothesis fingerprint。随后运行最窄单测、Ruff、完整 unit，并
在 WSL2 derivatives 模拟环境执行真实 development batch/campaign。

## 12. Migration、Rollback 与兼容

历史 `format_version=1` replay plan 保持可读，缺失 `funding_bps` 时按既有 runner 默认 `0.5`
兼容。新预注册 plan 使用独立类型和来源引用。回滚可移除新增 builder/CLI；已生成 artifact 作为
失败或历史研究证据保留，禁止篡改。

## 13. 配置与环境隔离

受版本控制配置只描述 research-only 假设。实际 artifact 写入 `artifacts/research`；Windows、WSL2
和容器路径必须显式指定。development 强制 `real_factor_development`，test/holdout 固定封存。

## 14. 代码组织与依赖

领域校验与 builder 放在 `aats/data_platform/research_factory/`，薄 CLI 放在 `scripts/`，配置放在
`configs/research_campaigns/`，测试放在对应 unit 目录。不增加第三方依赖，不复制收益或统计公式。

## 15. 文档与运维手册

完成后同步收益运行手册、验收矩阵和收益差距评估，明确本轮候选、实际数据窗口、统计结果、
holdout 状态和下一门。文档必须区分代码能力、一次性运行事实和未知未来收益。

## 16. 部署与验收标准

- 新 campaign 在运行前完整预注册，所有候选有不同的 factor signature；
- plan 明确绑定 fee、slippage、funding 成本及所有来源 SHA；
- 历史 replay plan 继续通过兼容测试；
- development batch 不读取 holdout，campaign 完整计入全部计划；
- 实际结果不因失败而删除，不降低门槛，不打开 test；
- Ruff、相关单测、完整 unit 与标准 derivatives 模拟部署通过；
- 只有统计门全部通过的候选才可进入 P2 L2 request，其他候选必须淘汰。

### 实施结果

配置 `profit_candidates_v3_20260825` 在查看 development 结果前固定四个不同 Factor DSL 签名：
短周期反转、成交量确认动量、funding 拥挤反转和前一日区间突破。生成阶段写出 1 个 manifest、
4 个 proposal、4 个 hypothesis card、4 个 plan 和 registration evidence；数据库、holdout、
运行参数和订单均未访问。

WSL2 `derivatives` 模拟环境随后对同一 Gold development 窗口运行全部四个计划，并执行 2,000
次 block bootstrap（seed 7）及完整 campaign。结果为 4/4 experiment gate 失败、4 个唯一假设、
0 个代表候选通过、`capital_eligible=false`、holdout=`sealed_not_evaluated`。Campaign evidence：

`/root/aats/artifacts/research/research_factory/campaigns/profit_candidates_v3_20260825/campaign_evidence.json`

SHA-256=`a67403ace4b6197005f161ce1b88aaf42f4231341afa00ab0f2d2966f84d968a`。
因此本轮按预注册门停止，不生成 P2 L2 request，也不调整阈值重试。
