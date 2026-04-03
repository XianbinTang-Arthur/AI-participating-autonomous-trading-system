# Task 206 - Shrink Deprecated Auto Execution Key Reads into Settings

## Business objectives and boundaries
- Ensure only the settings migration layer reads `strategy_sleeve_auto_parallel_enabled`.
- Keep backward compatibility for legacy config/env inputs during this intermediate phase only.
- Historical note: the legacy key was removed in the subsequent cleanup phase.

## Module responsibilities and domain model
- `settings.py`
  - Owns legacy-key interpretation and exposes normalized accessors.
- `config.py`
  - Uses settings accessors for startup warnings and runtime metadata.
- `query_service.py`
  - Uses settings accessors for compatibility payloads.
- `sleeve_execution_permission.py`
  - Uses settings accessors for guard diagnostics.

## Retirement effect
- Business code no longer directly reads `strategy_sleeve_auto_parallel_enabled`.
- Historical note: the legacy key later stopped being accepted as a settings input.

## Testing strategy
- Add/update settings tests for:
  - config source
  - deprecated key name
  - compatibility value accessors
- Re-run the narrow runtime integration that surfaces deprecated-key diagnostics.

## Acceptance criteria
- All direct reads of `settings.strategy_sleeve_auto_parallel_enabled` outside `settings.py` are removed.
- Runtime/operator compatibility payloads still expose the same migration diagnostics through settings accessors.
