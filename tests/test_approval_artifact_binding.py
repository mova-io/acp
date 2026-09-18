"""An approval binds the EXACT corrected artifact it was given for, and an unbound one is held.

Two release blockers, reproduced by the parent and inverted here:

1. A MISSING binding failed open. approved_write_hold only compared the parts of a binding that
   were recorded, so an approved row with no recorded revision, digest or snapshots — a legacy
   approval, or any row a path approved without binding it — was admitted by the writer.
2. The binding named the scan-wide ASSESSMENT revision, never the corrected copy. A new corrected
   artifact (record_remediation to a new sha) leaves remediation_source_revision unchanged, so an
   approval given against one copy was written into different bytes.

Contract now:
  * every approval records `approved_corrected_sha256`: the corrected artifact's sha, or "none"
    when the document had no corrected copy yet (a first write builds on the assessed source,
    which the source revision already binds);
  * GET /hitl/queue lists that token per row as `corrected_artifact`, and an approval sends it back
    verbatim as `expected_corrected_sha256` — compared under the row lock like the other viewed
    fields (409 stale_viewed_version / viewed_version_required);
  * the writer holds an approval whose binding is incomplete, or whose artifact is not the one
    the write builds on; held rows stay counted and flagged, and one "single" re-approval of the
    current version re-binds them.

Synthetic fixtures only.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from test_hitl_decision_atomicity import decision  # noqa: F401  (fixture)

FILE = "deck.pptx"
A, B = "a" * 64, "b" * 64


def _record(st, sha=None):
    st.save_file_result("s1", {"file": FILE, "engine": "office", "status": "analysed",
                               "score": 50, "compliant": 0, "skipped_rules": 0, "issues": []},
                        "2026-09-01T00:00:00Z")
    if sha:
        st.record_remediation("s1", FILE, corrected_sha256=sha)


def _listed(st, item_id):
    from test_hitl_owner_isolation import _client
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE scan_runs SET owner_email=%s WHERE id=%s",
                       ("reviewer@example.com", "s1"))
    rows = _client("reviewer@example.com").get("/hitl/queue").json()
    return next(r for r in rows if r["id"] == item_id)


def viewed(st, item_id):
    """What a client showing the row sends: every viewed field, read off the listed row."""
    row = st.get_hitl_item(item_id)
    record = st.get_file_record("s1", FILE) or {}
    return dict(expected_version=row.get("decision_version") or 0,
                expected_source_revision=st.remediation_source_revision("s1"),
                expected_proposal_snapshot_ids=list(row.get("proposal_snapshot_ids") or []),
                expected_corrected_sha256=record.get("corrected_sha256") or "none",
                approval_scope="single")


def _approve(decision, sha=A):
    st, item_id, update, Body, request = decision
    _record(st, sha)
    update(item_id, Body(status="approved", approved_values=["Synthetic caption"],
                         **viewed(st, item_id)), request)
    assert st.get_hitl_item(item_id)["status"] == "approved"
    return st, item_id


def _code(st, item):
    reason = st.approved_write_hold(item)
    return reason and st.WRITE_HOLD_CODES.get(reason)


# ── gap 1: a missing binding is held, not admitted ─────────────────────────────────────────

def test_an_approval_with_no_recorded_binding_is_held(decision):
    """The parent's repro 1, inverted."""
    st, item_id = _approve(decision)
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE hitl_queue SET approved_source_revision=NULL,"
                            "approved_value_sha256=NULL,approved_proposal_snapshot_ids=NULL "
                            "WHERE id=%s", (item_id,))
    assert _code(st, st.get_hitl_item(item_id)) == "binding_missing"


@pytest.mark.parametrize("column", ["approved_source_revision", "approved_value_sha256",
                                    "approved_proposal_snapshot_ids", "approved_corrected_sha256"])
def test_any_missing_part_of_the_binding_holds(decision, column):
    st, item_id = _approve(decision)
    with st._db.cursor() as cur:
        st._db.execute(cur, f"UPDATE hitl_queue SET {column}=NULL WHERE id=%s", (item_id,))
    assert _code(st, st.get_hitl_item(item_id)) == "binding_missing"


def test_a_legacy_unbound_approval_is_held_counted_flagged_and_never_written(decision, monkeypatch):
    """An approval recorded before bindings existed: approved by update_hitl_item alone. It is
    not reconstructed from the current state — it is held, visibly, and still counted."""
    import core
    import handlers
    from proposals import Verification
    st, item_id, update, Body, request = decision
    _record(st, A)
    st.update_hitl_item(item_id, "approved")
    row = st.get_hitl_item(item_id)
    assert row["status"] == "approved" and _code(st, row) == "binding_missing"
    assert st.count_unapplied_approved_values("s1", FILE) == 1
    assert _listed(st, item_id)["approval_recheck_required"] is True
    reached = []
    monkeypatch.setattr("apply_alt.apply_alt_text", lambda *a, **k: reached.append(a) or (a[0], [], []))
    monkeypatch.setitem(sys.modules, "blob", SimpleNamespace(download_remediated=lambda *a: b"x"))
    monkeypatch.setattr(handlers, "_verify_residual", lambda *a, **kw: Verification(True, ()))
    handlers._apply_approved_values({"scan_id": "s1", "file": FILE}, {})
    assert reached == []
    held = [json.loads(d["detail"]) for d in st.list_decisions("s1")
            if d["action"] == "apply.stale_approval_held"]
    assert held == [{"item_id": item_id, "refusal": "binding_missing",
                     "approval_recheck_required": True}]


# ── gap 2: the approval binds the exact corrected artifact ─────────────────────────────────

def test_a_changed_corrected_artifact_is_held_with_the_same_assessment_revision(decision):
    """The parent's repro 2, inverted: same assessment token, different corrected sha."""
    st, item_id = _approve(decision, sha=A)
    revision = st.remediation_source_revision("s1")
    st.record_remediation("s1", FILE, corrected_sha256=B)
    assert st.remediation_source_revision("s1") == revision
    assert _code(st, st.get_hitl_item(item_id)) == "artifact_moved"
    assert st.count_unapplied_approved_values("s1", FILE) == 1


def test_the_approval_records_the_artifact_the_reviewer_viewed(decision):
    st, item_id = _approve(decision, sha=A)
    assert st.get_hitl_item(item_id)["approved_corrected_sha256"] == A
    assert _code(st, st.get_hitl_item(item_id)) is None


def test_the_queue_lists_the_artifact_token(decision):
    st, item_id, *_ = decision
    _record(st)
    assert _listed(st, item_id)["corrected_artifact"] == "none"
    st.record_remediation("s1", FILE, corrected_sha256=A)
    assert _listed(st, item_id)["corrected_artifact"] == A


def test_a_viewed_artifact_that_moved_is_refused_under_the_row_lock(decision):
    st, item_id, update, Body, request = decision
    _record(st, A)
    fields = viewed(st, item_id)
    st.record_remediation("s1", FILE, corrected_sha256=B)
    response = update(item_id, Body(status="approved", **fields), request)
    assert response.status_code == 409
    assert json.loads(response.body)["code"] == "stale_viewed_version"
    assert st.get_hitl_item(item_id)["status"] == "pending"
    assert st.list_decisions("s1") == []


def test_an_approval_without_the_viewed_artifact_is_required(decision):
    st, item_id, update, Body, request = decision
    _record(st, A)
    fields = {**viewed(st, item_id), "expected_corrected_sha256": None}
    response = update(item_id, Body(status="approved", **fields), request)
    assert response.status_code == 409
    assert json.loads(response.body)["code"] == "viewed_version_required"
    assert st.list_decisions("s1") == []


def test_one_single_re_approval_re_binds_a_held_row(decision):
    st, item_id = _approve(decision, sha=A)
    st.record_remediation("s1", FILE, corrected_sha256=B)
    assert _code(st, st.get_hitl_item(item_id)) == "artifact_moved"
    _, _, update, Body, request = decision
    row = update(item_id, Body(status="approved", **viewed(st, item_id)), request)
    assert row["approved_corrected_sha256"] == B
    assert _code(st, st.get_hitl_item(item_id)) is None


def test_a_first_write_binds_the_absence_of_a_corrected_copy(decision):
    """Approved before any corrected copy existed: bound to "none". A corrected copy produced
    after the approval is bytes the reviewer never saw, so the approval is then held."""
    st, item_id = _approve(decision, sha=None)
    assert st.get_hitl_item(item_id)["approved_corrected_sha256"] == "none"
    assert _code(st, st.get_hitl_item(item_id)) is None
    st.record_remediation("s1", FILE, corrected_sha256=A)
    assert _code(st, st.get_hitl_item(item_id)) == "artifact_moved"


def test_the_writer_holds_an_approval_given_against_different_bytes(decision, monkeypatch):
    import handlers
    from proposals import Verification
    st, item_id = _approve(decision, sha=A)
    st.record_remediation("s1", FILE, corrected_sha256=B)
    reached = []
    monkeypatch.setattr("apply_alt.apply_alt_text", lambda *a, **k: reached.append(a) or (a[0], [], []))
    monkeypatch.setitem(sys.modules, "blob", SimpleNamespace(download_remediated=lambda *a: b"x"))
    monkeypatch.setattr(handlers, "_verify_residual", lambda *a, **kw: Verification(True, ()))
    handlers._apply_approved_values({"scan_id": "s1", "file": FILE}, {})
    assert reached == []
    assert st.get_hitl_item(item_id)["status"] == "approved"
    assert not st.get_hitl_item(item_id)["applied"]


def test_the_retry_gate_refuses_an_approval_given_against_different_bytes(decision):
    st, item_id = _approve(decision, sha=A)
    st.record_remediation("s1", FILE, corrected_sha256=B)
    _, reason = st.approved_write_binding(st.get_hitl_item(item_id))
    assert st.WRITE_HOLD_CODES.get(reason) == "artifact_moved"
