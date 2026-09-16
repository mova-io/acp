# Monolithic scan lifecycle boundary

Current main `5dd70c60` routes the durable `scheduled_sweep` handler through `core._do_scheduled_scan` into `scanner.run_scan`. The scanner lists, reads cached source bytes or downloads, and analyzes before `save_scan` calls `add_inventory`. A local fixture confirms content-read admission without any lifecycle lookup. No provider or model calls were used.

The shared scanner now reads the exact source lifecycle lookup before cache/download/analysis. It excludes `Archived`, `Already archived`, and `Deleted`; retained restoration states are not terminal. It preserves the complete listing in `_inventory_items`, assessed-subset summary counts, and `scope.lifecycle_gate` listing/exclusion/unknown counts with per-file reasons. Lookup failures stop content access. Missing identity and absent history are explicitly unknown, never inferred from names or presented as Active.

This depends on PR #2086's `get_source_lifecycle_states(owner, provider, inventory_items)` and `lifecycle_identity.source_identity`. Tenant and provider normalize; provider account/drive and item identifiers remain opaque. The gate does not add a terminal override or widen privileges.

Shared coverage includes scheduled singleton and owner-scoped occurrences, synchronous/threaded legacy scan routes, and the legacy queued `scan` handler. Metadata-only deferred discovery and later assessment use their existing lifecycle paths. Tests exercise the actual scheduled handler and scanner without network calls, plus source-provider/namespace isolation and retained inventory.

Remaining limitation: this guard excludes prior terminal source state. It does not evaluate newly matching rules before monolithic analysis. Scheduled monolithic scans currently omit the discovery rule evaluator; synchronous/threaded legacy paths evaluate after analysis. Candidate compliance requires separate reuse of staged discovery/lifecycle/assessment with immutable evidence and existing authorization checks. No complete lifecycle-compliance claim is made here.

Explicit customer/admin erasure remains governed by the existing reset contract; after authorized erasure no hidden historical identity is inferred. Unknown identity or no retained evidence does not prove provider state.
