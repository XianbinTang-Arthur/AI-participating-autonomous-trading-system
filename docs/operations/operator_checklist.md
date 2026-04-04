# 运维检查清单 (Operator Checklist)

## 日常巡检

### 每日 / 每次运行后

- [ ] 运行质量巡检
  ```bash
  python scripts/rdp_run_quality_monitor.py
  ```
  - 检查 health 状态: healthy / degraded / unhealthy
  - 如有 critical failure，立即排查

- [ ] 检查最近 round 状态
  ```bash
  python scripts/rdp_list_active_rounds.py
  ```
  - 确认最近 round 是否 succeeded
  - 如有 failed/partial，查看失败原因

- [ ] 检查参数注册表
  ```bash
  python scripts/rdp_freeze_parameter_set.py --action show
  ```
  - 确认是否有 frozen 参数
  - 确认当前有效参数版本

---

## 新 Round 运行前

- [ ] 确认数据窗口
  - 数据是否已 backfill 到目标时间范围
  - Gold 表是否有数据

- [ ] 确认参数版本
  - 是否需要使用 `--params-json` 注入 Phase 2 推荐参数
  - 默认参数 vs 推荐参数，是否有意为之

- [ ] 确认数据库连接
  - Phase 3 需要 live DB (或 `--replay-only`)
  - Phase 4 需要 Gold OHLCV 数据

---

## 新 Round 运行后

- [ ] 检查退出码
  - 0 = 全部成功
  - 2 = 部分成功 -> 查看哪些 combo 失败
  - 3 = 全部失败 -> 排查数据/连接问题

- [ ] 检查产物完整性
  ```bash
  python scripts/rdp_validate_artifacts.py
  ```

- [ ] 更新 artifact index
  ```bash
  python scripts/rdp_build_artifact_index.py
  ```

- [ ] 更新 active round index
  ```bash
  python scripts/rdp_list_active_rounds.py
  ```

---

## 参数管理

- [ ] 实验产出新参数时，导入到 registry
  ```bash
  python scripts/rdp_freeze_parameter_set.py --action import \
      --source <path_to_candidates.json>
  ```

- [ ] 验证通过后，冻结参数
  ```bash
  python scripts/rdp_freeze_parameter_set.py --action freeze \
      --parameter-set-id <id>
  ```

- [ ] 旧参数被替代后，标记废弃
  ```bash
  python scripts/rdp_freeze_parameter_set.py --action deprecate \
      --parameter-set-id <id>
  ```

---

## 故障排查

### opening_count = 0

1. 检查参数: `min_safe_net_edge_bps` 是否太高
2. 检查数据: 该时间窗口的 bar 数据是否存在
3. 尝试放宽参数重跑

### 全部 combo 失败 (exit code 3)

1. 检查数据库连接
2. 检查数据是否已 backfill
3. 检查 stderr 日志
4. 使用 `rdp_retry_failed_round.py --action plan` 生成诊断

### manifest 校验失败

1. 运行 `rdp_validate_artifacts.py --fix` 自动补全
2. 如果仍有 error，手动检查 manifest 结构

### 质量巡检 unhealthy

1. 查看 `quality_monitor_summary.json` 中 `passed: false` 的检查项
2. 按 category 分类处理:
   - `artifact`: 文件/目录缺失
   - `result`: 结果异常
   - `parameter`: 参数文件问题
   - `governance`: 治理文件缺失

---

## 交接须知

新接手人员应:

1. 阅读 [平台运行手册](platform_runbook.md)
2. 阅读 [Artifact 规范](artifact_conventions.md)
3. 运行质量巡检确认平台状态
4. 查看参数注册表了解当前有效参数
5. 查看 active round index 了解最近运行情况
