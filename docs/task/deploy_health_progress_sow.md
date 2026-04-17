# Deploy 健康检查可观测性修复

## 背景
- `scripts/deploy.sh` 在 `step_health()` 中只打印一行“健康检查（超时 90s）...”
- 随后静默轮询 gateway `/healthz` 和应用容器健康状态
- 在冷启动或慢启动场景下，操作者会误以为 deploy 卡死

## 目标
- 保持现有健康门槛不变
- 在等待期间持续输出可读的进度信息
- 明确区分 gateway 未就绪与容器未就绪
- 超时时保留详细容器状态诊断

## 方案
1. 增加 `gateway_health_ok()` helper，单独判断 gateway `/healthz`
2. 增加 `required_app_container_states_compact()` helper，输出单行容器状态摘要
3. 在 `step_health()` 中按“状态变化或每 15 秒”打印一次进度
4. 不修改 deploy 判定逻辑，只增强可观测性

## 验收
- deploy 进入健康检查后，不再出现长时间无输出
- 日志中能看到 gateway 与各应用容器的当前状态
- 超时失败时仍输出完整容器状态和日志查看提示
