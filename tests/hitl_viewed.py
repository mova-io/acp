"""The viewed binding a reviewer's client sends with an approval (PUT /hitl/queue/{id}).

Every human approval names the version it approves (tests/test_approval_viewed_binding.py): the
row's decision version, its served `proposal_snapshot_ids` (`[]` when it has none) and the
assessment `source_revision` GET /hitl/queue lists it against. Tests that approve through the
route read those off the row exactly as a client showing it would — immediately before the
decision, because these tests are not about a stale view. `approval_scope: "single"` marks it a
single reviewer's decision rather than a frozen batch selection.
"""
from __future__ import annotations


def viewed_fields(item_id: str, store=None) -> dict:
    """{expected_version, expected_source_revision, expected_proposal_snapshot_ids,
    expected_corrected_sha256, approval_scope} for the row as it stands now — the corrected
    artifact exactly as GET /hitl/queue lists it as `corrected_artifact` (its sha256, or "none"
    before any copy)."""
    if store is None:
        import core
        store = core.store
    row = store.get_hitl_item(item_id)
    snapshots = row.get("proposal_snapshot_ids")
    return {"expected_version": row.get("decision_version") or 0,
            "expected_source_revision": store.remediation_source_revision(row["scan_id"]),
            "expected_proposal_snapshot_ids": list(snapshots) if isinstance(snapshots, list) else [],
            "expected_corrected_sha256": store.corrected_artifact_token(row["scan_id"], row["file"]),
            "approval_scope": "single"}


def approve_bound(store, item_id: str, values=None, *, resolution: str | None = None,
                  note: str | None = None, value: str | None = None,
                  actor: str = "reviewer@example.com") -> dict:
    """Record a reviewer approval the way the product records one — store.complete_hitl_decision —
    so it carries the COMPLETE binding (assessment revision, corrected artifact, value digest,
    proposal snapshots) of the version current right now. `values` is positional per proposal,
    exactly as approve_proposal_values takes it ([] / None entries accept the drafts).

    Replaces the update_hitl_item + approve_proposal_values pair, which recorded an approval with
    no binding at all: the writer now HOLDS such a row (a legacy, unbound approval), so a test
    that needs its values written approves the way a reviewer does."""
    row, _ = store.complete_hitl_decision(
        item_id, "approved", note, value, resolution=resolution, approved_values=values,
        actor=actor, detail=None)
    return row
