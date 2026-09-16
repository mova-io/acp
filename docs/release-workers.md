# Dedicated Release capacity

Release can run independently from remediation using the same application image and durable job store. Existing installations retain their current routing until `ACP_DEDICATED_RELEASE_WORKERS=1` is explicitly enabled.

## Initial capacity

One private Container App, one replica maximum, 1 CPU and 2 GiB memory. `ACP_WORKERS=3` reserves one slot for reports and two for publishing, packaging and release continuation. The database pool is capped at three connections. Reports cannot take the publishing slot. There are no additional model calls. This adds Azure compute cost; the model-evaluation spending limit does not act as an Azure billing cap.

Existing admission checks, source hashes, receipts, retry limits and graceful shutdown remain in force. `deliver_corrected_copy` stays with remediation because it completes remediation's own saved-copy delivery. Only explicit Release jobs move.

## Safe rollout

1. Merge and deploy the tested image with dedicated routing still off. This is also the first deployment that understands the Release heartbeat.
2. Run `deploy/public/release_worker.py` with explicit subscription, resource group, source remediation worker, new Release name, tested image and environment. Without `--apply` it only validates. Stage names must end in `-staging`; production names must not.
3. Apply in staging first. Provisioning uses the source worker's environment, secret references and registry settings. Secret values never enter logs or command arguments. The app starts without ingress or an active queue scaler; exact existing Blob Data Contributor scopes are copied to its new identity before activation. System-identity Key Vault references require explicit provisioning rather than guessing access.
4. Confirm the Release heartbeat in `/readyz`, three reported slots with no insufficient-capacity warning, matching image version, successful storage access, and a small publishing/report smoke test. Confirm delivery receipts and no duplicate copies after an interrupted-job retry.
5. Set GitHub variable `STAGING_RELEASE_WORKER` to the new app name. The normal deployment path includes that worker in image updates, environment isolation, graceful drain and role-version verification. It activates dedicated routing and removes Release types from the remediation scaler. Repeat for production using `PRODUCTION_RELEASE_WORKER` only after staging passes.
6. Observe queue waits, failed retries, memory and provider throttling. The initial replica maximum stays one. Increase it only after measuring demand and reviewing resource/billing headroom. Azure may briefly overlap revisions during rollout.

Example validation (replace IMAGE with the CI-verified image):

```sh
python3 deploy/public/release_worker.py \
  --subscription 8fab0f8f-b577-45d7-a485-ec32f73b22be \
  --resource-group mdk-accessibility \
  --source acp-remediate --name acp-release \
  --image IMAGE --environment production
```

For dry-run capacity scripts, set `ACP_DEDICATED_RELEASE_WORKERS=1` to preview the isolated remediation query. Live right-sizing reads the deployed worker flag.

## Recovery

If provisioning fails after creation, leave dedicated routing off. The existing remediation fleet continues to handle Release. Inspect the new app and its identity grants before retrying; provisioning refuses to overwrite an existing app.

To return to shared processing, clear the environment's Release-worker workflow variable and deploy with dedicated routing off. Verify the remediation queue includes Release jobs and the fleet is healthy before stopping the dedicated worker. Do not remove the worker or its code while jobs remain active. A stale Release heartbeat marks readiness degraded once dedicated routing is active.

## Automatic publishing throughput

Durable completion of remediation, approved writes, rescoring and uploads promotes the existing queued automatic continuation immediately. Duplicate events reuse that continuation; a wake arriving during execution requests immediate recovery. Twenty-second polling remains the fallback, with longer stalled-delivery checks. No event grants approval or renews stopped/expired consent.

Each automatic permission admits at most two outstanding uploads. Admission occupies a slot before a provider response exists; queued jobs and provider retries keep their slots. SharePoint appends work only to the same permission and frozen release, preserving existing jobs and work items. Unrelated, paused, cancelled or receipt-only legacy executions remain protected. Provider retry/backoff rules are unchanged. During staging validation confirm two independent authorized corrected copies upload concurrently, report capacity remains available, Stop prevents later admission, and interrupted delivery reuses its receipt. Rollout stays under the parent task's coordination after active scans and other tasks settle.

Treat 1 CPU/2Gi as an initial staging configuration, not measured adequacy. Before production cutover record peak memory/CPU, DB pool waits, provider throttles and upload latency while two uploads and a report run together. Keep one replica maximum during this validation. If capacity or delivery verification fails, follow shared-routing recovery above and drain the Release worker gracefully; retain all jobs and receipts.

## Explicit container grants

`--blob-grants-file FILE` optionally selects private container scopes instead of cloning every source Blob Data Contributor grant. The file is a JSON array of objects with exactly `scope` and `role` (`reader` or `contributor`). For production, precreate the private `release-packages` container; a container-scoped worker cannot create it at account level. Select only containers needed for the chosen Release flow, for example `sources` with reader access and `remediated`/`release-packages` with contributor access. Include other containers only if the validated flow needs them.

Every selection must be a container under an existing source Blob grant, in the source's explicit `ACP_BLOB_ACCOUNT` and requested subscription. Production selections cannot include staging containers. Conditional grants are retained only for an exact same-scope contributor clone; narrowing or changing their role is rejected rather than interpreting the condition. Default invocations still copy exact source grants and conditions.

Example entry (use the actual account/resource group and repeat for each required container):

```json
[{"scope":"/subscriptions/8fab0f8f-b577-45d7-a485-ec32f73b22be/resourceGroups/mdk-accessibility/providers/Microsoft.Storage/storageAccounts/acpremediatedstore/blobServices/default/containers/sources","role":"reader"}]
```

Validate first with `--blob-grants-file FILE` and no `--apply`. Review the file before applying. Existing source identity grants remain unchanged. Verify storage access before routing activation; an unrelated administrative source role never authorizes a selected grant.
