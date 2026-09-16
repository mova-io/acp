# ACP managed Redis cutover

Prepared 14 September 2026. Production currently uses a reachable single self-hosted Redis container. PostgreSQL remains the durable scan/job/evidence authority; Redis carries transient credentials, admission state and event streams.

## Target and cost

`deploy/public/managed-redis.json` provisions Azure Managed Redis Balanced_B0 in East US 2, with replication/high availability, TLS, no disk persistence and no eviction. `NoCluster` preserves the current ordinary Redis client and multi-key admission commands; database index must be zero. The current Container Apps environment has no virtual network, so this first target uses authenticated TLS on the public endpoint. Private networking requires a separate network rollout; this template does not claim a private endpoint.

Microsoft's retail pricing API on 14 September returned B0 at USD 0.016/hour for East US 2 (USD 11.68 at 730 hours), before agreement/tax and any separately billed high-availability capacity. Verify the Azure deployment estimate before provisioning; do not treat this as an account invoice. Sources: [pricing](https://azure.microsoft.com/en-us/pricing/details/managed-redis/), [resource schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.cache/2025-07-01/redisenterprise), [database schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.cache/2025-07-01/redisenterprise/databases).

## Preflight

1. Register Microsoft.Cache and validate the template. Provision the target separately; do not change application secrets or endpoints yet.
2. In the running app, inspect source Redis memory, logical database index and key types without printing keys or values. B0 is unsuitable if memory cannot fit with headroom. Verify target TLS and authentication from that same runtime, not only a laptop.
3. Confirm production `/readyz` is healthy and its durable queue has zero active/claimable work immediately before cutover. Respect the existing deployment guard. Never override it for this migration.
4. Retain old Redis, its secret references and old application revisions for rollback. Do not print connection strings, access keys, credential values or raw DUMP payloads.

## Cutover transaction

API and workers are separate processes. Updating them independently while requests arrive can send a scan's credentials to one cache and have its worker read the other. Therefore an empty queue alone is insufficient.

1. Temporarily prevent new external requests and job admission. Confirm no active work remains, and wait for existing request handlers to finish. Record the start of the maintenance window.
2. Copy the quiescent source's database 0 keyspace to the empty target, retaining remaining TTLs and key types. Never reset credential TTLs to a fresh hour. Compare typed content and remaining TTLs; Redis DUMP serialization can differ across server versions, so matching DUMP bytes alone is not a valid verification method.
3. Add a **new** secret name on every consuming service (`acp-app`, legacy `acp-worker`, `acp-discovery`, `acp-assess`, `acp-remediate`). Keep old secret names unchanged. Switch each new revision's REDIS_URL to that new secret. Check separately whether vision admission uses a dedicated Redis URL; change it only if it actually points to the source being migrated.
4. Keep external requests held until every consumer has the same target, TLS/authentication works, all worker roles are alive, and `/readyz` reports `topology=managed` and `tls=true` without degradation.
5. Restore external requests, reconnect SSE clients and verify owner-scoped existing sessions and a fresh authorized scan. Recorded activity history remains in PostgreSQL even if a transient connection resets.

If any copy, authentication, health or session check fails, restore old revision/secret references before reopening requests. Do not retire old Redis until rollback checks and a full owner-authorized workflow succeed.

## Live validation boundaries

Jeremy's latest 147-file scan completed assessment and was explicitly erased at 17:51:30 UTC on 14 September; it cannot be reused as authority for remediation or publication. Deva's retained 85-file run has 75 published receipts with matching corrected-file hashes and 10 missing delivery records. Her source credentials were unavailable during the audit. A Redis move cannot recreate erased scans or expired credentials, and does not authorize publication of her recovered review-only document.
