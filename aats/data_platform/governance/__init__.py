"""Phase 5: Governance / Productionization 模块.

为研究平台提供治理能力：
  artifact_index        — 索引所有已知 round / run / report / summary
  parameter_registry    — 参数版本治理（draft / candidate / frozen / deprecated）
  round_status          — 统一 round 生命周期与 active round 索引
  retry_logic           — 失败 round 重跑计划生成
  quality_monitor       — 数据 / artifact / 结果层质量巡检
  manifest_validation   — round_manifest 规范校验
"""
