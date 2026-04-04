# Task64：周期性前向验证报表

## 目标
- 在现有收益与执行质量报表之上，增加按固定周期聚合的前向验证视图。
- 明确输出“继续试盘 / 继续观察 / 建议缩容 / 建议暂停”的审查结论。
- 让 operator API 和前端策略页都能直接读取同一份结论。

## 现有相似功能
### 已有功能
- 已有单次收益概览：
  - `/reports/profitability-overview`
- 已有执行异常报表：
  - `/reports/execution-anomalies`
- 已有分层收益报表：
  - `/reports/strategy-segments`

### 当前缺口
- 这些报表都偏“当前明细”或“按维度切片”。
- 系统还没有固定周期的审查结果，也没有直接告诉 operator 当前该继续、缩容还是暂停。

## 本次实现
### 1. 新增前向验证报表
- 新接口：
  - `GET /reports/forward-validation`
- 参数：
  - `window_days`
  - `period_count`
- 输出：
  - 最近若干个固定周期的净收益、费用拖累、滑点和慢成交比例
  - 最新周期的 verdict 和 reasons

### 2. 前向验证结论
- 最新周期会给出四类结论之一：
  - `continue`
  - `observe`
  - `shrink`
  - `pause`
- 判定依据复用当前试盘守护阈值，不再重复定义另一套标准。

### 3. 前端展示
- 策略页新增“前向验证”卡片。
- 展示：
  - 当前结论
  - 最近周期净收益
  - 费用拖累
  - 高滑点 / 慢成交比例
  - 多个周期的对比表

## 相关文件
- 后端聚合：
  - `aats/services/operator/query_service.py`
- API：
  - `aats/api/routes.py`
- 前端：
  - `aats/api/static/modules/store.js`
  - `aats/api/static/modules/views/strategy-view.js`

## 验收标准
- Operator API 能返回结构化的前向验证报告。
- 最新周期能输出明确 verdict。
- 前端策略页能渲染前向验证卡片和周期表。
