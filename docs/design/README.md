# AATS Design 历史索引

> 文档状态：现行设计提案目录索引
> 最后核对：2026-08-28（目录、状态与替代入口）

本目录保存设计提案、实施方案和阶段决策。设计稿可能未实施、部分实施、被替代或已经完成；文件存在不代表当前代码遵循该设计。

判断当前行为时，以 [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)、[`../code_review/README.md`](../code_review/README.md) 和代码/迁移/Compose/脚本为准。新设计应明确目标、非目标、安全不变量、数据迁移、兼容性、测试、回滚和审批条件。

## 现行提案

- [`rdp_derivatives_backtest_run_v1_adr_2026_08_28.md`](rdp_derivatives_backtest_run_v1_adr_2026_08_28.md)：
  RDP LF-B 首个 `BTC-USDT-SWAP independent_15m` 衍生品回测纵向切片 ADR。状态为 **Proposed**；
  LF-B1.1 纯合同/记账基础已在 `bf7a24dfe0a3` 完成本地静态验收，但 snapshot/event/reducer/publisher/
  recovery/qualification 尚未实施，也未取得实名 RDP Owner / Independent Risk Reviewer /
  Data Lineage Reviewer 批准；固定 `capital_promotion_eligible=false`，不得据此解锁资格轮、Phase 6、
  部署或真实资金行为。
