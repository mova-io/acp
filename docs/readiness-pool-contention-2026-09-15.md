# Readiness pool contention proposal

A controlled staging capture on commit5dd70c60, image `5dd70c6-1789521415`, version2026.9.15.10 measured a full-readiness request taking6.376seconds externally and6.253seconds in the serving process. Four worker roles were fresh, Release had3slots, durable queue activity was0, and Redis was reachable. The subsequent narrow probe returned503/db_check_in_flight in0.118seconds.

Serving request `d4200de34c60`, September16 01:24:17–01:24:23.896UTC:

| Completed DB phase | Count | Total milliseconds |
|---|---:|---:|
| Query | 14 | 2786.936 |
| Commit | 14 | 1301.386 |
| Return | 14 | 1627.726 |

These account for about91% of the serving span. One return took1627.194ms and completed at01:24:20.653821. A normal container probe `9e0d3e883113` spent1648.227ms checking out a connection, completing at01:24:20.653468. Role-heartbeat reads took3461.227ms overall. Redis took47.464ms. Vision phases include nested settings reads, so their totals do not establish provider HTTP latency.

The overlap is consistent with psycopg2's ThreadedConnectionPool holding its bookkeeping lock while opening a new socket, blocking putconn for an existing lease. A deterministic fixture using the real adapter/library reproduces that blockage on the baseline. This does not prove the cause of all earlier20-second timeouts.

## Proposed change

Use a narrow ThreadedConnectionPool subclass that reserves pending sockets under the existing bookkeeping lock, establishes them outside that lock, and registers or releases them under the lock. Existing leases can be returned and reused while another socket opens. In-flight reservations count toward the same maximum; keyed callers wait outside the lock for their shared lease. Shutdown rejects and closes a late socket.

Only the adapter's pool factory changes. Its ordinary/mutation admission gates, minimum/maximum connection counts, idle retention, SELECT1 probe, one-check-in-flight gate, HTTP deadline, transaction handling and failure responses remain unchanged. Physical connection establishment still has the existing driver behavior; this proposal does not claim a new connect timeout or cancellation guarantee.

An idle-only probe checkout was rejected because it would report busy when a healthy pool has room to grow. Moving socket creation instead preserves the existing readiness decision.

## Verification and limits

The baseline return-overlap regression fails; it passes with the proposed factory. Real PostgreSQL16 tests use pinned psycopg2-binary2.9.10 and a guarded disposable loopback database. They cover real sockets during growth, maximum pending reservations, returned-lease reuse, actual rejected connection establishment, interruption cleanup, shutdown/return overlap, keyed reuse, rollback and socket health, the mutation reserve, and absence of fixture sessions after cleanup. Real-library deterministic fixtures additionally cover concurrent keyed requests and admission permit cleanup.

The subclass deliberately uses the pinned driver's protected pool bookkeeping fields. CI runs the new PostgreSQL tests in both the integration selection and its no-skips check; driver upgrades must keep those tests passing. Independent review and all required CI remain necessary before merge. A controlled staging comparison must follow merge before considering production. Reducing the14 query/commit round-trips is separate work; no such batching or readiness relaxation is included.

Capture diagnostics were removed after a fresh ready/idle/Redis guard. Cleanup APIrevision965 was newest ready, and the staging workflow was restored active after another fresh guard. No diagnostic probe or deployment remained running at window release. Production was unchanged and remains on hold.
