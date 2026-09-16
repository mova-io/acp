# Lifecycle state audit and corrections

Baseline: main `6c522079`. All provider transports in this audit are fakes; no customer file, provider configuration, publishing destination or deployed database was changed.

## Reproduced failures

Six integrated assertions failed before changes: a verified automatic archive remained an Archive Candidate in inventory; a new winning delete rule replaced each of Archived, Deleted and Already archived; Graph item identity resolved as Google Drive identity; and Already archived was omitted from default assessment exclusions.

Two matching archive/delete rules are not two completed actions. Existing precedence correctly chooses an archive recommendation by default, permits an explicitly authorized delete override, and leaves equal-priority conflicts for review. Regression fixtures now verify a single effective recommendation and reconciled file count. No evidence establishes which exact customer file exhibited the reported symptom.

## Corrections

PR 2085 independently guards the provider namespace at approval and undo. The durable follow-up preserves terminal source state under `(normalized tenant, normalized provider, opaque account/drive namespace, opaque source item id)`. Paths and filenames never establish identity. Google Drive requires its verified account ID; Graph requires the drive ID. Opaque IDs remain case-sensitive.

Verified automatic archive and applied reviewed/unattended governance archive/delete actions stamp the inventory and effective disposition. Recommendations cannot replace terminal/exempt state, including late projection writes. Already archived is excluded from default assessment. A protected inventory row cannot be silently rebound to another source item; rejected relist rows count as failed updates.

New listings replay exact source state, including safely identified legacy terminal snapshots. Tombstones survive delta absence/reappearance, scan deletion and analytics reset because those operations do not restore provider files. Restoration receipts shadow older snapshots so historical terminal rows cannot undo an intentional restoration. Missing stable account/drive identity remains protected within the known scan and is explicitly reported as unavailable cross-scan linkage; it is never inferred from the name or another account.

Undo claims precede provider calls. A successful undo requires source trash/folder readback; its receipt and local restoration commit together. A repeated undo returns its prior receipt without new provider writes. An old undo cannot clear a newer disposition or act on a rebound inventory item. A plain Active/Reactivated status write cannot clear provider-deletion exclusion. The recommendation override endpoint continues to record disagreement without restoring or untrashing a file.

Checkout metadata is preserved when an enrichment read omits it. Automatic Graph archival rechecks live checkout/record state and blocks checkout or unreadable hold state; no checkout is cleared by this work. The existing source-specific scope limitations remain: this does not add a Graph undo route.

## Verification and limits

Owned offline integration tests cover terminal preservation, renamed rescans, tenant/provider/account/drive/item isolation, tenant/provider alias case, opaque namespace case, missing identity, source rebinding, late projections, attended/unattended archive/delete, verified/unverified undo, replay, history pruning, conflict partition and checkout.

The lifecycle/disposition/archive/delete-scan regression group passed 648 checks. The preview-breakdown suite passed 26 separately; its import-time mock of `routes` prevents safely collecting it alongside real API tests in one process. Schema/reset guards passed locally; real PostgreSQL tests require the disposable CI service and are not claimed as locally verified. The follow-up adds real PostgreSQL projection/restoration and concurrent undo-claim tests to the existing gated integration suite.

Schema revision 55 adds one table and preserves existing replicas' columns. The schema checksum guard is updated. Source lifecycle state is classified separately as safety state rather than disposable analytics or configuration.

Delta removal may mean loss of access rather than deletion. This audit therefore does not turn arbitrary removal events into a claim that a provider deleted the file. A known terminal item remains protected if it disappears and later reappears. Live customer source accuracy, provider credentials, checkout behavior and actual source operations remain unverified by the offline fixtures. Parent owns merge and deployment; staging capture hold is preserved.
