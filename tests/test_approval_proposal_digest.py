"""An approval binds the proposal CONTENT/TARGET the reviewer viewed, not only its version tokens.

THE DEFECT (parent repro, real producer → route). Store.enqueue_proposals, run without a run
context, refreshes a pending row IN PLACE — slide 1 Picture 1 becomes slide 2 Picture 2 — on the
same corrected artifact and source revision. Snapshot ids stay empty and decision_version does
not move, so every token the approval compared still matched, and an old frozen request approved
"Original picture description" onto a different picture.

THE CONTRACT. GET /hitl/queue serves `proposal_digest`, a server-computed sha256 of the row's
reviewable content (every proposal and evidence entry as its producer wrote it, minus the
reviewer-owned `approved_value`). An approval echoes it as `expected_proposal_digest`, compared
under the row lock for single and frozen-batch approvals alike, and recorded as
`approved_proposal_digest` so a later producer refresh also holds an approval already given.
Because the digest is computed from the stored content, EVERY producer path that changes content
changes it by construction; none has to remember to bump a version.

Synthetic fixtures only.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from test_hitl_decision_atomicity import decision  # noqa: F401  (fixture)

FILE = "deck.pptx"
A = "a" * 64
P1 = "ppt/slides/slide1.xml#Picture 1"
P2 = "ppt/slides/slide2.xml#Picture 2"


def _record(st, sha=A):
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


def frozen(st, item_id):
    """Everything a client showing the row sends, read off the LISTED row."""
    row = _listed(st, item_id)
    return dict(expected_version=row.get("decision_version") or 0,
                expected_source_revision=row["source_revision"],
                expected_proposal_snapshot_ids=list(row.get("proposal_snapshot_ids") or []),
                expected_corrected_sha256=row.get("corrected_artifact"),
                expected_proposal_digest=row.get("proposal_digest"),
                approval_scope="single")


def _refresh(st, locator=P2, value="A different object."):
    """The real producer path, no run context: the same row's proposals replaced in place."""
    st.enqueue_proposals("s1", FILE, "1.1.1", [{
        "locator": locator, "before": "", "proposed_value": value,
        "rationale": "new draft", "source": "vision"}])


def _refused(st, item_id, response, code="stale_viewed_version"):
    assert getattr(response, "status_code", None) == 409, response
    assert json.loads(response.body)["code"] == code
    row = st.get_hitl_item(item_id)
    assert row["status"] == "pending"
    assert all("approved_value" not in p for p in row["proposals"])
    assert st.list_decisions("s1") == []
    assert all(j["type"] != "apply_approved_values" for j in st.list_jobs())


def test_a_refresh_to_a_different_target_refuses_the_old_frozen_approval(decision):
    """The parent's repro, inverted: real enqueue_proposals refresh → route."""
    st, item_id, update, Body, request = decision
    _record(st)
    old = frozen(st, item_id)
    _refresh(st)
    after = st.get_hitl_item(item_id)
    assert after["proposals"][0]["locator"] == P2
    assert after["proposal_snapshot_ids"] in (None, [])          # nothing else moved
    assert (after.get("decision_version") or 0) == old["expected_version"]
    response = update(item_id, Body(status="approved",
                                    approved_values=["Original picture description"], **old), request)
    _refused(st, item_id, response)


@pytest.mark.parametrize("change", ["value", "locator", "before"])
def test_any_content_change_on_the_same_row_refuses_the_viewed_approval(decision, change):
    st, item_id, update, Body, request = decision
    _record(st)
    old = frozen(st, item_id)
    proposal = dict(st.get_hitl_item(item_id)["proposals"][0])
    proposal.update({"value": {"proposed_value": "A different description."},
                     "locator": {"locator": P2},
                     "before": {"before": "some text the reviewer never saw"}}[change])
    st.enqueue_proposals("s1", FILE, "1.1.1", [proposal])
    _refused(st, item_id, update(item_id, Body(status="approved", **old), request))


def test_same_non_empty_snapshot_ids_do_not_excuse_an_in_place_change(decision):
    """Snapshot ids unchanged (an in-place update that kept them) — the content still binds."""
    st, item_id, update, Body, request = decision
    _record(st)
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE hitl_queue SET proposal_snapshot_ids=%s WHERE id=%s",
                       (json.dumps(["snap-1"]), item_id))
    old = frozen(st, item_id)
    assert old["expected_proposal_snapshot_ids"] == ["snap-1"]
    row = st.get_hitl_item(item_id)
    row["proposals"][0]["locator"] = P2
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE hitl_queue SET proposals=%s WHERE id=%s",
                       (json.dumps(row["proposals"]), item_id))
    assert st.get_hitl_item(item_id)["proposal_snapshot_ids"] == ["snap-1"]
    _refused(st, item_id, update(item_id, Body(status="approved", **old), request))


def test_evidence_attached_after_viewing_refuses_the_viewed_approval(decision):
    st, _, update, Body, request = decision
    _record(st)
    # Its own document, so the deferral opens a fresh evidence-only row (on deck.pptx it would
    # merge into the fixture's existing 1.1.1 row).
    doc = "evidence.pptx"
    item_id = st.queue_hitl_deferral("s1", doc, "Synthetic images", 1, rule_id="1.1.1/deferred")
    assert item_id
    assert st.attach_hitl_evidence("s1", doc, "1.1.1", [{"locator": P1, "thumb": "t1"}]) == item_id
    old = frozen(st, item_id)
    assert st.attach_hitl_evidence("s1", doc, "1.1.1", [{"locator": P2, "thumb": "t2"}]) == item_id
    response = update(item_id, Body(status="approved", approved_values=["Described"], **old), request)
    assert response.status_code == 409
    assert st.list_decisions("s1") == []


def test_unchanged_content_approves_and_records_the_viewed_digest(decision):
    """Positive control."""
    st, item_id, update, Body, request = decision
    _record(st)
    old = frozen(st, item_id)
    row = update(item_id, Body(status="approved", approved_values=["Viewed description"], **old),
                 request)
    assert row["status"] == "approved"
    assert row["approved_proposal_digest"] == old["expected_proposal_digest"]
    assert st.approved_write_hold(st.get_hitl_item(item_id)) is None


def test_the_digest_ignores_the_reviewers_own_approved_value(decision):
    """Approving writes approved_value into the proposals; the served digest does not move, so
    an unchanged re-approval of the same row is still an exact match (and a replay)."""
    st, item_id, update, Body, request = decision
    _record(st)
    old = frozen(st, item_id)
    update(item_id, Body(status="approved", approved_values=["A reviewer-edited description"],
                         **old), request)
    assert st.get_hitl_item(item_id)["proposals"][0]["approved_value"] == \
        "A reviewer-edited description"
    assert _listed(st, item_id)["proposal_digest"] == old["expected_proposal_digest"]
    assert st.approved_write_hold(st.get_hitl_item(item_id)) is None


def test_after_a_refresh_the_current_row_can_be_approved(decision):
    st, item_id, update, Body, request = decision
    _record(st)
    frozen(st, item_id)
    _refresh(st)
    current = frozen(st, item_id)
    row = update(item_id, Body(status="approved", **current), request)
    assert row["status"] == "approved" and row["proposals"][0]["locator"] == P2


def test_a_missing_digest_is_required(decision):
    st, item_id, update, Body, request = decision
    _record(st)
    old = {**frozen(st, item_id), "expected_proposal_digest": None}
    _refused(st, item_id, update(item_id, Body(status="approved", **old), request),
             code="viewed_version_required")


def test_a_refresh_after_approval_holds_the_approval_and_flags_it(decision, monkeypatch):
    """Post-approval retarget: the approved text is the same as the new draft, so the value
    digest cannot see the change — the recorded content digest does."""
    import handlers
    from proposals import Verification
    st, item_id, update, Body, request = decision
    _record(st)
    draft = st.get_hitl_item(item_id)["proposals"][0]["proposed_value"]
    update(item_id, Body(status="approved", **frozen(st, item_id)), request)
    _refresh(st, locator=P2, value=draft)
    held = st.approved_write_hold(st.get_hitl_item(item_id))
    assert st.WRITE_HOLD_CODES.get(held) == "proposals_superseded"
    listed = _listed(st, item_id)
    assert listed["approval_recheck_required"] is True
    assert listed["approval_recheck_reason"] == "proposals_superseded"
    reached = []
    monkeypatch.setattr("apply_alt.apply_alt_text", lambda *a, **k: reached.append(a) or (a[0], [], []))
    monkeypatch.setitem(sys.modules, "blob", SimpleNamespace(download_remediated=lambda *a: b"x"))
    monkeypatch.setattr(handlers, "_verify_residual", lambda *a, **kw: Verification(True, ()))
    handlers._apply_approved_values({"scan_id": "s1", "file": FILE}, {})
    assert reached == []
    # One re-approval of the CURRENT row re-binds it.
    row = update(item_id, Body(status="approved", **frozen(st, item_id)), request)
    assert row["decision_version"] == 2
    assert st.approved_write_hold(st.get_hitl_item(item_id)) is None


def test_the_retry_gate_refuses_an_approval_whose_content_changed_in_place(decision):
    """Snapshot ids present and unchanged, artifact unchanged, value unchanged: only the target
    moved. The single-item retry must not re-apply the approval to it."""
    st, item_id, update, Body, request = decision
    _record(st)
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE hitl_queue SET proposal_snapshot_ids=%s WHERE id=%s",
                       (json.dumps(["snap-1"]), item_id))
    update(item_id, Body(status="approved", **frozen(st, item_id)), request)
    binding, reason = st.approved_write_binding(st.get_hitl_item(item_id))
    assert reason is None and binding
    row = st.get_hitl_item(item_id)
    row["proposals"][0]["locator"] = P2
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE hitl_queue SET proposals=%s WHERE id=%s",
                       (json.dumps(row["proposals"]), item_id))
    _, reason = st.approved_write_binding(st.get_hitl_item(item_id))
    assert reason == st.RETRY_PROPOSALS_SUPERSEDED
    assert st.retry_approved_write(item_id)["accepted"] is False


def test_a_legacy_approval_without_a_recorded_digest_is_held_as_unbound(decision):
    st, item_id, update, Body, request = decision
    _record(st)
    update(item_id, Body(status="approved", **frozen(st, item_id)), request)
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE hitl_queue SET approved_proposal_digest=NULL WHERE id=%s", (item_id,))
    held = st.approved_write_hold(st.get_hitl_item(item_id))
    assert st.WRITE_HOLD_CODES.get(held) == "binding_missing"
    assert _listed(st, item_id)["approval_recheck_reason"] == "binding_missing"
