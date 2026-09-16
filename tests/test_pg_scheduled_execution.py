"""Scheduled same-owner handoff and server binding on PostgreSQL's actual transactions."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import pytest

pytestmark = pytest.mark.skipif(not os.environ.get('DATABASE_URL', '').startswith('postgres'),
                                reason='requires the disposable PostgreSQL integration database')

@pytest.fixture
def accepted_pg():
    import store
    import scheduled_scan_store as execution
    st = store.Store()
    assert st._db.supports_for_update, 'integration must exercise PostgreSQL'
    suffix = uuid.uuid4().hex
    owner, key = 'staged-' + suffix + '@example.test', 'staged:' + suffix
    st.save_user_scan_schedule(owner, True, 'UTC', '09:00', list(range(7)), source='drive',
                               source_scope={'include_ids': ['folder']})
    payload = {'owner_email': owner, 'occurrence_key': key, 'scheduled_for': st._now()}
    assert st.enqueue_scheduled_sweep(key, payload)
    # Claim only this fixture's elected job; never consume another integration case's work.
    with st.transaction(), st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE jobs SET status='running',locked_by=%s,attempts=1 "
                       "WHERE scheduled_owner=%s AND status='queued' RETURNING *",
                       ('pg-worker-' + suffix, owner))
        job = st._db.fetchone(cur)
    assert job is not None
    row = execution.accept(st, payload, job, st.get_user_scan_schedule(owner))
    return execution, st, row, job


def test_pg_concurrent_claim_handoff_elects_one_successor(accepted_pg):
    execution, st, row, job = accepted_pg
    def offer():
        try:
            execution.handoff(st, row, job, phase='awaiting_discover')
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: offer(), range(2)))
    assert outcomes.count(True) == 1
    active = st.active_scheduled_sweep(row['owner_email'])
    assert active['id'] == execution.get(st, row['owner_email'], row['occurrence_key'])['execution_job_id']
    assert st.get_job(job['id'])['status'] == 'done'


def test_pg_insertion_failure_preserves_old_claim_and_revision(accepted_pg, monkeypatch):
    execution, st, row, job = accepted_pg
    original = st._db.execute
    def fail(cur, sql, params=()):
        if 'INSERT INTO jobs' in sql:
            raise RuntimeError('injected successor rollback')
        return original(cur, sql, params)
    monkeypatch.setattr(st._db, 'execute', fail)
    with pytest.raises(RuntimeError, match='injected successor rollback'):
        execution.handoff(st, row, job, phase='awaiting_discover')
    assert execution.get(st, row['owner_email'], row['occurrence_key'])['execution_revision'] == 0
    assert st.get_job(job['id'])['status'] == 'running'


def test_pg_server_descendant_binding_and_payload_tamper_denial(accepted_pg):
    import json
    execution, st, row, tick = accepted_pg
    owner, sid = row['owner_email'], row['scan_id']
    item = {'file': 'active.html', 'drive_file_id': 'item', 'drive_account_id': 'Account-A'}
    st.init_scan_run(sid, 'drive', 1, st._now(), 'default', 'r', owner=owner)
    st.add_inventory(sid, [item])
    snapshot = st.stage_snapshot_id(sid)
    payload = {'scan_id': sid, 'source': 'drive', 'user': owner}
    batch = st.enqueue_stage_batch(sid, 'assess', 'scan_assess', [payload],
                                   snapshot_id=snapshot, request_fingerprint='fixture')
    row = execution.handoff(st, row, tick, phase='awaiting_assess',
                            updates={'assess_batch_id': batch['batch_id'], 'snapshot_id': snapshot})
    with st.transaction(), st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE jobs SET status='running',locked_by='pg-assessor',attempts=1 "
                       "WHERE scan_id=%s AND type='scan_assess' AND status='queued' RETURNING *", (sid,))
        assessment = st._db.fetchone(cur)
    child_id = execution.enqueue_content(st, assessment, 'scan_file', {**payload, **item})
    with st.transaction(), st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE jobs SET status='running',locked_by='pg-content',attempts=1 "
                       "WHERE id=%s AND status='queued' RETURNING *", (child_id,))
        child = st._db.fetchone(cur)
    assert child['id'] == child_id
    assert execution.source_context(st, sid, child, provider='drive', owner=owner, item=item)
    with st._db.cursor() as cur:
        st._db.execute(cur, 'UPDATE jobs SET payload=%s WHERE id=%s',
                       (json.dumps({**st.get_job(child_id)['payload'], 'drive_file_id': 'foreign'}), child_id))
    with pytest.raises(ValueError, match='binding changed'):
        execution.source_context(st, sid, child, provider='drive', owner=owner, item=item)


def test_pg_tenant_reset_revokes_accepted_tick_without_root_scan(accepted_pg):
    execution, st, row, tick = accepted_pg
    owner = row['owner_email']
    assert execution.source_context(st, row['scan_id'], tick, provider='drive', owner=owner)
    st.reset_user_data('  ' + owner.upper() + ' ')
    assert execution.get(st, owner, row['occurrence_key']) is None
    assert st.get_job(tick['id']) is None
    assert execution.source_context(st, row['scan_id'], tick, provider='drive', owner=owner) is None
