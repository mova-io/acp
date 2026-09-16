"""Missing captions enter the existing bounded, consent-fenced recovery pipeline."""
import json
from types import SimpleNamespace
import pytest
import vision_recovery as recovery
from ai_run_policy import run_context
from test_vision_recovery import seed, SID, FILE, OWNER


def clear_retries(store):
    with store._db.cursor() as cur:
        store._db.execute(cur, "DELETE FROM jobs WHERE scan_id=%s AND type='vision_proposal_retry'", (SID,))


def test_missing_draft_without_transport_miss_is_scheduled_once(isolated_store):
    job, _ = seed(isolated_store)
    clear_retries(isolated_store)
    with run_context(isolated_store, job['payload'], job) as context:
        recovery.schedule(isolated_store, context, job, [], inspect_pending=True)
        recovery.schedule(isolated_store, context, job, [], inspect_pending=True)
    assert len(isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')) == 1


def test_complete_drafts_are_not_sent_again(isolated_store):
    job, payload = seed(isolated_store)
    clear_retries(isolated_store)
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, 'UPDATE hitl_queue SET proposals=%s,finding_count=1 WHERE id=%s',
            (json.dumps([{'locator':'word/document.xml#r1', 'proposed_value':'Usable authored caption'}]), payload['item_id']))
    with run_context(isolated_store, job['payload'], job) as context:
        recovery.schedule(isolated_store, context, job, [], inspect_pending=True)
    assert not isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')


@pytest.mark.parametrize('reason,code', [
    ('provider_usage_unknown', 'vision_spending_reconciliation_required'),
    ('existing_draft_attempt_requires_reconciliation', 'vision_spending_reconciliation_required'),
    ('provider_access_denied', 'vision_provider_access_denied'),
    ('budget_admission_denied', 'vision_budget_admission_denied'),
    ('run_dispatch_permission_unavailable', 'vision_run_permission_unavailable'),
    ('verified_model_pricing_unavailable', 'vision_pricing_not_verified'),
    ('provider_limit_exceeded', 'vision_provider_limit_exceeded'),
])
def test_missing_draft_exposes_block_without_dispatch(isolated_store, reason, code):
    job, _ = seed(isolated_store)
    clear_retries(isolated_store)
    with run_context(isolated_store, job['payload'], job) as context:
        context.deferred.append({'reason':reason})
        recovery.schedule(isolated_store, context, job, [], inspect_pending=True)
    queued = isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')
    assert len(queued) == (1 if code == 'vision_spending_reconciliation_required' else 0)
    if queued:
        assert json.loads(queued[0]['payload'])['waiting_spending'] is True
    assert isolated_store.list_scan_events(SID)[-1]['detail']['reason_code'] == code


def test_durable_spending_uncertainty_blocks_even_without_captured_reason():
    context = SimpleNamespace(enabled=True, deferred=[], owner_id=OWNER, run_id='run',
        ledger=SimpleNamespace(snapshot=lambda *args: {'blocked':True, 'available_units':1000}))
    assert recovery._recovery_block(context) == 'vision_spending_reconciliation_required'


def test_exhausted_budget_does_not_queue_another_paid_call():
    context = SimpleNamespace(enabled=True, deferred=[], owner_id=OWNER, run_id='run',
        ledger=SimpleNamespace(snapshot=lambda *args: {'blocked':False, 'available_units':0}))
    assert recovery._recovery_block(context) == 'vision_budget_exhausted'


def test_reconciliation_waits_without_generation_then_resumes_same_allowance(isolated_store, monkeypatch):
    from test_vision_recovery import draft
    import remediate_office, blob
    from test_vision_recovery import DATA
    monkeypatch.setattr(blob, 'download_remediated', lambda *args: DATA)
    job, original = seed(isolated_store)
    clear_retries(isolated_store)
    waiting = dict(original, waiting_spending=True, wait_check=1)
    recovery._enqueue(isolated_store, waiting)
    monkeypatch.setattr(recovery, '_recovery_block', lambda context, misses=(), check_admission=True: 'vision_spending_reconciliation_required')
    monkeypatch.setattr(remediate_office, 'alt_proposals_for_office', lambda *a, **k: pytest.fail('paid generation while usage unknown'))
    recovery.process(isolated_store, waiting)
    assert len(isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')) == 2
    monkeypatch.setattr(recovery, '_recovery_block', lambda context, misses=(), check_admission=True: None)
    recovery.process(isolated_store, waiting)
    recovery.process(isolated_store, waiting)
    jobs = isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')
    normal = [json.loads(j['payload']) for j in jobs if not json.loads(j['payload']).get('waiting_spending')]
    assert normal == [original]
    draft(monkeypatch)
    import ai_standing_approval
    approvals = []
    monkeypatch.setattr(ai_standing_approval, 'approve_file', lambda store, context: approvals.append(context.run_id))
    recovery.process(isolated_store, normal[0])
    assert approvals == [original['run_id']]
    assert isolated_store.get_hitl_item(original['item_id'])['proposals'][0]['proposed_value']


def test_unknown_usage_checks_stop_after_eight_and_do_not_restart(isolated_store, monkeypatch):
    import blob
    from test_vision_recovery import DATA
    monkeypatch.setattr(blob, 'download_remediated', lambda *args: DATA)
    _, original = seed(isolated_store)
    clear_retries(isolated_store)
    monkeypatch.setattr(recovery, '_recovery_block', lambda context, misses=(), check_admission=True: 'vision_spending_reconciliation_required')
    for check in range(1, 9):
        waiting = dict(original, waiting_spending=True, wait_check=check)
        recovery._enqueue(isolated_store, waiting)
        recovery.process(isolated_store, waiting)
        recovery.process(isolated_store, waiting)
    queued = isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')
    assert len(queued) == 8
    assert all(json.loads(j['payload'])['waiting_spending'] for j in queued)


def test_null_proposals_can_recover_without_losing_review_population(isolated_store, monkeypatch):
    from test_vision_recovery import draft
    job, previous = seed(isolated_store)
    clear_retries(isolated_store)
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, 'UPDATE hitl_queue SET proposals=NULL WHERE id=%s', (previous['item_id'],))
    with run_context(isolated_store, job['payload'], job) as context:
        recovery.schedule(isolated_store, context, job, [], inspect_pending=True)
    payload = json.loads(isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')[0]['payload'])
    assert payload['proposals_before'] is None
    draft(monkeypatch)
    recovery.process(isolated_store, payload)
    row = isolated_store.get_hitl_item(previous['item_id'])
    assert row['proposals'][0]['proposed_value'] == 'A useful recovered caption'
    assert row['finding_count'] == 8


def test_current_saved_run_can_resume_missing_drafts_without_rescan(isolated_store):
    job, previous = seed(isolated_store)
    clear_retries(isolated_store)
    recovery.schedule_existing_pending(isolated_store, OWNER, SID, previous['run_id'])
    recovery.schedule_existing_pending(isolated_store, OWNER, SID, previous['run_id'])
    assert len(isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')) == 1
    assert len(isolated_store.list_scan_jobs_of_type(SID, 'remediate_file')) == 1
    with pytest.raises(ValueError):
        recovery.schedule_existing_pending(isolated_store, 'other@example.test', SID, previous['run_id'])


def test_partial_nonempty_drafts_do_not_hide_missing_finding_coverage(isolated_store):
    job, payload = seed(isolated_store)
    clear_retries(isolated_store)
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, 'UPDATE hitl_queue SET proposals=%s WHERE id=%s',
            (json.dumps([{'locator':'word/document.xml#r1', 'proposed_value':'Usable caption'}]),payload['item_id']))
    with run_context(isolated_store,job['payload'],job) as context:
        recovery.schedule(isolated_store,context,job,[],inspect_pending=True)
    assert len(isolated_store.list_scan_jobs_of_type(SID,'vision_proposal_retry')) == 1
    assert isolated_store.get_hitl_item(payload['item_id'])['finding_count'] == 8
