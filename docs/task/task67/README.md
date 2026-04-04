# Task67：试盘复盘包

## 目标
- 把已有的收益、执行异常、分层表现、试盘守护、恢复状态和放量准入结果汇总成一份统一复盘对象。
- 避免 operator 在多个报表之间来回跳转，降低周审查成本。

## 现有能力判断
- 已有相似功能：
  - `/reports/profitability-overview`
  - `/reports/execution-anomalies`
  - `/reports/strategy-segments`
  - `/reports/forward-validation`
  - `/reports/scaling-readiness`
- 当前缺失：
  - 一份可直接用于周审查的统一复盘包
  - 前端上的统一摘要视图

## 本轮实现
- 新增聚合接口：`GET /reports/trial-review-packet`
- 策略页新增“试盘复盘包”卡片
- 汇总以下信息：
  - 最近净收益与样本量
  - 费用拖累与胜率
  - 高滑点 / 慢成交异常数
  - 最强与最弱分层切片
  - 当前动作建议

## 作用
- 让值班与策略复盘直接回答三个问题：
  - 现在该继续、缩容、暂停，还是允许进入放量评审？
  - 当前收益是否被执行问题吞掉？
  - 哪些切片在赚钱，哪些切片正在拖累整体结果？
