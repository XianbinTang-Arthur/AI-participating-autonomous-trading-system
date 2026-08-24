# AATS Task 与 SOW 历史索引

> 文档状态：历史交付目录索引  
> 最后核对：2026-08-23（目录与替代入口）

本目录保存任务书、SOW、阶段设计、实施记录和交付报告。它是工程可追溯性材料，不是当前系统说明；“完成”“通过”“上线”等措辞只对文件记录的基线和验证范围成立。

## 使用规则

- 查当前行为：使用 [`../code_review/README.md`](../code_review/README.md) 与代码真源；
- 查当前操作：使用 [`../operations/README.md`](../operations/README.md) 与 [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md)；
- 查当前测试：使用 [`../testing/README.md`](../testing/README.md)；
- 引用历史任务时保留文件名、日期、commit 和验证范围；
- 新任务材料继续写入本目录，不得写入 `docs/` 根层兼容区；
- 同一任务有子目录时，以其 `README.md` 作为该任务入口。

截至 2026-08-23，本目录含数百份历史材料，另有 184 份早期 SOW/任务文件保留在 `docs/` 根层以维持路径兼容。批量迁移必须先做引用和外部审计影响评估。

## Legacy Word 文档

下列 `.docx` 是 2026-03 的初始蓝图，保留原文件名和路径以维持历史追溯；它们不是当前说明：

- [`Ai Autonomous Trading System Reference Implementation Skeleton.docx`](<Ai Autonomous Trading System Reference Implementation Skeleton.docx>)：早期 reference skeleton，包含已经漂移的目录、Kafka/Redpanda 候选、旧事件名、旧服务边界和 `run_local.py` 假设；
- [`Ai Autonomous Trading System Whitepaper V1.docx`](<Ai Autonomous Trading System Whitepaper V1.docx>)：概念白皮书 v1，包含 Kubernetes、Kafka/Redpanda/NATS 候选和早期状态机，不代表当前 WSL2 Compose、NATS、Postgres、Redis 或交易实现。

两份文件只可用于了解项目起点。当前替代入口是 [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)、[`../code_review/README.md`](../code_review/README.md) 和 [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md)。本轮只读结构检查确认其历史性质；由于文档 OOXML 缺少明确页尺寸且当前环境没有 LibreOffice，未完成视觉渲染检查，因此不对原件版式作“已优化”声明。
