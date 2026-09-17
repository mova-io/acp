"""Automatic checks are admitted pipeline work, never credited approval or resolution."""
from types import SimpleNamespace
import pytest
from test_ai_standing_approval import seed, proposal, OWNER, SID, FILE
from ai_run_policy import run_context
from ai_run_approval_override import read, save
from automatic_review_queue import annotate


def setup_queue(store, monkeypatch):
    job = seed(store, monkeypatch, enabled=False)
    with run_context(store, job['payload'], job) as ctx:
        item = store.enqueue_proposals(SID, FILE, '2.4.6', [proposal(store)])
        setting = read(store, OWNER, SID, ctx.run_id)
        save(store, OWNER, SID, ctx.run_id, True, 0, setting['source_revision'])
    return item, ctx.run_id


def test_owner_route_projects_exact_pending_checks_without_mutation(isolated_store, monkeypatch):
    from routes.hitl import hitl_list
    import ai_standing_approval
    item, run_id = setup_queue(isolated_store, monkeypatch)
    monkeypatch.setattr(ai_standing_approval, '_source', lambda *a, **k: pytest.fail('Polling must not read provider or blob bytes'))
    rows = hitl_list(SimpleNamespace(state=SimpleNamespace(user_email=OWNER)), scan_id=SID)
    row = next(row for row in rows if row['id'] == item)
    assert row['auto_approval_status'] == 'checking'
    assert row['auto_approval_run_id'] == run_id
    assert row['status'] == 'pending' and not row.get('applied') and not row.get('validated')
    assert isolated_store.get_hitl_item(item)['status'] == 'pending'
    assert annotate(isolated_store, rows, 'other@example.test')[0].get('auto_approval_status') is None
    assert annotate(isolated_store, [isolated_store.get_hitl_item(item)], 'other@example.test')[0].get('auto_approval_status') is None


@pytest.mark.parametrize('failure', ['off', 'finished', 'source', 'proposal', 'manual'])
def test_unadmitted_stale_or_manual_rows_remain_human_input(isolated_store, monkeypatch, failure):
    item, run_id = setup_queue(isolated_store, monkeypatch)
    row = isolated_store.get_hitl_item(item)
    if failure == 'off':
        state = read(isolated_store, OWNER, SID, run_id)
        save(isolated_store, OWNER, SID, run_id, False, 1, state['source_revision'])
    elif failure == 'finished':
        with isolated_store._db.cursor() as cur:
            isolated_store._db.execute(cur, "UPDATE jobs SET status='done' WHERE scan_id=%s AND type='apply_approved_values'", (SID,))
    elif failure == 'source':
        monkeypatch.setattr(isolated_store, 'remediation_source_revision', lambda sid: 'changed')
    elif failure == 'proposal':
        row['proposals'][0]['proposed_value'] = 'not the recorded exact output'
    else:
        row['proposals'] = []
    projected = annotate(isolated_store, [row], OWNER)[0]
    assert projected.get('auto_approval_status') is None
    assert projected['status'] == 'pending'


def test_deferred_reason_survives_finished_coordinator_and_is_exact_scope(isolated_store, monkeypatch):
    from automatic_review_queue import record
    item_id, run_id = setup_queue(isolated_store, monkeypatch)
    row = isolated_store.get_hitl_item(item_id)
    state = read(isolated_store, OWNER, SID, run_id)
    record(isolated_store, OWNER, SID, run_id, state['source_revision'], row,
           'review_required', 'This change requires individual review')
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, "UPDATE jobs SET status='done' WHERE scan_id=%s AND type='apply_approved_values'", (SID,))
    projected = annotate(isolated_store, [row], OWNER)[0]
    assert projected['automatic_approval']['state'] == 'review_required'
    assert projected['automatic_approval']['reason'] == 'This change requires individual review'
    assert projected['automatic_approval']['owner'] == 'You'
    assert projected['status'] == 'pending' and not projected.get('applied')
    stale = {**row, 'proposal_snapshot_ids': ['replacement']}
    assert 'automatic_approval' not in annotate(isolated_store, [stale], OWNER)[0]
    assert 'automatic_approval' not in annotate(isolated_store, [projected], 'other@example.test')[0]


def test_admitted_writer_disposition_reconciles_finished_job(isolated_store, monkeypatch):
    from ai_run_approval_override import process_pending
    item_id, run_id = setup_queue(isolated_store, monkeypatch)
    state = read(isolated_store, OWNER, SID, run_id)
    process_pending(isolated_store, dict(owner=OWNER, scan_id=SID, run_id=run_id, source_revision=state['source_revision']))
    row = isolated_store.get_hitl_item(item_id)
    assert row['status'] == 'approved'
    projected = annotate(isolated_store, [row], OWNER)[0]
    assert projected['automatic_approval']['state'] == 'queued'
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, "UPDATE jobs SET status='dead' WHERE scan_id=%s AND type='apply_approved_values'", (SID,))
    assert annotate(isolated_store, [row], OWNER)[0]['automatic_approval']['state'] == 'blocked'
    assert not isolated_store.get_hitl_item(item_id).get('applied')


def test_explicit_judgment_failure_is_human_but_missing_metadata_is_status_check(isolated_store, monkeypatch):
    import ai_standing_approval
    item, run_id = setup_queue(isolated_store, monkeypatch)
    row = isolated_store.get_hitl_item(item)
    def human(*args, **kwargs):
        raise ValueError('The draft contradicts visible image evidence; individual review is required')
    monkeypatch.setattr(ai_standing_approval, 'eligible_item', human)
    marker = annotate(isolated_store, [row], OWNER)[0]['automatic_approval']
    assert marker['responsibility'] == 'human'
    assert marker['run_id'] == run_id and marker['scan_id'] == SID
    def check(*args, **kwargs):
        raise ValueError('Proposal version unavailable')
    monkeypatch.setattr(ai_standing_approval, 'eligible_item', check)
    marker = annotate(isolated_store, [row], OWNER)[0]['automatic_approval']
    assert marker['responsibility'] == 'check'
    assert isolated_store.get_hitl_item(item)['status'] == 'pending'


def test_applied_without_active_verifier_needs_system_check_not_another_approval(isolated_store, monkeypatch):
    from automatic_review_queue import record
    item_id, run_id = setup_queue(isolated_store, monkeypatch)
    state = read(isolated_store, OWNER, SID, run_id)
    row = isolated_store.get_hitl_item(item_id)
    record(isolated_store, OWNER, SID, run_id, state['source_revision'], row, 'queued', 'Automatically approved')
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, "UPDATE jobs SET status='done' WHERE scan_id=%s AND type='apply_approved_values'", (SID,))
    projected = annotate(isolated_store, [{**row, 'status':'approved', 'applied':True, 'validated':False}], OWNER)[0]
    marker = projected['automatic_approval']
    assert marker['state'] == 'blocked' and marker['responsibility'] == 'check'
    # The copy moved to `automatic_review_queue.stalled_reason` and is now shown to the user
    # verbatim, so it says what is true in plain words. What it must NOT do is claim no
    # verification was ever recorded — a finished job is not an absent one — while still making
    # clear that re-approving is not the move. Both are asserted, the wording is not.
    assert 'not yet independently checked' in marker['reason']
    assert 'Another approval is not needed.' in marker['reason']
    assert 'is recorded' not in marker['reason']
    assert projected['applied'] and not projected['validated']
