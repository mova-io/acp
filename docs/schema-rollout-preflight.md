# Bounded schema preparation and startup evidence

Deployment prepares the exact pinned schema before updating any API or worker image. This addresses concurrent schema initialization during rollout. Staging showed startup exits and liveness restarts, but the failed processes' stderr was unavailable; a schema migration failure is a hypothesis, not a confirmed cause of those exits.

## Before image replacement

The deployment runner reads each target's named main container, environment, ready revision, and PostgreSQL connection privately. All roles must have the same configured database endpoint and database name. This compares configuration, not a PostgreSQL cluster identifier. Credentials and provider response bodies are never printed. The runner must already have database network access; this change does not modify firewall rules.

An already prepared schema takes the verification path without DDL. A schema-changing pin must contain the bounded migration hook. An older recovery pin can therefore use an already prepared compatible schema, but cannot migrate a behind schema with unbounded older tooling. Equal-version checksum mismatches fail closed.

Migration uses the existing PostgreSQL advisory lock and rechecks the marker after admission. Before any DDL, it tries an exclusive lock on `jobs` with `NOWAIT`, then checks queued and running jobs inside that transaction. A held reader or active durable job refuses preparation without cancelling customer work. Any later DDL failure rolls back the entire transaction and marker.

This is transaction-scoped admission, not a persistent idle lease. It does not prove that every legacy request or thread is idle. Readers arriving during migration can wait for its bounded transaction, and new jobs can arrive after commit. Schema changes still need compatibility with the serving older build; worker draining remains a separate deployment mechanism.

Each schema SQL statement receives the remaining attempt budget, and deferred constraint work is forced into a bounded statement before commit. Ordinary process initialization defaults to 60 seconds; deployment preparation uses 120 seconds plus 15 seconds for the receipt. PostgreSQL does not guarantee that `statement_timeout` interrupts every durability wait within `COMMIT`. The deployment supervisor is the hard outer bound, terminates a stalled child, and requires a successful receipt and exited child before authorizing image updates. If supervision interrupts commit, the result is treated as ambiguous and rollout stops; the next attempt verifies the durable schema marker before deciding whether DDL is needed. Transport and provider operations have separate bounded timeouts.

## Startup and evidence

The API image update atomically preserves its template and adds a missing Startup probe on `/healthz:8077`, with approximately 90 seconds for startup before liveness begins. Existing Startup probes must target that listener and provide at least that budget; incompatible probes require explicit review. Existing readiness and liveness probes remain intact. Readiness is not weakened, and the startup listener opens only after application initialization.

After replacement, deployment waits for target revisions to be ready and collects sanitized system events and `schema.boot` phases. Any observed new-revision process termination, liveness restart, schema failure, or nonzero replica restart count fails the deployment check, even if a later process is healthy. Blue/green API verification happens before traffic promotion. Normal rollout checks can fail after images have changed; this is not automatic rollback.

The retained JSON artifact contains allowlisted timestamps, reasons, exit codes, counts, schema versions, phases, and durations. Raw messages, connection strings, and provider bodies are excluded. Phase history is explicitly incomplete because current console retrieval may omit earlier failed processes. Missing evidence fails the deployment check rather than asserting clean startup.

## Validation and rollout

Disposable PostgreSQL tests cover fresh and warm preparation, checksum refusal, held-reader refusal and release, active jobs, new jobs after commit, transactional rollback, running SQL timeout, deferred constraint cancellation before commit, advisory admission timeout, concurrent migration candidates, stalled-client termination, and a real spawned receipt. Deployment tests cover named-container selection, environment/database boundaries, preserved templates, private request files, transient update retries, sanitized restart evidence, and preflight failure before image writes in normal and blue/green paths.

Merge and staging rollout require independent review and exact-head CI. Production remains on hold. Live verification must check the combined deployed version, schema marker, new-revision startup evidence, readiness, and lifecycle behavior; local tests do not establish those live outcomes.

## Transient startup reads and durable failure evidence

The first staged API process for revision `acp-app-staging--0000975` exited with code 3 after approximately six seconds while all four worker roles started cleanly. The replacement API process completed schema verification and startup. Azure retained the termination event but did not retain the failed process's console exception. Valid unchanged capacity configuration and legacy schedule data make deterministic configuration failures unlikely. A transient database failure during the API-only scheduler reload is the strongest current inference, not a confirmed root cause.

Startup now emits allowlisted begin, completion, and failure records around its main-thread phases. Scheduler reload retries once only for PostgreSQL connection, interface, or pool-admission failures. Its database reads complete before it mutates APScheduler, so that retry does not duplicate scheduler, capacity reconciler, worker, or reporter startup. Configuration and programming errors remain fail-fast.

After the store is available, a final phase failure is also written as a credential-free receipt keyed by the exact Container Apps revision. Deployment reads that receipt through a read-only database connection. A recovered final process cannot erase or hide the failed process's receipt. Receipt persistence is best-effort so evidence failure cannot replace the original exception; schema/store failures continue to use the existing `schema.boot` evidence and restart gate.
