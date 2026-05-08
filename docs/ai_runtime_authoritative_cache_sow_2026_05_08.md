# AI Runtime Authoritative Read Cache SOW

## Context

Gateway dashboard/API read paths call `ai_runtime_status` through the decision
process because gateway does not load `ai_service`. Live logs showed the same
authoritative AI runtime status being requested by `aiRuntime`, `aiOverview`,
and `aiConfigModel` in the same refresh window, producing repeated 3-8 second
panel reads and P0 snapshot timeouts.

## Change

- Add a 20 second process-local cache for remote authoritative AI runtime reads.
- Coalesce concurrent reads with single-flight so one gateway refresh window
  emits at most one `ai_runtime_status` bridge request per runtime/client/loop.
- Invalidate this cache after AI command mutations so operator mode changes are
  reflected immediately after write-side actions.

## Non-goals

- No change to AI decision logic, strategy thresholds, order creation, or risk
  blockers.
- No change to the local `ai_runtime()` stub used by sync health/recovery paths.

## Acceptance

- `/ai/runtime`, `/ai/overview`, `/ai-config/summary`, and dashboard bundle
  agree on the same authoritative decision-process status.
- Repeated short-window reads reuse one bridge request.
- AI mutation routes clear the short-window cache.
