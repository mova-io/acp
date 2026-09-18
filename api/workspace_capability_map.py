"""Which capability each API route requires (PRD §11), as one table.

WHY A TABLE AND NOT 236 DECORATORS. This repo has 236 routes. Decorating each one puts the
authorization decision in 236 places, and PRD §18 asks for "100% of workflow routes mapped to a
capability" — a claim nobody can check by reading 236 files. Worse, the failure mode of the
decorator approach is silent: a route added without one is simply unprotected, and looks exactly
like a route that was deliberately left open.

So the mapping lives here, enforcement happens once (api/app.py's middleware), and
tests/test_capability_map_is_complete.py asserts that EVERY route the app actually dispatches is
either mapped or explicitly exempt with a reason. A new route fails that test until somebody
decides which it is. That is the difference between "we mapped everything" and "we can show that
everything is mapped".

ANY-OF, NOT ALL-OF, and that is the important modelling choice. A route is reachable if the caller
holds ANY of the capabilities listed for it. Routes do not belong to one tab: GET /scans/{sid}
backs Discover, Assess, Remediate and Release, and requiring `discover.view` for it would break
Assess for a Viewer whose Discover is hidden — a role the PRD's own §7 grid defines. So a shared
READ route lists the view capability of every tab that legitimately uses it, and a MUTATING route
lists the single capability that names the action. Composite approval-and-publish is explicitly
listed in ALL_OF_ROUTES and requires both grants.

    read  /scans/{sid}          -> {discover.view, assess.view, remediate.view, release.view}
    write POST /scans/{sid}/remediate -> {remediate.run}

Getting this backwards — one capability per route, chosen by whichever tab came to mind — is how
enforcement ships as a pile of 403s for roles the PRD says should work.

EXEMPT IS NOT "FORGOTTEN". Every exemption carries a reason, and the three kinds are:
  * unauthenticated by design (health, the public verify endpoint, the SPA's config)
  * identity, which must answer before a role can be known (/me, /me/access, the bootstrap)
  * identity and public endpoints that cannot depend on an already-resolved role.
    ACR routes require workspace access AND retain their per-report acr_authz checks.
"""
from __future__ import annotations

import workspace_rbac as rbac

# Shorthand for the read-side unions that appear repeatedly below.
_SCAN_READ = frozenset({"discover.view", "assess.view", "remediate.view", "release.view",
                        "monitor.view"})
_ASSESS_READ = frozenset({"assess.view", "remediate.view", "release.view"})
_REMEDIATE_READ = frozenset({"remediate.view", "release.view"})
_SOURCES_READ = frozenset({"sources.view", "discover.view"})

# (method, path) -> the capabilities that grant it; holding ANY is enough.
ROUTE_CAPABILITIES: dict[tuple[str, str], frozenset[str]] = {}


def _map(method: str, path: str, caps) -> None:
    ROUTE_CAPABILITIES[(method.upper(), path)] = frozenset(caps)


def _map_many(pairs, caps) -> None:
    for method, path in pairs:
        _map(method, path, caps)


# ── Sources (the Sources tab) ─────────────────────────────────────────────────
_map_many([
    ("GET", "/sources"), ("GET", "/sources/locations"), ("GET", "/folders"),
    ("GET", "/sharepoint/sites"), ("GET", "/sharepoint/folders"),
    ("GET", "/sharepoint/sites/{site_id:path}/drives"),
    # Reading a tenant's onboarding readiness is the same right as reading its sites: it names
    # which permissions this sign-in carries and which metadata the tenant will answer, and it
    # issues nothing but the bounded read-only probes the picker beside it already makes.
    ("GET", "/sharepoint/readiness"),
    ("GET", "/drive/folder-name"), ("GET", "/drive/adc-scopes"),
], _SOURCES_READ)
# Changing where ACP looks is `sources.manage` (PRD §5), not merely seeing the tab.
_map_many([("PUT", "/sources/locations")], {"sources.manage"})
# Uploading INTO a source is a write to the customer's estate.
_map_many([("POST", "/drive/upload"), ("POST", "/sharepoint/upload")], {"sources.manage"})

# ── Discover ──────────────────────────────────────────────────────────────────
_map_many([("POST", "/scans"), ("POST", "/discovery/preflight")], {"discover.run"})
_map_many([
    ("GET", "/scans"), ("GET", "/scans/active"), ("GET", "/inventory"),
    ("GET", "/scans/{sid}"), ("GET", "/scans/{sid}/status"), ("GET", "/scans/{sid}/live"),
    ("GET", "/scans/{sid}/events"), ("GET", "/scans/{sid}/history"),
    ("GET", "/scans/{sid}/timeline"), ("GET", "/scans/{sid}/timings"),
    ("GET", "/scans/{sid}/manifest"), ("GET", "/scans/{sid}/digest"),
    ("GET", "/scans/{sid}/inventory"), ("GET", "/scans/{sid}/inventory-diff"),
    ("GET", "/scans/{sid}/inventory.csv"), ("GET", "/scans/{sid}/exceptions.csv"),
    ("GET", "/scans/{sid}/source-status"),
    ("GET", "/scans/{sid}/queue-estimate"), ("GET", "/scans/{sid}/pii"),
    ("GET", "/scans/jobs/{job_id}"), ("GET", "/scans/{sid}/comment-counts"),
    ("GET", "/scans/{sid}/comments"), ("GET", "/decisions"), ("GET", "/scans/{sid}/decisions"),
    ("GET", "/scans/{sid}/files/{filename:path}/examined"),
    ("GET", "/scans/{sid}/files/{filename:path}/status"),
], _SCAN_READ)
# A scan's own lifecycle. Cancel is `assess.cancel` (PRD §11 names it); delete and token
# management belong to whoever may RUN discovery, since they are that scan's controls.
_map_many([("POST", "/scans/{sid}/cancel")], {"assess.cancel", "discover.run"})
_map_many([
    ("DELETE", "/scans/{sid}"), ("DELETE", "/scans/{sid}/tokens"),
    ("PUT", "/scans/{sid}/acknowledge"), ("DELETE", "/scans/{sid}/acknowledge"),
], {"discover.run"})
# Existing scan owners may refresh credentials needed to execute their allowed stage.
# This does not grant token revocation, new discovery, or access to another owner's scan.
_map_many([("POST", "/scans/{sid}/drive-token"), ("POST", "/scans/{sid}/sp-token")],
          {"discover.run", "assess.run", "remediate.run"})
_map_many([("POST", "/scans/{sid}/release/automatic/repair-source-identity")],
          {"discover.run", "assess.run", "remediate.run", "release.publish"})
_map_many([("POST", "/scans/{sid}/comments")], _SCAN_READ)   # commenting is part of reviewing
_map_many([
    ("PUT", "/scans/{sid}/decisions"), ("PUT", "/scans/{sid}/decisions/{filename:path}"),
    ("POST", "/scans/{sid}/files/{filename:path}/confirm"),
], {"discover.run", "assess.run", "remediate.run"})
# Scope rules decide WHAT is scanned — a discovery-shaped decision.
_map_many([("GET", "/scope/rules"), ("GET", "/scope/selectors")], {"discover.view"})
_map_many([("POST", "/scope/rules"), ("PATCH", "/scope/rules/{rule_id}"),
           ("DELETE", "/scope/rules/{rule_id}")], {"discover.run"})

# ── Assess ────────────────────────────────────────────────────────────────────
_map_many([("PUT", "/scans/{sid}/assessment-scope"), ("POST", "/scans/{sid}/assess"), ("POST", "/scans/{sid}/rescore")], {"assess.run"})
_map_many([
    ("GET", "/scans/{sid}/assessment-scope"),
    ("GET", "/assess/codeset"), ("GET", "/assess/eligibility"),
    ("GET", "/assess/eligibility/scoped"), ("GET", "/scans/{sid}/traces"),
    ("GET", "/scans/{sid}/ai_calls"), ("GET", "/rules"), ("GET", "/capability"),
    ("GET", "/scans/{sid}/trace/session"), ("GET", "/scans/{sid}/trace/session/data"),
    ("GET", "/scans/{sid}/trace/{kind}/exists"),
    ("GET", "/scans/{sid}/trace/file/{filename:path}/data"),
    ("GET", "/scans/{sid}/trace/file/{filename:path}/exists"),
    ("GET", "/scans/{sid}/trace/file/{filename:path}/history"),
], _ASSESS_READ)
# The evidence primitives — page renders, geometry, contrast checks. Read-only views OF a
# document, used by both the Assess worklist and the Remediate review card.
_map_many([
    ("GET", "/scans/{scan_id}/files/{filename:path}/content"),
    ("GET", "/scans/{scan_id}/files/{filename:path}/thumbnail"),
    ("GET", "/scans/{scan_id}/files/{filename:path}/page/{page}"),
    ("GET", "/scans/{scan_id}/files/{filename:path}/geometry"),
    ("GET", "/scans/{scan_id}/files/{filename:path}/source_link"),
    ("GET", "/scans/{scan_id}/files/{filename:path}/heading-outline"),
    ("GET", "/scans/{scan_id}/files/{filename:path}/table-structure"),
    ("GET", "/scans/{scan_id}/files/{filename:path}/verify-contrast"),
    ("GET", "/scans/{scan_id}/files/{filename:path}/verify-resize"),
    ("GET", "/scans/{scan_id}/files/{filename:path}/verify-pdf-contrast"),
    ("GET", "/scans/{scan_id}/files/{filename:path}/dispositions"),
    ("GET", "/scans/{scan_id}/files/{filename:path}/scanned-layout"),
], _ASSESS_READ)
_map_many([
    ("POST", "/scans/{scan_id}/files/{filename:path}/dispose"),
], {"assess.run"})
# The rubric is what "compliant" MEANS. Reading it is part of assessing; changing it is a
# platform-configuration act, which is why it sits behind Settings rather than Assess.
_map_many([("GET", "/rubric")], _ASSESS_READ)
_map_many([("PUT", "/rubric")], {"settings.view"})

# ── Remediate ─────────────────────────────────────────────────────────────────
_map_many([
    ("POST", "/scans/{sid}/remediate"),
    # Running real saved-copy detectors records verification evidence, not a content decision.
    ("POST", "/scans/{sid}/verify-saved-copy"),
    ("POST", "/scans/{scan_id}/files/{filename:path}/remediate"),
    ("POST", "/scans/{sid}/files/{filename:path}/undo-fix"),
    # Scoped recovery and run controls. `remediate.run` rather than `remediate.review`, and the
    # split is deliberate: these ACT on the estate or on the queue (a delivery writes a document
    # into a customer's library; a cancel stops work), while `remediate.review` decides the
    # content of a fix. A reviewer who may approve alt text is not thereby permitted to write to
    # SharePoint — and `release.publish` is not required either, because delivering a corrected
    # copy to the run's own mirror folder is finishing the remediation the operator started, not
    # publishing a release.
    ("POST", "/scans/{sid}/remediation/exceptions/retry-delivery"),
    ("POST", "/scans/{sid}/remediation/exceptions/retry-documents"),
    ("POST", "/scans/{sid}/remediation/cancel"),
    ("POST", "/scans/{sid}/remediation/pause"),
    ("POST", "/scans/{sid}/remediation/resume"),
], {"remediate.run"})
_map_many([
    ("GET", "/scans/{sid}/remediation/automation-policy"),
    ("GET", "/scans/{sid}/remediation/impact-policy"),
    ("GET", "/scans/{sid}/remediation/budget/{run_id}"),
    ("GET", "/scans/{sid}/remediation/waterfall/{batch_id}"),
    ("GET", "/scans/{sid}/remediation/activity"),
    ("GET", "/scans/{sid}/remediation/activity/{seq}/evidence"),
    ("GET", "/scans/{sid}/remediation/activity/{seq}/copy"),
    ("GET", "/scans/{sid}/remediation/waterfall/{batch_id}/metrics"),
    ("GET", "/scans/{sid}/remediation/insights/{batch_id}"),
    ("POST", "/scans/{sid}/remediation/impact-preview"),
    ("POST", "/scans/{sid}/remediation/impact-estimate"),
], {"remediate.view", "remediate.run"})
_map_many([
    ("POST", "/scans/{sid}/remediation/automation-policy/actions"),
    ("POST", "/scans/{sid}/remediation/impact-policy"),
], {"remediate.run"})
_map_many([
    ("POST", "/scans/{sid}/remediation/impact-assign"),
], {"remediate.review"})
# Live Ops recovery remains independently platform-admin gated in routes/system.py. The
# capability middleware still needs to name the underlying action: the dynamic stage endpoint
# can stop assess, remediate, or release work, so any one of those operating rights gets the
# request as far as the stricter admin boundary; Resume is remediation only.
_map_many([
    ("POST", "/admin/activity/workflows/{scan_id}/stages/{stage}/cancel"),
], {"discover.run", "assess.cancel", "remediate.run", "release.publish"})
_map_many([
    ("POST", "/admin/activity/workflows/{scan_id}/stages/remediate/resume"),
], {"remediate.run"})
_map_many([
    # POST because the preview evaluates an unsaved slider payload; it is still a read-only
    # calculation and grants no permission to execute remediation.
    ("POST", "/remediation/automation-policy/preview"),
    ("GET", "/scans/{sid}/remediation-status"),
    # The reconciled run snapshot reads the same run as remediation-status and carries strictly
    # more of it — filenames, SharePoint site and library names, the run's own state — so it takes
    # the same capability. Anything narrower would let the summary be read where the detail it
    # summarises cannot be.
    ("GET", "/scans/{sid}/remediation/snapshot"),
    # The exception view is the snapshot's detail — the same filenames plus the provider
    # container each corrected copy would be written to — so it takes the same capability the
    # snapshot does. Narrower would let the summary be read where the detail it summarises
    # cannot be; wider would put destination identifiers behind a view-only role's read.
    ("GET", "/scans/{sid}/remediation/exceptions"),
    # The durable-artifact inventory (PRD §12/§20.5) is the exceptions view's data with the
    # storage question added: one row per corrected document, carrying the filename AND the exact
    # location its authoritative copy was written to. That is strictly the same disclosure this
    # block's comment above already reasons about — "destination identifiers behind a view-only
    # role's read" — so it takes the same capability rather than a narrower one.
    ("GET", "/scans/{sid}/artifacts"),
    ("GET", "/scans/{sid}/remediation-diffs"),
    ("GET", "/scans/{sid}/files/{filename:path}/remediation-diffs"),
    ("GET", "/scans/{sid}/files/{filename:path}/remediation-state"),
    ("GET", "/scans/{sid}/applied-fixes"), ("GET", "/scans/{sid}/diff"),
    ("GET", "/scans/{scan_id}/files/{filename:path}/remediated"),
    ("GET", "/hitl/queue"), ("GET", "/hitl/analytics"),
    ("GET", "/hitl/queue/{item_id}/companion"),
], _REMEDIATE_READ)
# The review queue: approving or rejecting a proposed fix IS the review action (PRD §11's
# `remediate.review`), and it is deliberately not the same as running remediation.
_map_many([
    ("PUT", "/hitl/queue/{item_id}"), ("PATCH", "/hitl/queue/{item_id}/assign"),
    ("POST", "/hitl/queue/{item_id}/retry-write"),
    ("POST", "/hitl/queue/{scan_id}/auto"), ("POST", "/hitl/queue/{scan_id}/verify"),
    # Retires review items a different verified change already made moot, from the recorded
    # check of the saved copy. It changes what the review queue asks of a person, so it is a
    # review action like /auto — never a remediation run (no AI, no document write).
    ("POST", "/hitl/queue/{scan_id}/reconcile-targets"),
], {"remediate.review"})
# AI drafting assists a reviewer; it writes nothing to a document on its own.
_map_many([("GET", "/ai/suggest"), ("GET", "/ai/explain"), ("GET", "/ai/validate"),
           ("GET", "/ai/copilot")],
          {"remediate.review", "remediate.run"})

# ── Release ───────────────────────────────────────────────────────────────────
# Publishing is a GRANT (PRD §5), never implied by seeing the Release tab.
_map_many([("POST", "/scans/{sid}/publish"),
           ("POST", "/scans/{sid}/release/automatic"),
           ("POST", "/scans/{sid}/release/reports/retry"),
           ("POST", "/scans/{sid}/release/automatic/{authorization_id}/stop"),
           ("POST", "/scans/{sid}/release/automatic/{authorization_id}/resume")], {"release.publish"})
_map_many([("POST", "/scans/{sid}/release/continuation/{intent_id}/authorize")],
          {"release.publish", "remediate.review"})
_map_many([("POST", "/scans/{sid}/release/continuation/{intent_id}/resume")], {"release.publish"})
ALL_OF_ROUTES = {("POST", "/scans/{sid}/release/continuation/{intent_id}/authorize")}

_map_many([
    ("GET", "/releases"),
    ("GET", "/scans/{sid}/release"),
    ("GET", "/scans/{sid}/release/continuation"),
    ("GET", "/scans/{sid}/release/automatic"),
    ("POST", "/scans/{sid}/release/continuation/plan"),
    ("GET", "/scans/{sid}/release/manifest"),
    ("GET", "/scans/{sid}/release/reports"),
    ("GET", "/scans/{sid}/release/reports/{bundle_id}/{asset_index}"),
    # This is a read-only projection despite using POST: the selected filenames are carried in
    # the body so a large release is not constrained by URL length. It does not publish, approve,
    # or persist anything, and therefore belongs to the same release.view boundary as the
    # manifest and preview projections rather than the release.publish grant.
    ("POST", "/scans/{sid}/release/ai-provenance"),
    ("POST", "/scans/{sid}/release/preview"),
    ("POST", "/scans/{sid}/release/package/preview"),
    ("POST", "/scans/{sid}/release/package"),
    ("POST", "/scans/{sid}/release/package/prepare"),
    ("GET", "/scans/{sid}/release/package/jobs/{job_id}/download"),
], {"release.view"})

# Canonical cross-stage execution contract. Reads serve both stage cards and Live Operations;
# mutations retain the stage-specific route checks in their handlers and require an operating
# capability here rather than becoming an unclassified route.
_map_many([
    ("GET", "/workflows/{workflow_id}/stages/{stage}/executions/current"),
    ("GET", "/stage-executions/{execution_id}"),
    ("GET", "/stage-executions/{execution_id}/snapshot"),
    ("GET", "/stage-executions/{execution_id}/events"),
    ("GET", "/stage-executions/{execution_id}/queue"),
], {"operations.view", "discover.view", "assess.view", "remediate.view", "release.view"})
_map_many([("GET", "/scans/{sid}/stage-lineage")],
          {"operations.view", "discover.view", "assess.view", "remediate.view", "release.view"})
_map_many([
    ("GET", "/scans/{sid}/finding-dispositions"),
    ("GET", "/scans/{sid}/finding-dispositions/{finding_id}/events"),
], {"remediate.view", "operations.view", "release.view"})
_map_many([
    ("POST", "/workflows/{workflow_id}/stages/{stage}/executions"),
    ("POST", "/stage-executions/{execution_id}/pause"),
    ("POST", "/stage-executions/{execution_id}/resume"),
    ("POST", "/stage-executions/{execution_id}/cancel"),
    ("POST", "/stage-executions/{execution_id}/supersede"),
], {"discover.run", "assess.run", "assess.cancel", "remediate.run", "release.publish"})
_map_many([("GET", "/scans/{sid}/report.pdf")], {"release.view", "reports.export"})

# ── Report facts, the server renderer, and reviewer decisions ─────────────────
# Reading the facts a report is built from is the same right as reading the scan it describes —
# it is a projection of findings, saved changes and reviewer verdicts this workspace can already
# see through /scans/{sid}, and it issues no writes. The exact-bytes preview is listed with them
# because it is the same evidence in image form: it renders only bytes ACP already holds for a
# document in the scan, and only when the caller names their digest.
_map_many([
    ("GET", "/scans/{sid}/files/{filename:path}/report-facts"),
    ("GET", "/scans/{sid}/report-facts"),
    ("GET", "/scans/{sid}/files/{filename:path}/change-reviews"),
    ("GET", "/scans/{sid}/files/{filename:path}/artifact/{sha256}/page/{page}"),
], _SCAN_READ)
# Rendering a report is an EXPORT: it produces a document that leaves the workspace, so it takes
# the same pair as /scans/{sid}/report.pdf rather than a view grant on its own.
_map_many([("POST", "/scans/{sid}/report-render")], {"release.view", "reports.export"})
# Recording a verdict on a saved AI change is the reviewer's own action (PRD §5), not a view of
# it. `remediate.review` names exactly that and nothing else: a role that may watch remediation
# must not be able to sign off the changes it produced.
_map_many([("PUT", "/scans/{sid}/files/{filename:path}/change-reviews/{change_id:path}")],
          {"remediate.review"})

# ── Monitor ───────────────────────────────────────────────────────────────────
_map_many([("GET", "/schedule"),
           ("GET", "/schedule/history"), ("GET", "/schedule/notifications"),
           ("GET", "/analytics/compliance-trend")], {"monitor.view"})
_map_many([("PUT", "/schedule"),
           ("POST", "/schedule/notifications/{notification_id}/read")],
          {"monitor.view", "settings.view"})

# ── Live Operations ───────────────────────────────────────────────────────────
_map_many([("GET", "/admin/activity"), ("GET", "/jobs"), ("GET", "/jobs/{job_id}"),
           ("GET", "/control/estate"), ("GET", "/control/workers/capacity"),
           ("GET", "/control/costs"),
           ("GET", "/control/workers/replicas"), ("GET", "/control/workers/revisions")],
          {"operations.view"})
# The platform audit trail and the diagnostics export (PRD §13). Operational visibility, so they
# sit with Live Operations above — but BOTH ARE GATED TWICE, and the second gate is the load-
# bearing one: each handler calls `_require_admin`, so a workspace role holding `operations.view`
# still gets 403 unless the caller is the platform owner or an ACP_ADMIN_EMAILS entry. An audit
# log names who changed what and when, and a support bundle is composed for a support ticket;
# neither is something a Live Operations viewer should read by virtue of that role alone.
#
# Mapped rather than EXEMPT because an exemption says "this route needs no capability decision",
# and these needed one.
_map_many([("GET", "/admin/audit-events"), ("GET", "/admin/support-bundle")],
          {"operations.view"})
_map_many([("POST", "/admin/jobs/clear-dead"), ("PATCH", "/control/workers/replicas")],
          {"workers.manage"})

# ── Capacity scheduling (Settings -> Scheduling, and the Live Operations mode strip) ──────────
# ONE BLOCK, DELIBERATELY. These routes were mapped in two places after two implementations of
# this PRD landed together — here and again in the Settings block below. ROUTE_CAPABILITIES is a
# dict, so the LATER mapping silently won, and the later one granted the GET on settings.view
# alone: the Live Operations capacity strip would have been blank for an operations-only user,
# with nothing anywhere reporting why. The Settings block no longer names these routes.
#
# The writes carry settings.view AS WELL AS workers.manage. settings.view granting a write looks
# wrong in isolation and is this repository's existing convention for the Settings panel (PUT
# /settings and every /ai write are mapped the same way); dropping it here would have revoked an
# access #1538 deliberately granted. Holding ANY capability in the set is enough, so the union
# preserves both intents rather than picking a winner. _require_admin in each handler is the
# authoritative gate either way; this map only narrows who may reach them.
# The GET is granted by EITHER capability because the same payload feeds two surfaces: the
# read-only Scheduling tab in Settings (PRD §4 gives a view-only Settings user the right to
# inspect the schedule and, per §10, its validation result) and the capacity-mode strip in Live
# Operations. Mapping it to one of them would blank the other for exactly the users it is for.
_map_many([("GET", "/control/capacity-schedule")], {"operations.view", "settings.view"})
# Pricing a proposed schedule is the dry run that precedes Phase 3's write, so it sits with the
# capability that manages capacity rather than with the ones that only read it. The handler
# additionally enforces _require_admin — this map narrows who may reach it, not who may act.
_map_many([("POST", "/control/capacity-schedule/validate")],
          {"settings.view", "workers.manage"})
# Phase 3's writes. All three change durable state and two can reach Azure, so they sit with the
# capability that manages capacity — and each handler additionally enforces _require_admin, which
# is the authoritative gate; this map narrows who may reach them.
_map_many([("PUT", "/control/capacity-schedule"),
           ("POST", "/control/capacity-schedule/apply"),
           ("POST", "/control/capacity-schedule/override"),
           ("DELETE", "/control/capacity-schedule/override")],
          {"settings.view", "workers.manage"})
# The rendered policy is a READ — what ACP would apply, inspectable before anyone applies it,
# which is the whole argument for showing it. Same grant as the schedule it derives from.
_map_many([("GET", "/control/capacity-schedule/policy")], {"operations.view", "settings.view"})
_map_many([("GET", "/admin/schedule-guardrails"),
           ("PUT", "/admin/schedule-guardrails")],
          {"settings.view", "workers.manage"})

# ── Scan Analytics ────────────────────────────────────────────────────────────
_map_many([("GET", "/admin/analytics/overview"),
           ("GET", "/admin/analytics/scans/{scan_id}"),
           ("GET", "/admin/analytics/export"),
           ("GET", "/admin/analytics/methodology"),
           ("GET", "/ai/costs")], {"analytics.view"})

# ── Settings and platform administration ──────────────────────────────────────
_map_many([("GET", "/settings"),
           ("GET", "/ai/providers"),
           ("GET", "/ai/second-opinion-policy"), ("GET", "/ai/remediation-pilot"),
           ("GET", "/ai/status"),
           ("GET", "/ai/providers/health"), ("GET", "/ai/providers/{provider}/health")],
          {"settings.view"})
_map_many([("PUT", "/settings"), ("PUT", "/ai/providers"),
           ("PUT", "/ai/second-opinion-policy"), ("PUT", "/ai/remediation-pilot"),
           ("POST", "/ai/providers/test"),
           ("POST", "/ai/providers/{provider}/secret")],
          {"settings.view"})
_map_many([("PUT", "/workers")], {"workers.manage"})
_map_many([("GET", "/admin/people"), ("GET", "/admin/allowlist"), ("GET", "/admin/admins")],
          {"people.manage"})
_map_many([
    ("POST", "/admin/people"), ("PUT", "/admin/people/{email}"),
    ("DELETE", "/admin/people/{email}"), ("PUT", "/admin/allowlist"),
    ("POST", "/admin/invite"), ("PUT", "/admin/admins"),
], {"people.manage"})
_map_many([("PUT", "/admin/people/{email}/role"),
           ("GET", "/admin/people/{email}/role-impact")], {"people.manage"})
_map_many([
    ("GET", "/admin/workspace-roles/enforcement"),
    ("PUT", "/admin/workspace-roles/enforcement"),
    ("GET", "/admin/roles"), ("GET", "/admin/roles/{role_id}"), ("GET", "/admin/capabilities"),
    ("POST", "/admin/roles"), ("PUT", "/admin/roles/{role_id}"),
    ("DELETE", "/admin/roles/{role_id}"), ("POST", "/admin/workspace-roles/bootstrap"),
    ("GET", "/admin/workspace-roles/preflight"),
], {"roles.manage"})
# Wiping the workspace is the most destructive action ACP has; it stays owner-only at the route
# (_require_owner) and is additionally mapped here so it can never be reached by a role.
_map_many([("POST", "/admin/reset")], {"roles.manage"})

# ── Lifecycle / disposition ───────────────────────────────────────────────────
# Deciding what happens to a document at end of life is a Release-shaped decision, and executing
# it moves or trashes real files — which is why the two are separated.
_map_many([
    ("GET", "/disposition/policies"), ("GET", "/disposition/policies/conflicts"),
    ("GET", "/disposition/audit"), ("GET", "/disposition/approvals"),
    ("GET", "/scans/{sid}/lifecycle/files"), ("GET", "/scans/{sid}/lifecycle/rules"),
    ("GET", "/scans/{sid}/lifecycle/summary"),
    ("GET", "/scans/{sid}/lifecycle/files/{document_id:path}"),
    ("GET", "/scans/{sid}/lifecycle/files/{document_id:path}/history"),
], {"release.view", "monitor.view"})
_map_many([
    ("POST", "/disposition/policies"), ("PUT", "/disposition/policies/{policy_id}"),
    ("DELETE", "/disposition/policies/{policy_id}"),
    ("PUT", "/disposition/policies/{policy_id}/enabled"),
    ("PUT", "/disposition/policies/reorder"), ("POST", "/disposition/preview"),
    ("POST", "/disposition/policies/{policy_id}/preview"),
    ("POST", "/scans/{sid}/files/{filename:path}/lifecycle-override"),
    ("POST", "/disposition/approvals"), ("POST", "/disposition/approvals/plan"),
], {"release.view"})
# Approving and executing a move-or-trash. `release.publish` because it is the same class of act:
# an irreversible change to the customer's estate.
_map_many([
    ("POST", "/disposition/approvals/{audit_id}/approve"),
    ("POST", "/disposition/approvals/{audit_id}/reject"),
    ("POST", "/disposition/approvals/{audit_id}/undo"),
    ("POST", "/disposition/policies/{policy_id}/execute"),
], {"release.publish"})

# ── Archive auto-fire (R9) ────────────────────────────────────────────────────
# The unattended sibling of the block above, and mapped one tier stricter at every level for the
# reason the whole feature turns on: nobody is watching when it acts.
#
# READING is release.view + monitor.view, matching the lifecycle reads above. Somebody has to be
# able to see WHICH files a machine is about to move and on what evidence without also holding the
# right to start it — a permission shape that only works if the two are separate capabilities.
_map_many([
    ("GET", "/lifecycle/archive/policy"),
    ("GET", "/lifecycle/archive/candidates"),
    ("GET", "/lifecycle/archive/executions"),
    ("GET", "/lifecycle/archive/executions/{execution_id}"),
], {"release.view", "monitor.view"})
# CONFIGURING it is release.publish, NOT release.view — unlike authoring a disposition rule, which
# sits at release.view above because a rule only ever writes a recommendation. This policy is the
# authorization itself: saving it decides what may be moved with no human in the loop, so it is
# the same class of act as publishing, one step removed. The kill switch is here too rather than
# somewhere looser, and that is a deliberate trade — anyone who can turn the lane ON can turn it
# off, and nobody else can, which is the right way round for a control whose failure mode is
# unauthorised STOPPING of a governance process the customer configured. The route additionally
# gates on _require_admin.
_map_many([
    ("PUT", "/lifecycle/archive/policy"),
    ("POST", "/lifecycle/archive/kill-switch"),
], {"release.publish"})
# RUNNING it moves customer files unattended. release.publish here as well, and the route is
# additionally _require_owner — the strictest gate this codebase has for an estate change, which
# routes/disposition.py already applies to its own execute path for the attended version of the
# same act.
_map_many([("POST", "/lifecycle/archive/run")], {"release.publish"})

# ── Campaigns, org memory, content workspaces ─────────────────────────────────
_map_many([("GET", "/campaigns"), ("GET", "/campaigns/{campaign_id}")], _REMEDIATE_READ)
_map_many([
    ("POST", "/campaigns"), ("PUT", "/campaigns/{campaign_id}/status"),
    ("PUT", "/campaigns/{campaign_id}/batches/{batch_id}/status"),
], {"remediate.run"})
_map_many([("GET", "/org-memory")], _REMEDIATE_READ)
_map_many([("POST", "/org-memory"), ("POST", "/org-memory/derive"),
           ("PUT", "/org-memory/{mid}/status")], {"remediate.review"})
_map_many([
    ("GET", "/content-workspaces"), ("GET", "/content-workspaces/{workspace_id}"),
    ("GET", "/content-workspaces/{workspace_id}/documents"),
    ("GET", "/content-workspaces/{workspace_id}/documents/{document_id}"),
    ("GET", "/content-workspaces/{workspace_id}/documents/{document_id}/versions/{version_id}/assessment"),
    ("GET", "/content-workspaces/{workspace_id}/documents/{document_id}/versions/{version_id}/download"),
], _SOURCES_READ)
_map_many([
    ("POST", "/content-workspaces"),
    ("POST", "/content-workspaces/{workspace_id}/documents/upload-session"),
    ("POST", "/content-workspaces/{workspace_id}/documents/{document_id}/complete"),
    ("POST", "/content-workspaces/{workspace_id}/documents/{document_id}/resolve-duplicate"),
    ("POST", "/content-workspaces/{workspace_id}/documents/{document_id}/versions/upload-session"),
], {"sources.manage"})
_map_many([
    ("POST", "/content-workspaces/{workspace_id}/assess"),
    ("POST", "/content-workspaces/{workspace_id}/documents/{document_id}/versions/{version_id}/assess"),
], {"assess.run"})

# ── Exports (PRD §5's "Export reports or inventory") ──────────────────────────
_map_many([("GET", "/hub")], {"reports.export", "monitor.view"})

# ── SSE streams (PRD §16) ─────────────────────────────────────────────────────
# "SSE streams enforce the same permissions as their corresponding status endpoints." Listed
# together, and asserted against those endpoints in the tests, because a stream is the ONE place
# where forgetting is invisible: a 403 on a status poll is obvious in the UI, while an unguarded
# stream just keeps delivering — the data still flows, nobody sees an error, and the leak looks
# like the feature working.
STREAM_TWINS: dict[tuple[str, str], tuple[str, str]] = {
    ("GET", "/scans/{scan_id}/discover/stream"): ("GET", "/scans/{sid}/status"),
    ("GET", "/scans/{sid}/remediation/stream"): ("GET", "/scans/{sid}/remediation-status"),
    ("GET", "/scans/jobs/{job_id}/stream"): ("GET", "/scans/jobs/{job_id}"),
    ("GET", "/admin/activity/stream"): ("GET", "/admin/activity"),
    ("GET", "/api/realtime/v1/stream"): ("GET", "/api/realtime/v1/status"),
}
# The multiplexed owner stream can carry Discover, Assess, Remediate, Release, and Monitor
# transitions. Its snapshot twin has the same read union; the route still derives owner identity
# from the access gate, so neither endpoint can be used to select another person's stream.
_map("GET", "/api/realtime/v1/status", _SCAN_READ)
for _stream, _twin in STREAM_TWINS.items():
    _map(_stream[0], _stream[1], ROUTE_CAPABILITIES[_twin])


_map_many([("GET", "/scans/{sid}/remediation/accepted-plan/{run_id}")], {"remediate.view"})


# ── exempt, with reasons ──────────────────────────────────────────────────────
# Each entry says WHY. An exemption without one is indistinguishable from an oversight, and the
# completeness test refuses a route that is in neither table.
EXEMPT: dict[tuple[str, str], str] = {
    ("GET", "/monitor/estate"): "counts-only production monitor; route independently validates dedicated X-Monitor-Key with hmac and fails closed when unset",
    # FastAPI framework routes are Starlette Routes, not APIRoutes. They are
    # already outside core's protected API enumeration and publicly served.
    # A raw route audit must account for them without changing that auth policy.
    ("GET", "/docs"): "existing public framework API documentation, no customer records",
    ("GET", "/docs/oauth2-redirect"): "existing public Swagger OAuth redirect helper",
    ("GET", "/openapi.json"): "existing public API schema, no customer records",
    ("GET", "/redoc"): "existing public framework API documentation, no customer records",
    ("GET", "/healthz"): "liveness probe — must answer before anything is configured",
    ("GET", "/readyz"): "readiness probe — same",
    ("GET", "/probe/readyz"): "readiness probe — same",
    ("GET", "/docs/health"): "documentation health, no customer data",
    ("GET", "/openapi/health.json"): "schema of the health endpoint, no customer data",
    ("GET", "/config"): "fetched PRE-AUTH by the SPA to know how to sign in",
    ("GET", "/public/verify/{scan_id}"): "R15 — anyone may verify a report digest without an account",
    ("POST", "/alerts/webhook"): "inbound webhook, authenticated by its own shared secret",
    # Identity must answer BEFORE a role can be known. Gating these on a capability is circular:
    # the SPA cannot learn it has no access without being allowed to ask.
    ("GET", "/me"): "identity — must answer before a role can be resolved",
    ("GET", "/me/access"): "the role answer itself; gating it on a capability is circular",
    ("GET", "/workspace/bootstrap"): "carries /me/access; same circularity",
    ("GET", "/workspace/active-workflows"): "continuity status required before choosing a stage",
    ("POST", "/me/reset-data"): "self-service, scoped to the caller's OWN scans by construction",
    ("GET", "/settings/mine"): "the caller's own preferences",
    ("PUT", "/settings/mine"): "the caller's own preferences",
    ("DELETE", "/settings/mine"): "the caller's own preferences",
    # A plain <a> navigation target with no auth header; it only 302s to a Langfuse deep link,
    # and core.is_public already treats it as public.
    ("GET", "/scans/{sid}/trace/{kind}"): "unauthenticated redirect target (see core.is_public)",
    ("GET", "/scans/{sid}/trace/file/{filename:path}"): "same redirect target, per-file form",
}

# Knowledge Graph uses the existing owner-scoped scan payload, not a new data API.
# Add only read routes: the shared scan capability union also backs comment writes.
for _key, _caps in tuple(ROUTE_CAPABILITIES.items()):
    if _key[0] == "GET" and _caps == _SCAN_READ:
        ROUTE_CAPABILITIES[_key] = _caps | {"graph.view"}

# Conformance workspace permission is additional to every existing per-report check.
_map_many([
    ('GET', '/acr'),
    ('GET', '/acr/editions'),
    ('GET', '/acr/{report_id}'),
    ('GET', '/acr/{report_id}/criteria'),
    ('GET', '/acr/{report_id}/criteria/{criterion_num}'),
    ('GET', '/acr/{report_id}/gaps'),
    ('GET', '/acr/{report_id}/criteria/{criterion_num}/plans'),
    ('GET', '/acr/{report_id}/validation'),
    ('GET', '/acr/{report_id}/audit'),
    ('GET', '/acr/{report_id}/preview'),
    ('GET', '/acr/{report_id}/roles'),
    ('GET', '/acr/{report_id}/publication'),
    ('GET', '/acr/{report_id}/revisions'),
    ('GET', '/acr/{report_id}/revisions/{revision}'),
    ('GET', '/acr/{report_id}/revisions/{revision}/export'),
], {"acr.view"})
_map_many([
    ('POST', '/acr'),
    ('PATCH', '/acr/{report_id}'),
    ('POST', '/acr/{report_id}/evidence/axe'),
    ('POST', '/acr/{report_id}/criteria/{criterion_num}/applicability'),
    ('POST', '/acr/{report_id}/criteria/{criterion_num}/evidence'),
    ('POST', '/acr/{report_id}/criteria/{criterion_num}/decision'),
    ('POST', '/acr/{report_id}/criteria/{criterion_num}/approve'),
    ('POST', '/acr/{report_id}/criteria/{criterion_num}/plans/start'),
    ('POST', '/acr/{report_id}/plans/runs/{run_id}/step'),
    ('POST', '/acr/{report_id}/plans/runs/{run_id}/complete'),
    ('PUT', '/acr/{report_id}/roles'),
    ('POST', '/acr/{report_id}/publish'),
    ('POST', '/acr/{report_id}/revise'),
], {"acr.operate"})


def is_exempt(method: str, path: str) -> bool:
    return (method.upper(), path) in EXEMPT


def required_capabilities(method: str, path: str) -> frozenset[str] | None:
    """What this route needs, or None when it is exempt or unknown.

    None is DELIBERATELY ambiguous between "exempt" and "not in the table", and the middleware
    treats it as allow — because an unknown route is a bug in this file, and a 403 on every
    unmapped route would take the product down on the day somebody adds an endpoint. The
    completeness test is what makes that safe: an unmapped route cannot reach main.
    """
    return ROUTE_CAPABILITIES.get((method.upper(), path))


def unmapped_routes(routes) -> list[tuple[str, str]]:
    """Every (method, path) that is neither mapped nor exempt. Empty is the invariant."""
    out = []
    from types import SimpleNamespace
    from core import enumerate_api_routes
    # Readiness also accepts already-normalized route descriptors. Preserve
    # those rather than silently dropping them through APIRoute-only discovery.
    effective = []
    for route in routes:
        if hasattr(route, "path") and hasattr(route, "methods"):
            effective.append(route)
        else:
            effective.extend(enumerate_api_routes(SimpleNamespace(routes=[route])))
    for route in effective:
        for method in (route.methods or ()):
            if method in ("HEAD", "OPTIONS"):
                continue
            key = (method.upper(), route.path)
            if key not in ROUTE_CAPABILITIES and not is_exempt(*key):
                out.append(key)
    return sorted(set(out))


def unknown_capabilities() -> list[str]:
    """Capabilities named here that the catalog does not define — a typo in this file otherwise
    produces a route nobody can ever reach, since no role can hold a capability that is not real."""
    named = {cap for caps in ROUTE_CAPABILITIES.values() for cap in caps}
    return sorted(named - rbac.CAPABILITIES)


def allows(method: str, path: str, held) -> bool:
    needed = required_capabilities(method, path)
    if not needed:
        return True
    if (method.upper(), path) in ALL_OF_ROUTES:
        return needed <= frozenset(held)
    return bool(needed & frozenset(held))

# Creating a delivery parent is part of explicitly authorized publishing.
_map_many([("POST", "/release/folders")], {"release.publish"})

_map_many([("GET", "/scans/{sid}/remediation/ai-approval/{run_id}"),
           ("POST", "/scans/{sid}/remediation/ai-approval/{run_id}")], {"remediate.review"})
