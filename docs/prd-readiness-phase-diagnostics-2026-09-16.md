# Opt-in readiness phase attribution

Staging sustained readiness is not yet proven: repeated full readiness requests timed out, while the container probe alternated between two-second failures and quick successes. Separate-process DB/Redis checks were quick after a slow connection open. These measurements do not identify the serving process's blocked phase. Production remains on hold.

Add diagnostics only when ACP_READINESS_PHASE_DIAGNOSTICS=1. Record allowlisted phase names and finite elapsed timing, with a random request correlation ID; no SQL, parameters, exceptions, credentials, source names, document names or response content. Emit a bounded pending-phase event before a stalled request completes. Bound tracked requests, events per request/process and tracking duration; clean up on completion/failure/cancellation. Defaults are off and readiness payload/status/deadline, admission, pooling and routing behavior remain unchanged.

Capture request dispatch, worker heartbeat, queue summary/claimable count, roles, Redis and optional vision. Nested PostgreSQL markers cover admission, pool checkout, query, commit and return. Pool checkout includes both library lock wait and possible physical connection establishment; do not label either separately without measurement. The optional Ollama cold probe has an existing configured90s timeout, not a demonstrated live cause.

Offline tests prove phase attribution for blocked optional vision, DB checkout and request dispatch; event bounds and cancellation/failure cleanup; labels-only privacy; disabled compatibility. Parent deploys a green diagnostics build to staging in a controlled window before attribution or remediation. Keep PR2082 separate.
