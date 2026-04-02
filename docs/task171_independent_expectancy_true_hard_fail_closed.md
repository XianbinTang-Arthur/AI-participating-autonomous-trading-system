# Task 171 - Independent Expectancy True Hard Fail-Closed

## Business objectives and boundaries

- Remove the last remaining "failed expectancy object" path from the `independent` family.
- Keep the existing runtime behavior for live safety: expectancy resolution failure must block new risk.
- Do not change public APIs outside the `independent` family internals and tests.

## Module responsibilities and domain model

- `independent_family.py` owns expectancy resolution and book-level trading eligibility.
- `IndependentBookExpectancy` should represent only successful expectancy resolution.
- Expectancy resolution failure should be represented as `None`, not as a fallback expectancy-shaped object.

## Input/output interfaces

- Custom `expectancy_resolver(...)` may return:
  - a valid `IndependentBookExpectancy`
  - `None`
- Any invalid return shape or mismatched leg is treated as resolution failure and normalized to `None`.

## Error handling and idempotency

- Exceptions from the custom resolver or internal expectancy computation return `None`.
- Invalid resolver outputs also return `None`.
- `_evaluate_independent_book(...)` remains the single point that translates `None` into:
  - `independent_{leg}_book_expectancy_resolution_failed`
  - blocked open / scale-in behavior

## State transition and lifecycle

- Successful resolution: normal expectancy-driven evaluation.
- Failed resolution: fail-closed for new risk, without constructing a fallback expectancy object.

## Testing strategy

- Keep the existing failure-path regression.
- Add one regression for an invalid resolver output shape to ensure it is normalized to `None -> blocked`.

## Migration, rollback, compatibility

- No schema or config migration.
- Backward compatibility is preserved for valid custom resolvers.
- Invalid custom resolvers now fail closed instead of leaking expectancy-shaped objects downstream.
