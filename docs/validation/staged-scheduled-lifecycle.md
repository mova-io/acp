# Staged scheduled lifecycle validation

The durable scheduled handler now returns between metadata discovery, immutable lifecycle evaluation and the existing assessment stage. Its revision-fenced tick handoff preserves the same owner admission slot and fixed occurrence deadline. Scheduled assessment cannot opt into lifecycle candidates. Existing queued terminal exclusion remains independent and authoritative.

Validated offline on merged main cdd8c68b (PR 2089), with no real provider or model calls:

- 47 scheduler tests passed: real single generic worker, Drive/SharePoint, user/global schedules, candidate exclusions, late Deleted receipt preservation, cumulative failure budget, fixed deadline, atomic fan-out replay, forged/mutated authority, cancellation, full reset, deployment draining and changed SharePoint app/library denial.
- 314 focused lifecycle, schedule, cancellation and deployment regression tests passed before the final three additional denial/draining tests; the final 47 scheduler set passed after those additions.
- 4 tests passed on a disposable loopback PostgreSQL 16 database: concurrent handoffs elect one successor, successor-insert rollback preserves original claim/revision, and server descendant bindings reject changed actual payloads. The fourth fixture proves full tenant reset revokes a real claimed pre-root tick. These tests are included in both PostgreSQL CI passes.
- DDL version 56 is additive; existing lifecycle schema 55 statements remain unchanged. DDL checksum is 4a3338134fdc573bcaa2062935c2711d.

No staging or production changes were made by this task. The parent task owns final integration, required CI review and any runtime deployment. The immutable six-hour default (configurable five minutes to twenty-four hours) introduces a logical execution ceiling. Database completion writes require database availability. Already-running content reads are not cancelled retroactively. Direct internal calls without a durable claimed job retain their existing legacy path, whose prior-terminal guard is already merged; new-rule preevaluation for that separate legacy path is outside this PR.

Independent review identified a pre-root tenant-reset gap. The corrected reset transaction directly erases normalized-owner accepted occurrence authority and elected/descendant jobs before scan joins, plus scheduled stage/input lineage, notifications and sweep result. Pre/post-discovery, owner aliases, other-owner isolation and rollback fixtures pass. Live schedule configuration and watermarks are preserved.
