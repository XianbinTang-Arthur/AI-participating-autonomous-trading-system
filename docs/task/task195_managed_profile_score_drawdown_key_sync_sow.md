## Task 195: Managed Profile Score Drawdown Key Sync

### Business objectives and boundaries
- Synchronize managed derivatives strategy profile YAML files to the new independent score drawdown config key.
- Update profile-loading tests so the managed profiles explicitly prove they set the new key.
- Keep runtime backward compatibility intact; do not remove the legacy settings field from code.

### Module responsibilities and domain model
- `configs/strategy_profiles/derivatives.yaml`
  - Replace the legacy independent score-stability config key with the new score-drawdown key.
- `configs/strategy_profiles/derivatives_live.yaml`
  - Apply the same managed-profile key migration for the live derivatives profile.
- `tests/unit/test_env_profiles.py`
  - Assert the managed profiles surface the new key value after loading.

### Input/output interfaces
- Inputs:
  - managed profile YAML values
  - `load_managed_profile_values(...)`
- Outputs:
  - loaded managed profile dicts containing `strategy_hedge_independent_min_score_drawdown_bps`

### Database schema / tables / indexes / constraints
- No database changes.

### Transactions, Consistency, Concurrency
- No transactional behavior.
- Managed profile loading should remain deterministic across test runs.

### Authorization, Authentication, Data Security
- No auth or security changes.

### Error Handling and Idempotency
- Pure configuration migration.
- Safe to apply repeatedly because the YAML key is replaced, not duplicated.

### State Transition and Lifecycle
- No runtime state-machine changes.

### Caching and Performance
- No runtime performance impact.

### Logging, Monitoring, Auditing
- Managed profile inspection and downstream diagnostics will now align with the new key name.

### Testing Strategy
- Update `test_env_profiles.py` assertions for both derivatives managed profiles.
- Run the narrowest integration check that exercises configured-parameter exposure for the new key.

### Migration, Rollback, Compatibility
- No database migration.
- Runtime compatibility is preserved because code still supports the legacy field as fallback.
- Rollback is a direct config-file revert.

### Configuration and Environment Isolation
- Use `.venv\\Scripts\\python.exe` for validation.
- Do not change `.env` files in this task.

### Code Organization and Dependencies
- Keep the change scoped to managed profile YAMLs and their loader tests.

### Documentation and Operations Manual
- This SOW records the managed-profile sync step so phase-two code and project config stay aligned.

### Deployment and Acceptance Criteria
- Managed derivatives YAML files use `strategy_hedge_independent_min_score_drawdown_bps`.
- Profile-loading tests assert the new key value.
- Lint, unit tests, and the narrowest affected integration test pass.
