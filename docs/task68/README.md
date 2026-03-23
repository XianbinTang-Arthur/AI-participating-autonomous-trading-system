# Task68：试盘复盘留痕

## 目标
- 把当前复盘包从“可查看”推进到“可留痕、可回看”。
- 让每次正式周审查都能形成一条 operator action 记录，便于后续追踪和复盘。

## 已有能力
- 已有统一复盘对象：`/reports/trial-review-packet`
- 已有 operator action 事件流

## 本轮补充
- 新增人工记录接口：`POST /system/trial-review/record`
- 新增复盘历史接口：`GET /reports/trial-review-history`
- 策略页新增“记录本次复盘”按钮
- 复盘包中直接展示最近一次复盘留痕

## 作用
- 避免试盘评审只停留在临时结论
- 为后续周审查、缩容、暂停、放量提供可追溯依据
