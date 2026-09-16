# Scheduled discovery, lifecycle and assessment

The durable scheduled path currently runs monolithic analysis before lifecycle evaluation. The terminal guard in #2087 protects prior terminal state, but newly matching Archive/Delete rules still need existing lifecycle evaluation before content access.

Reuse `handlers._scan_discover` for metadata-only discovery, persisted inventory, immutable lifecycle evaluations and the frozen scan scope. Then enqueue the existing `scan_assess` through `Store.enqueue_stage_batch`, bound to the discovery snapshot. Keep candidate overrides off for scheduled work and retain the existing terminal exclusion floor. Do not duplicate a rule evaluator or enable new model/provider features.

Use the existing revision-fenced successor pattern from `release_continuation_store.save` and `automatic_release_store.save`. Extend the existing `schedule_occurrences` row with narrowly scoped execution context, revision, scan identity, immutable deadline, cumulative failures and successor identity (schema56, coordinated after schema55). No new generic worker framework or mutable retry limit.

At each tick, atomically compare the occurrence revision and current job's worker/attempt ownership, mark that completed tick done, and insert exactly one successor `scheduled_sweep` with the same `scheduled_owner`. The existing owner uniqueness constraint remains satisfied without an admission gap. Fresh tick jobs carry ordinary retry limits; actual errors accumulate across the logical occurrence and can never be refunded by waiting. Waiting schedules no more than one tick per fixed interval, with a persisted overall deadline and a bounded wake count. Completion, cancellation or deadline expiry stops successors and emits the occurrence result/notification once.

Create immutable server-owned context before discovery: tenant, source/provider, exact source scope, occurrence, scan id, source-auth mode and feature policy; no credentials. Successor payloads contain only references/revision. Refreshing a claim resumes that accepted execution, preserving timezone/catchup/watermarks and avoiding new work admission or source widening. A changed future schedule does not rebind already accepted input.

Scheduled Drive ADC authorization must come from this stored context, owned scan and claimed job plus frozen item/account membership. Ordinary missing GIS tokens remain errors. A client payload flag or guessed continuation/scan id never supplies ADC authority. SharePoint app authorization stays bound to the accepted library/drive metadata. Any late terminal-state update must also block queued content before cache/download and retain skipped/exclusion evidence.

| Regression | Required evidence |
| --- | --- |
| Single shared worker | Parent tick finishes; assessment/finalizer advance with no waiting worker deadlock |
| Matching archive/delete rule | Real evaluator writes immutable rule evidence; flagged sources never reach cache/download/analysis |
| Source authentication | Ordinary missing token, forged context, cross-owner/provider/scope/account/item denied before ADC creation |
| Handoff | Transaction rollback preserves current owner token; duplicate/stale workers create one successor and cannot complete a replacement claim |
| Retry/timeout | Waiting does not spend error budget; cumulative actual failures and fixed deadline stop the logical execution |
| Replay | Same occurrence/scan/snapshot reused; no duplicate evaluation, analysis admission or completion notification |
| Cancellation/draining | No new source work after cancellation; deployment replay keeps context and snapshot without final failure |
| Late terminal update | A queued file archived/deleted after selection records exclusion without content access |
| Cadence | Existing timezone, catchup, owner fairness and notification-policy regressions remain green |

Alternative rejected: hold the scheduled worker while queued assessment runs, which can deadlock a shared worker pool. Another rejected alternative is endlessly increasing a retry limit for waiting. The revision-fenced durable successor is already established in this application.

Initial scope is the real durable scheduled handler. Direct internal calls without a durable job and synchronous/threaded legacy manual scans remain separately identified gaps for new-rule preevaluation; #2087 still protects their prior terminal sources. No complete lifecycle compliance claim until those paths are addressed.

## Concrete bounds and claim tests

Freeze a six-hour execution deadline at acceptance, with a configuration range of five minutes to twenty-four hours. Waiting ticks use a minimum thirty-second delay and a persisted wake ceiling of ceil(deadline duration / 30) + 2. Neither retries nor successor handoffs extend the deadline. The logical actual-error budget is three across the entire occurrence; record each job/attempt failure at most once and preserve ordinary per-job retry limits. Existing child-job errors remain explicitly represented rather than translated into successful analysis.

Persistence regression fixtures must use real Store claims. Accept one occurrence, then hand off revision zero; assert the old tick done, exactly one same-owner successor queued, admission watermark unchanged and no claimable ownership gap. Inject successor insertion failure and assert transaction rollback restores the old running claim/revision. Reclaimed/duplicate attempts must not hand off or change accepted scope/auth context. Advance the clock past the deadline and assert no successor, bounded terminal outcome and one occurrence notification.

Source authorization tests must prove that immutable server-owned acceptance plus a current claimed descendant job are required: mutable payload ADC flags, guessed occurrence IDs, a foreign scan owner, changed provider, unlisted item and a different account/drive fail before client construction. Missing-token ordinary discovery stays denied. Complete frozen item binding must include the provider account/drive and the exact discovered item; account identity is verified before scheduled Drive content access. Explicit full reset must erase acceptance context with schedule_occurrences.

The orchestration fixture runs one actual generic worker repeatedly with providers and content engines replaced by counters. Real discovery lifecycle rules flag candidates, one assessable Active fixture progresses to finalization, ticks return between phases, and a queued late-terminal fixture reaches the independent content gate. A deployment retry resumes the same accepted scan/input snapshot and cannot duplicate evaluation or fan-out. Preserve existing schedule timezone/catchup, owner uniqueness/fairness, cancellation and notification policy tests.


## Descendant authority and completion

Schema 56 adds the six occurrence fields and one `jobs.scheduled_execution_binding` column. Schema 55's lifecycle DDL and ledger behavior are unchanged. Only the claimed accepted assessment batch can write descendant bindings, within the same transaction that admits its entire frozen fan-out. Each binding contains occurrence, owner, root scan, discovery snapshot, batch, actual parent claim and digests of immutable accepted authority and the actual stored payload. It contains no credentials. Ordinary API payloads cannot set this column. A replay reuses admitted job IDs and the original file count, and rejects changed stored payloads.

Drive content authorization checks the fresh ADC account against the complete frozen discovered account/item identity. SharePoint acceptance freezes its configured library and an identity digest of tenant/client IDs; library/app changes and out-of-library scope fail before obtaining an app token. Future schedule edits do not alter accepted scope or policy. Full reset removes occurrence context and jobs through existing erasure tables.

The six-hour default introduces a maximum logical duration where the old monolithic path had no fixed overall ceiling; operators can select five minutes through twenty-four hours with `ACP_SCHEDULED_EXECUTION_TIMEOUT_S`. This bound is immutable after acceptance. Deployment draining spends no error budget. Three real failures, exhausted ordinary tick attempts, child failure or deadline expiry terminate the occurrence and cancel remaining work. The final tick failure path closes the occurrence under the exact worker/attempt claim even when its handler could not persist a logical error count. Complete database unavailability prevents any completion write; existing retry/reclaim recovery must regain database access.

Legacy global schedules reserve the existing owner uniqueness slot using a singleton sentinel, preserving global last-sweep reporting and user-notification behavior across their staged execution. Per-user schedules retain their existing watermarks, cadence and notification policy.
