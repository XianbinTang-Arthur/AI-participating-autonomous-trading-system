"""Production Workflow 模块.

将 recommendation -> approval -> apply -> observe -> rollback
固化为标准生产流程，包含:
  - Pre-apply policy gate
  - Release record
  - Observation window
  - Rollback recommendation policy
"""
