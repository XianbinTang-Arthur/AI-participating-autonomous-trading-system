# Weekly Review — YYYY-MM-DD

> 本模板用于每周对 AATS 项目做一次系统性复盘。目标：让 AI agent 的自主迭代**可审计、可追溯、可纠偏**。
>
> 填写人：Claude agent（自填 + 用户校核）
> 周期：每周日晚（或连续工作 ≥ 5 天时至少一次）

## 1. 本周交付（commits）
列出本周每个 commit，格式：
```
- <sha> <subject>
  - 假设/bug 证据：...
  - 效果（实测 before/after）：...
  - 风险：...
  - 状态：deployed / pending / rolled-back
```

## 2. 未完成 / 欠账
```
- <backlog item>: <原因 / 阻塞 / ETA>
```

## 3. 我做错了什么（必写）
**这一节最重要。**不许空。如果真的什么都没错，写清楚"本周无已识别错误，但持续关注 XXX"。

例子：
- 过早宣称"全绿"：SOW task109 复核时 audit agent 建议的 2 项实际是误报，我没核实就信了。
- 部署后没实测：S3 parallel_fetch 修复后只验证 warm path，没验证冷启动。
- 修复顺序错：优先做了代码审查，但用户真正卡的是 UI 体感。

## 4. 我做对了什么
避免自我夸赞，只列**有客观证据**的：
- 例：recovery=137s → 毫秒级，monitor 连续 X 分钟 fullscan=0

## 5. 实盘指标
从 Postgres / Grafana / Prometheus 采：
- 当前持仓 / 未平仓单
- 本周交易笔数 / 胜率 / 累计 PnL
- 异常事件（fill 失败、pool 耗尽、alert fire 总数）

## 6. 下周计划
按 Tier 排序（紧急/系统扫描/标杆借鉴/基础设施/欠账）。

## 7. 需要用户裁决的
列出**我不敢独自做主**的决策，等用户醒来/空时 review：
- 例：ai_operating_mode 切换到 ai_assisted 是否要做
- 例：某 SOW 是否该启动 vs 搁置

## 8. 自律纪律自查
- [ ] 所有 commit 都按 "假设/效果/验证/rollback" 写
- [ ] 所有 deploy 都跑了 regression 测试
- [ ] 所有"修好了"声明都附 before/after 数据
- [ ] 没有读凭证文件
- [ ] 没有触发下单/平仓/资金操作
- [ ] 没有改 kill switch / recovery policy 语义
- [ ] 高风险点都 pause 了等决策

如果有 ❌，说明违规，立即在本文件 **红字**写清"违反什么、后果、补救"。
