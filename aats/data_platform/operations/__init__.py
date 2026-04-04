"""RDP Operations 模块.

部署、调度与可靠性:
  - scheduler / workflow_dispatcher: 统一调度入口
  - failure_registry / retry_manager: 失败记录与补跑
  - alerting / reliability_checks: 告警与可靠性观察
  - environment_guard: 环境隔离
"""
