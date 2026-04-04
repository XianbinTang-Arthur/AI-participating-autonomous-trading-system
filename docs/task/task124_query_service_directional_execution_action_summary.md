# Task124 Query Service Directional Execution Action Summary

## Goal

让 `query_service.py` 输出的 `execution_action` 摘要字段在存在 `position_intent` 时保留方向层，不再继续压平成 `enter / scale_in / reduce / exit / reverse`。

## Scope

- 调整 [query_service.py](/D:/文件/project/AIParticipatingAutonomousTradingSystem/aats/services/operator/query_service.py) 的执行动作摘要归一逻辑
- 保持 `DecisionOutcome.final_action` 继续使用抽象动作层，避免扩大行为变化
- 增加最窄的 operator API 集成测试，验证 `orders / fills / execution/latest` 会返回方向化 `execution_action`

## Validation

- `ruff check aats/services/operator/query_service.py tests/integration/test_operator_api.py`
- `pytest tests/integration/test_operator_api.py -q -k "test_execution_payloads_preserve_directional_position_intent_in_execution_action_summary"`
- `ruff check .`

## Notes

- 这是摘要字段语义修复，不涉及撮合、风控或存储层写入。
- 若后续要让更多 operator 摘要字段也保留方向层，应单独审查 `final_action`、分组统计与外部消费者的兼容性。
