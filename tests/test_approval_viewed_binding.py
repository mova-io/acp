"""An approval binds to the version the reviewer SAW, compared under the row lock (audit gap 8).

THE DEFECT, reproduced through the real route. Single approvals from Remediate, FileDrawer and the
HITL bell sent only `expected_version`. The server then stamped whatever assessment revision was
current at click time, so a re-assessment between opening a suggestion and approving it produced an
approval bound to a document version the reviewer never saw — and the writer would honour it.

THE CONTRACT (PUT /hitl/queue/{id}, status "approved"):
  * the body carries the VIEWED `expected_version`, `expected_source_revision` and
    `expected_proposal_snapshot_ids` (the row's `proposal_snapshot_ids` as served, `[]` when the
    row has none) and, for a single-item decision, `approval_scope: "single"`;
  * any of them missing -> 409 {code: "viewed_version_required"}; any of them different from the row
    under its lock -> 409 {code: "stale_viewed_version"}. Nothing is recorded and no job exists;
  * a match stamps exactly the revision the reviewer sent;
  * reject/skip never require the binding (they write nothing) and record it when sent;
  * a request with the binding but no `approval_scope` keeps the frozen-batch guard, unchanged
    (tests/test_hitl_exact_batch_selection.py).

Synthetic fixtures only.
"""
from __future__ import annotations

import json

import pytest
from test_hitl_decision_atomicity import decision  # noqa: F401  (fixture)

STALE_MESSAGE = ("This suggestion or its document changed after you opened it. "
                 "Review the current version, then approve again.")


def viewed(st, item_id):
    """The binding a client reads off the row it is showing (GET /hitl/queue shape)."""
    row = st.get_hitl_item(item_id)
    return dict(expected_version=row.get("decision_version") or 0,
                expected_source_revision=st.remediation_source_revision(row["scan_id"]),
                expected_proposal_snapshot_ids=list(row.get("proposal_snapshot_ids") or []),
                approval_scope="single")


def _reassess(st):
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE scan_runs SET rubric_hash=%s WHERE id=%s",
                       ("synthetic-unseen-assessment", "s1"))


def _conflict(response):
    """The route answers a viewed-version conflict with a 409 carrying top-level code/message, the
    shape frontend/src/api.js lifts onto the thrown Error (e.code, e.message, e.changes)."""
    assert getattr(response, "status_code", None) == 409, response
    return json.loads(response.body)


def _nothing_recorded(st, item_id):
    row = st.get_hitl_item(item_id)
    assert row["status"] == "pending"
    assert not row.get("approved_source_revision")
    assert "approved_value" not in row["proposals"][0]
    assert st.list_decisions("s1") == []
    assert all(j["type"] != "apply_approved_values" for j in st.list_jobs())


def test_expected_version_only_approval_is_refused_and_stamps_nothing(decision):
    """The parent's repro, inverted: the live callers' old body (expected_version only), sent after
    an unseen re-assessment. It used to approve and stamp the NEW revision."""
    st, item_id, update, Body, request = decision
    viewed_version = st.get_hitl_item(item_id).get("decision_version") or 0
    _reassess(st)
    body = _conflict(update(item_id, Body(status="approved", approved_values=["Synthetic caption"],
                                          expected_version=viewed_version), request))
    assert body["code"] == "viewed_version_required"
    assert body["changes"] == "none" and body["message"] == body["detail"]
    assert "refresh" in body["message"].lower()
    _nothing_recorded(st, item_id)


@pytest.mark.parametrize("missing", ["expected_version", "expected_source_revision",
                                     "expected_proposal_snapshot_ids"])
def test_each_viewed_field_is_required_on_approve(decision, missing):
    st, item_id, update, Body, request = decision
    fields = viewed(st, item_id)
    fields[missing] = None
    body = _conflict(update(item_id, Body(status="approved", **fields), request))
    assert body["code"] == "viewed_version_required"
    _nothing_recorded(st, item_id)


def test_viewed_source_revision_mismatch_is_refused(decision):
    st, item_id, update, Body, request = decision
    fields = viewed(st, item_id)
    _reassess(st)
    body = _conflict(update(item_id, Body(status="approved", approved_values=["Synthetic caption"],
                                          **fields), request))
    assert body == {"code": "stale_viewed_version", "message": STALE_MESSAGE,
                    "detail": STALE_MESSAGE, "changes": "none"}
    _nothing_recorded(st, item_id)


def test_viewed_snapshot_mismatch_is_refused(decision):
    st, item_id, update, Body, request = decision
    fields = viewed(st, item_id)
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE hitl_queue SET proposal_snapshot_ids=%s WHERE id=%s",
                       (json.dumps(["replacement-snapshot"]), item_id))
    assert _conflict(update(item_id, Body(status="approved", **fields), request))["code"] \
        == "stale_viewed_version"
    assert st.list_decisions("s1") == []


def test_viewed_decision_version_mismatch_is_refused(decision):
    st, item_id, update, Body, request = decision
    fields = viewed(st, item_id)
    update(item_id, Body(status="skipped", expected_version=fields["expected_version"]), request)
    assert _conflict(update(item_id, Body(status="approved", **fields), request))["code"] \
        == "stale_viewed_version"
    assert st.get_hitl_item(item_id)["status"] == "skipped"


def test_matching_binding_approves_and_stamps_the_viewed_revision(decision):
    st, item_id, update, Body, request = decision
    fields = viewed(st, item_id)
    row = update(item_id, Body(status="approved", approved_values=["Reviewer-edited caption"],
                               edited=True, **fields), request)
    assert row["status"] == "approved"
    assert row["approved_source_revision"] == fields["expected_source_revision"]
    assert row["proposals"][0]["approved_value"] == "Reviewer-edited caption"
    assert [d["action"] for d in st.list_decisions("s1")] == ["hitl.approved"]
    assert [j["type"] for j in st.list_jobs()] == ["apply_approved_values"]


def test_row_with_no_proposals_binds_to_the_empty_list(decision):
    """A judgement row (nothing drafted) is approved against the empty snapshot list it was shown;
    a client does not have to invent snapshot ids to approve it."""
    st, _, update, Body, request = decision
    item_id = st.queue_hitl_deferral("s1", "deck.pptx", "Synthetic judgement", 1, rule_id="1.4.3")
    fields = viewed(st, item_id)
    assert fields["expected_proposal_snapshot_ids"] == []
    row = update(item_id, Body(status="approved", **fields), request)
    assert row["status"] == "approved"
    assert row["approved_source_revision"] == fields["expected_source_revision"]


def test_reject_and_skip_need_no_binding_and_record_it_when_sent(decision, monkeypatch):
    st, item_id, update, Body, request = decision
    events = []
    monkeypatch.setattr(st, "record_hitl_event", lambda *a, **k: events.append(k))
    assert update(item_id, Body(status="skipped"), request)["status"] == "skipped"
    fields = viewed(st, item_id)
    _reassess(st)   # reject/skip write nothing: a moved source is no reason to refuse them
    assert update(item_id, Body(status="rejected", reject_reason="too_vague", **fields),
                  request)["status"] == "rejected"
    assert events[0]["source_revision"] is None
    assert events[1]["source_revision"] == fields["expected_source_revision"]
    assert events[1]["proposal_snapshot_ids"] == fields["expected_proposal_snapshot_ids"]


def test_rechecking_a_held_approval_rebinds_instead_of_replaying(decision):
    """No approval loop: an approval held because its source moved is re-approved against the
    version now on screen. An identical approval used to read as a replay and change nothing, so
    the row could never leave the held state."""
    st, item_id, update, Body, request = decision
    update(item_id, Body(status="approved", **viewed(st, item_id)), request)
    first = st.get_hitl_item(item_id)
    _reassess(st)
    listed = next(r for r in st.list_hitl_queue(scan_id="s1") if r["id"] == item_id)
    assert listed["approval_recheck_required"] is True
    fields = viewed(st, item_id)
    row = update(item_id, Body(status="approved", **fields), request)
    assert row["decision_version"] == first["decision_version"] + 1
    assert row["approved_source_revision"] == fields["expected_source_revision"] \
        != first["approved_source_revision"]
    assert st.approved_write_hold(st.get_hitl_item(item_id)) is None
    assert [d["action"] for d in st.list_decisions("s1")] == ["hitl.approved", "hitl.approved"]
    listed = next(r for r in st.list_hitl_queue(scan_id="s1") if r["id"] == item_id)
    assert listed["approval_recheck_required"] is False
    # And a second identical click on the now-current approval IS a replay.
    replay = update(item_id, Body(status="approved", **viewed(st, item_id)), request)
    assert replay["decision_version"] == row["decision_version"]


def test_unknown_approval_scope_is_rejected(decision):
    st, item_id, update, Body, request = decision
    with pytest.raises(Exception) as exc:
        update(item_id, Body(status="approved", **{**viewed(st, item_id), "approval_scope": "all"}),
               request)
    assert getattr(exc.value, "status_code", None) == 422
    assert st.list_decisions("s1") == []


def test_http_conflict_body_is_top_level(decision):
    """Through the real app: the 409 body carries code/message at the top level."""
    from test_hitl_owner_isolation import _client
    st, item_id, *_ = decision
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE scan_runs SET owner_email=%s WHERE id=%s",
                       ("reviewer@example.com", "s1"))
    fields = viewed(st, item_id)
    _reassess(st)
    response = _client("reviewer@example.com").put(
        f"/hitl/queue/{item_id}", json={"status": "approved", **fields})
    assert response.status_code == 409
    assert response.json()["code"] == "stale_viewed_version"
    assert response.json()["message"] == STALE_MESSAGE
    assert st.list_decisions("s1") == []
