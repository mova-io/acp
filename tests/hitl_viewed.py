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
    approval_scope} for the row as it stands now."""
    if store is None:
        import core
        store = core.store
    row = store.get_hitl_item(item_id)
    snapshots = row.get("proposal_snapshot_ids")
    return {"expected_version": row.get("decision_version") or 0,
            "expected_source_revision": store.remediation_source_revision(row["scan_id"]),
            "expected_proposal_snapshot_ids": list(snapshots) if isinstance(snapshots, list) else [],
            "approval_scope": "single"}
