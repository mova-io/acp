"""Frozen batch selections must not approve replacement proposal/source versions."""
import pytest
from test_hitl_decision_atomicity import decision as base_decision

@pytest.fixture()
def decision(base_decision):
    from ai_run_policy import run_context
    st, item_id, update, Body, request = base_decision
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE scan_runs SET owner_email=%s WHERE id=%s", ("reviewer@example.com", "s1"))
    result = st.enqueue_stage_batch("s1", "remediate", "remediate_file", [{
        "owner": "reviewer@example.com", "scan_id": "s1", "file": "deck.pptx",
        "remediation_impact_policy": {"ai": 1, "rule_based": 2, "ai_budget_usd": "0.10", "snapshot_id": "fixture"},
    }], snapshot_id=st.stage_snapshot_id("s1"), request_fingerprint="fixture")
    job = st.get_job(result["job_ids"][0])
    with run_context(st, job["payload"], job):
        st.enqueue_proposals("s1", "deck.pptx", "1.1.1", st.get_hitl_item(item_id)["proposals"])
    return base_decision


def expectations(st, item_id):
    row = st.get_hitl_item(item_id)
    return dict(status="approved", approved_values=[p["proposed_value"] for p in row["proposals"]],
                request_id="frozen-batch-1", expected_version=row.get("decision_version") or 0,
                expected_proposal_snapshot_ids=row["proposal_snapshot_ids"],
                expected_source_revision=st.stage_snapshot_id(row["scan_id"]),
                expected_corrected_sha256=st.corrected_artifact_token(row["scan_id"], row["file"]))


@pytest.mark.parametrize("change", ["snapshot", "source", "value", "locator", "missing_slot"])
def test_replacement_between_preview_and_decision_has_no_writes(decision, change):
    import json
    st, item_id, update, Body, request = decision
    before = expectations(st, item_id)
    with st._db.cursor() as cur:
        if change == "snapshot":
            st._db.execute(cur, "UPDATE hitl_queue SET proposal_snapshot_ids=%s WHERE id=%s",
                           (json.dumps(["replacement"]), item_id))
        elif change == "source":
            st._db.execute(cur, "UPDATE scan_runs SET rubric_hash=%s WHERE id=%s", ("reassessed", "s1"))
        elif change in {"value", "locator"}:
            row = st.get_hitl_item(item_id)
            row["proposals"][0]["proposed_value" if change == "value" else "locator"] = "replacement without a new snapshot"
            st._db.execute(cur, "UPDATE hitl_queue SET proposals=%s WHERE id=%s", (json.dumps(row["proposals"]), item_id))
        else:
            before["expected_proposal_snapshot_ids"] = []
    with pytest.raises(Exception) as exc:
        update(item_id, Body(**before), request)
    assert getattr(exc.value, "status_code", None) == 409
    assert st.get_hitl_item(item_id)["status"] == "pending"
    assert st.list_decisions("s1") == []
    assert all(j["type"] != "apply_approved_values" for j in st.list_jobs())


def test_exact_guarded_replay_does_not_duplicate_jobs_or_audit(decision):
    st, item_id, update, Body, request = decision
    body = Body(**expectations(st, item_id))
    first = update(item_id, body, request)
    assert update(item_id, body, request) == first
    assert len(st.list_decisions("s1")) == 1
    assert len([j for j in st.list_jobs() if j["type"] == "apply_approved_values"]) == 1


def test_same_request_id_cannot_be_reused_with_different_snapshot_expectation(decision):
    st, item_id, update, Body, request = decision
    expected = expectations(st, item_id)
    update(item_id, Body(**expected), request)
    expected["expected_proposal_snapshot_ids"] = ["different"]
    with pytest.raises(Exception) as exc:
        update(item_id, Body(**expected), request)
    assert getattr(exc.value, "status_code", None) == 409
    assert len(st.list_decisions("s1")) == 1


def _saved_copy(st, sha):
    st.save_file_result("s1", {"file": "deck.pptx", "engine": "office", "status": "analysed",
                               "score": 50, "compliant": 0, "skipped_rules": 0, "issues": []},
                        "2026-09-01T00:00:00Z")
    st.record_remediation("s1", "deck.pptx", corrected_sha256=sha)


def test_a_frozen_batch_is_refused_when_another_write_changed_the_corrected_copy(decision):
    """The exact-artifact binding covers batches too: the selection froze corrected copy A; an
    approved write since saved B under the SAME assessment revision. Nothing is recorded."""
    st, item_id, update, Body, request = decision
    _saved_copy(st, "a" * 64)
    frozen = expectations(st, item_id)
    assert frozen["expected_corrected_sha256"] == "a" * 64
    st.record_remediation("s1", "deck.pptx", corrected_sha256="b" * 64)
    assert frozen["expected_source_revision"] == st.stage_snapshot_id("s1")
    with pytest.raises(Exception) as exc:
        update(item_id, Body(**frozen), request)
    assert getattr(exc.value, "status_code", None) == 409
    assert st.get_hitl_item(item_id)["status"] == "pending"
    assert st.list_decisions("s1") == []
    assert all(j["type"] != "apply_approved_values" for j in st.list_jobs())


def test_a_frozen_batch_without_the_corrected_copy_it_saw_records_nothing(decision):
    import json
    st, item_id, update, Body, request = decision
    frozen = {**expectations(st, item_id), "expected_corrected_sha256": None}
    response = update(item_id, Body(**frozen), request)
    assert response.status_code == 409
    assert json.loads(response.body)["code"] == "viewed_version_required"
    assert st.list_decisions("s1") == []


def test_an_unchanged_frozen_batch_binds_its_copy_and_replays_idempotently(decision):
    st, item_id, update, Body, request = decision
    _saved_copy(st, "a" * 64)
    body = Body(**expectations(st, item_id))
    first = update(item_id, body, request)
    assert first["approved_corrected_sha256"] == "a" * 64
    assert update(item_id, body, request) == first
    assert len(st.list_decisions("s1")) == 1
    assert st.approved_write_hold(st.get_hitl_item(item_id)) is None


def test_frozen_batch_cannot_cross_owner_boundary(decision):
    from test_hitl_owner_isolation import _client
    st, item_id, update, Body, request = decision
    response = _client("somebody-else@example.com").put(f"/hitl/queue/{item_id}", json=expectations(st, item_id))
    assert response.status_code == 404
    assert st.get_hitl_item(item_id)["status"] == "pending"
    assert st.list_decisions("s1") == []


def test_another_scan_source_identity_is_rejected(decision):
    st, item_id, update, Body, request = decision
    expected = expectations(st, item_id)
    st.init_scan_run("other-scan", "drive", 1, "t0", "rubric", "hash")
    expected["expected_source_revision"] = st.stage_snapshot_id("other-scan")
    with pytest.raises(Exception) as exc:
        update(item_id, Body(**expected), request)
    assert getattr(exc.value, "status_code", None) == 409
    assert st.list_decisions("s1") == []


def test_queue_list_exposes_source_identity_without_mutating(decision):
    from test_hitl_owner_isolation import _client
    st, item_id, update, Body, request = decision
    rows = _client("reviewer@example.com").get("/hitl/queue").json()
    row = next(r for r in rows if r["id"] == item_id)
    assert row["source_revision"] == st.stage_snapshot_id("s1")
    assert row["proposal_snapshot_ids"] == st.get_hitl_item(item_id)["proposal_snapshot_ids"]
    assert st.list_decisions("s1") == []


def test_refreshed_source_cannot_relabel_an_old_proposal_as_current(decision):
    st, item_id, update, Body, request = decision
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE scan_runs SET rubric_hash=%s WHERE id=%s", ("new-assessment", "s1"))
    # The reviewer fetched the new current source identity, but these proposals were produced
    # under the older stage input. Exact snapshot IDs alone cannot make them current.
    with pytest.raises(Exception) as exc:
        update(item_id, Body(**expectations(st, item_id)), request)
    assert getattr(exc.value, "status_code", None) == 409
    assert st.list_decisions("s1") == []


def seal_assessment(st, fingerprint='sealed-assessment'):
    batch = st.enqueue_stage_batch('s1', 'assess', 'scan_file', [
        {'owner': 'reviewer@example.com', 'scan_id': 's1', 'file': 'deck.pptx'}],
        snapshot_id=st.stage_snapshot_id('s1'), request_fingerprint=fingerprint)
    with st._db.cursor() as cur:
        st._db.execute(cur, 'SELECT work_item_id,revision FROM stage_work_items WHERE execution_id=%s', (batch['batch_id'],))
        item = st._db.fetchone(cur)
    st.apply_stage_event(event_id=fingerprint, execution_id=batch['batch_id'],
        work_item_id=item['work_item_id'], event_type='work_item.completed',
        expected_revision=item['revision'], payload={'result_digest': fingerprint})
    manifest = st.seal_stage_if_ready(batch['batch_id'])['manifest_id']
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE jobs SET status='done' WHERE batch_id=%s", (batch['batch_id'],))
    return manifest


@pytest.fixture()
def canonical_decision(base_decision):
    from ai_run_policy import run_context
    st, item_id, *_ = base_decision
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE scan_runs SET owner_email=%s WHERE id=%s", ("reviewer@example.com", "s1"))
    manifest = seal_assessment(st)
    batch = st.enqueue_stage_batch('s1', 'remediate', 'remediate_file', [{
        'owner': 'reviewer@example.com', 'scan_id': 's1', 'file': 'deck.pptx',
        'remediation_impact_policy': {'ai': 1, 'rule_based': 2, 'ai_budget_usd': '0.10', 'snapshot_id': manifest},
    }], snapshot_id=manifest, input_manifest_id=manifest, request_fingerprint='canonical-remediation')
    job = st.get_job(batch['job_ids'][0])
    with run_context(st, job['payload'], job):
        st.enqueue_proposals('s1', 'deck.pptx', '1.1.1', st.get_hitl_item(item_id)['proposals'])
    return base_decision, manifest


def test_canonical_queue_identity_can_approve_exact_proposal_and_replay(canonical_decision):
    from test_hitl_owner_isolation import _client
    (st, item_id, update, Body, request), manifest = canonical_decision
    row = next(row for row in _client('reviewer@example.com').get('/hitl/queue').json() if row['id'] == item_id)
    assert manifest != st.stage_snapshot_id('s1')
    assert row['source_revision'] == manifest
    expected = expectations(st, item_id)
    expected['expected_source_revision'] = row['source_revision']
    first = update(item_id, Body(**expected), request)
    assert update(item_id, Body(**expected), request) == first
    assert st.get_hitl_item(item_id)['approved_source_revision'] == manifest
    assert len(st.list_decisions('s1')) == 1
    assert len([j for j in st.list_jobs() if j['type'] == 'apply_approved_values']) == 1


@pytest.mark.parametrize('refresh', [False, True])
def test_replaced_assessment_manifest_rejects_both_old_and_relabelled_proposals(canonical_decision, refresh):
    (st, item_id, update, Body, request), old_manifest = canonical_decision
    new_manifest = seal_assessment(st, 'new-assessment')
    assert new_manifest != old_manifest
    expected = expectations(st, item_id)
    expected['expected_source_revision'] = new_manifest if refresh else old_manifest
    with pytest.raises(Exception) as exc:
        update(item_id, Body(**expected), request)
    assert getattr(exc.value, 'status_code', None) == 409
    assert st.get_hitl_item(item_id)['status'] == 'pending'
    assert st.list_decisions('s1') == []
    assert not any(j['type'] == 'apply_approved_values' for j in st.list_jobs())
