# Readiness phase diagnostics

`ACP_READINESS_PHASE_DIAGNOSTICS=1` enables timing logs for `/readyz` and `/probe/readyz` in the serving API process. It defaults off. A controlled staging deployment can set this flag temporarily, capture `acp.readiness.phases` events, then remove the flag. Production activation remains a separate deployment decision.

Logs contain only a random correlation ID, request kind, allowlisted phase, state elapsed milliseconds, and a capped diagnostic-failure counter. Reporter or logging failures increment the counter without retaining exceptions; the next successful event exposes it. `pending` identifies the phase still running after two seconds; `completed` or `interrupted` closes a phase. `request_dispatch` includes waiting for a synchronous handler slot. Nested DB phases cover admission, checkout, query, commit and return. `db_pool_checkout` combines library lock wait and possible connection establishment; it cannot distinguish them. Initial lazy pool creation is included in `db_admission`.

Tracking permits four concurrent requests, 128 events and 12 pending events per request, 256 events per process per 60-second window, and 120 seconds of tracking. Overflow or expiry suppresses diagnostics without rejecting or cancelling readiness. A daemon reporter observes pending phases without making dependency calls. Completion, failure and cancellation remove tracking; a timed-out probe's continuing DB thread cannot emit after its request closes.

These diagnostics do not change readiness responses, deadlines, database admission or pool sizes. Timing identifies where to investigate; it does not establish why a dependency or checkout stalled.
