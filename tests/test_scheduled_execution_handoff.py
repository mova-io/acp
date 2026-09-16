"""Scheduled successor ownership, immutable limits and server-owned source authority."""
import json
from datetime import datetime, timedelta, timezone
import pytest

OWNER = 'owner@example.test'

@pytest.fixture
def accepted(isolated_store):
    import scheduled_scan_store as execution
    st = isolated_store
    now = datetime.now(timezone.utc)
    st.save_user_scan_schedule(OWNER, True, 'UTC', '09:00', list(range(7)), source='drive',
                               source_scope={'include_ids': ['accepted-folder']})
    payload = {'owner_email': OWNER, 'occurrence_key': 'owner:day', 'scheduled_for': now.isoformat()}
    assert st.enqueue_scheduled_sweep(payload['occurrence_key'], payload)
    job = st.claim_job('shared-worker', job_types=['scheduled_sweep'])
    cfg = st.get_user_scan_schedule(OWNER)
    row = execution.accept(st, payload, job, cfg, now=now, timeout_seconds=300)
    return execution, st, row, job, now


def test_handoff_releases_worker_and_preserves_owner_watermark(accepted):
    execution, st, row, job, now = accepted
    old_watermark = st.get_user_scan_schedule(OWNER)['last_enqueued_occurrence']
    updated = execution.handoff(st, row, job, phase='awaiting_assess', now=now)
    assert updated['execution_revision'] == 1
    assert updated['execution_deadline'] == row['execution_deadline']
    assert updated['execution_failures'] == 0
    active = st.active_scheduled_sweep(OWNER)
    assert active['id'] != job['id'] and active['status'] == 'queued'
    assert st.get_job(active['id'])['scheduled_owner'] == OWNER
    assert st.get_user_scan_schedule(OWNER)['last_enqueued_occurrence'] == old_watermark
    # The released generic worker can immediately claim an ordinary child.
    st.enqueue_job('scan_finalize', {'scan_id': row['scan_id']}, scan_id=row['scan_id'])
    child = st.claim_job('shared-worker')
    assert child['type'] == 'scan_finalize'
    with st._db.cursor() as cur:
        st._db.execute(cur, "SELECT status FROM jobs WHERE id=%s", (job['id'],))
        assert st._db.fetchone(cur)['status'] == 'done'


def test_failed_successor_insert_rolls_back_current_claim(accepted, monkeypatch):
    execution, st, row, job, now = accepted
    original = st._db.execute
    def failing(cur, sql, params=()):
        if 'INSERT INTO jobs' in sql:
            raise RuntimeError('injected handoff failure')
        return original(cur, sql, params)
    monkeypatch.setattr(st._db, 'execute', failing)
    with pytest.raises(RuntimeError, match='injected handoff failure'):
        execution.handoff(st, row, job, phase='awaiting_assess', now=now)
    assert execution.get(st, OWNER, row['occurrence_key'])['execution_revision'] == 0
    active = st.active_scheduled_sweep(OWNER)
    assert active['id'] == job['id'] and active['status'] == 'running'


def test_duplicate_or_stale_claim_cannot_handoff_again(accepted):
    execution, st, row, job, now = accepted
    execution.handoff(st, row, job, phase='awaiting_assess', now=now)
    with pytest.raises(ValueError):
        execution.handoff(st, row, job, phase='awaiting_assess', now=now)
    active = st.active_scheduled_sweep(OWNER)
    assert active['id'] == execution.get(st, OWNER, row['occurrence_key'])['execution_job_id']


def test_deadline_and_wake_budget_are_not_extended(accepted):
    execution, st, row, job, now = accepted
    with pytest.raises(TimeoutError):
        execution.handoff(st, row, job, phase='awaiting_assess', now=now + timedelta(seconds=301))
    assert execution.get(st, OWNER, row['occurrence_key'])['execution_deadline'] == row['execution_deadline']


def test_retry_acceptance_cannot_rebind_source_authority(accepted):
    execution, st, row, job, now = accepted
    cfg = st.get_user_scan_schedule(OWNER)
    cfg['source_scope'] = {'include_ids': ['foreign-folder']}
    again = execution.accept(st, json.loads(job['payload']) if isinstance(job['payload'], str) else job['payload'],
                             job, cfg, now=now, timeout_seconds=86400)
    assert again['execution_context'] == row['execution_context']
    assert again['execution_deadline'] == row['execution_deadline']


def test_failure_budget_is_cumulative_and_attempts_are_counted_once(accepted):
    execution, st, row, job, now = accepted
    one = execution.record_failure(st, row, job, now=now)
    duplicate = execution.record_failure(st, one, job, now=now)
    assert one['execution_failures'] == duplicate['execution_failures'] == 1
    next_row = execution.handoff(st, duplicate, job, phase='awaiting_assess', now=now)
    assert next_row['execution_failures'] == 1


def scoped_child(accepted, provider='drive', *, registered=True):
    execution, st, row, parent, now = accepted
    item = {'file': 'active.html', 'drive_file_id': 'item', 'drive_account_id': 'Account-A',
            'parent_folder': 'accepted-folder'}
    st.init_scan_run(row['scan_id'], 'drive', 1, now.isoformat(), 'default', 'r', owner=OWNER)
    st.add_inventory(row['scan_id'], [item])
    payload = {'scan_id': row['scan_id'], 'source': provider, 'user': OWNER, **item}
    if registered:
        snapshot = st.stage_snapshot_id(row['scan_id'])
        batch = st.enqueue_stage_batch(row['scan_id'], 'assess', 'scan_assess',
            [{'scan_id': row['scan_id'], 'source': 'drive', 'user': OWNER}],
            snapshot_id=snapshot, request_fingerprint='fixture')
        row = execution.handoff(st, row, parent, phase='awaiting_assess',
                                updates={'assess_batch_id': batch['batch_id'], 'snapshot_id': snapshot})
        assessment = st.claim_job('assess-worker', job_types=['scan_assess'])
        execution.enqueue_content(st, assessment, 'scan_file', payload)
    else:
        st.enqueue_job('scan_file', payload, scan_id=row['scan_id'])
    child = st.claim_job('child-worker', job_types=['scan_file'])
    return execution, st, row, child, item


@pytest.mark.parametrize('change', ['owner', 'provider', 'account', 'item', 'scan', 'claim'])
def test_source_authority_cannot_be_forged_or_widened(accepted, change):
    execution, st, row, child, item = scoped_child(accepted)
    owner, provider, scan = OWNER, 'drive', row['scan_id']
    if change == 'owner': owner = 'foreign@example.test'
    elif change == 'provider': provider = 'onedrive'
    elif change == 'account': item['drive_account_id'] = 'Account-B'
    elif change == 'item': item['drive_file_id'] = 'unlisted-item'
    elif change == 'scan':
        assert execution.source_context(st, 'guessed-scan', child, provider=provider, owner=owner, item=item) is None
        return
    elif change == 'claim': child['locked_by'] = 'impostor'
    with pytest.raises(ValueError):
        execution.source_context(st, scan, child, provider=provider, owner=owner, item=item)


def test_source_context_is_server_owned_and_bound_to_claimed_item(accepted):
    execution, st, row, child, item = scoped_child(accepted)
    context = execution.source_context(st, row['scan_id'], child, provider='drive', owner=OWNER, item=item)
    assert context['auth_mode'] == 'scheduled_adc'
    assert context['source_scope']['include_ids'] == ['accepted-folder']
    assert 'token' not in context


def test_mutable_adc_flag_without_acceptance_never_authorizes_source(isolated_store):
    import scheduled_scan_store as execution
    st = isolated_store
    st.init_scan_run('ordinary', 'drive', 1, 'now', 'default', 'r', owner=OWNER)
    item = {'file': 'active.html', 'drive_file_id': 'item', 'drive_account_id': 'Account-A'}
    st.add_inventory('ordinary', [item])
    st.enqueue_job('scan_file', {'scan_id': 'ordinary', 'user': OWNER, 'source': 'drive',
                                'scheduled_adc': True, **item}, scan_id='ordinary')
    child = st.claim_job('worker', job_types=['scan_file'])
    assert execution.source_context(st, 'ordinary', child, provider='drive', owner=OWNER, item=item) is None


def test_unregistered_job_on_active_owned_scan_cannot_inherit_adc(accepted):
    execution, st, row, child, item = scoped_child(accepted, registered=False)
    # Exact tenant/source/item and even a real claim are insufficient without server lineage.
    assert execution.source_context(st, row['scan_id'], child, provider='drive', owner=OWNER, item=item) is None


def test_binding_payload_mutation_and_cancelled_occurrence_deny_content(accepted):
    execution, st, row, child, item = scoped_child(accepted)
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE jobs SET payload=%s WHERE id=%s", (json.dumps({**child['payload'], 'source': 'onedrive'}), child['id']))
    with pytest.raises(ValueError, match='binding changed'):
        execution.source_context(st, row['scan_id'], child, provider='drive', owner=OWNER, item=item)
    with st._db.cursor() as cur:
        st._db.execute(cur, "UPDATE jobs SET payload=%s WHERE id=%s", (json.dumps(child['payload']), child['id']))
    assert st.cancel_scan(row['scan_id'], owner=OWNER)
    assert execution.get(st, OWNER, row['occurrence_key'])['result'] == 'cancelled'
    with pytest.raises(ValueError):
        execution.source_context(st, row['scan_id'], child, provider='drive', owner=OWNER, item=item)


def test_full_reset_erases_accepted_context_and_descendant_binding(accepted):
    execution, st, row, child, item = scoped_child(accepted)
    st.reset_analytics()
    assert execution.get(st, OWNER, row['occurrence_key']) is None
    assert st.get_job(child['id']) is None


def test_handoff_updates_cannot_change_accepted_scope_or_auth_mode(accepted):
    execution, st, row, job, now = accepted
    with pytest.raises(ValueError, match='accepted authority'):
        execution.handoff(st, row, job, phase='awaiting_assess', now=now, updates={'auth_mode': 'client_adc'})
    assert execution.get(st, OWNER, row['occurrence_key'])['execution_revision'] == 0


def test_inputs_are_frozen_before_discovery_or_retry(accepted):
    execution, st, row, job, now = accepted
    st.create_disposition_policy('later', name='later', match='[]', action='archive', action_config='{}',
                                 requires_approval=False, enabled=True, owner_email=OWNER)
    again = execution.accept(st, job['payload'], job, st.get_user_scan_schedule(OWNER), now=now)
    assert again['execution_context']['inputs'] == row['execution_context']['inputs']
    assert not again['execution_context']['inputs']['lifecycle_rules']


def test_current_tick_cancellation_stops_children_and_closes_occurrence(accepted):
    from worker import JobCancelledError
    execution, st, row, child, item = scoped_child(accepted)
    current = st.claim_job('tick-worker', job_types=['scheduled_sweep'])
    if not current:
        with st._db.cursor() as cur:
            st._db.execute(cur, "UPDATE jobs SET run_after=%s WHERE id=%s", (st._now(), row['execution_job_id']))
        current = st.claim_job('tick-worker', job_types=['scheduled_sweep'])
    assert st.request_job_cancellation(current['id'])
    with pytest.raises(JobCancelledError):
        execution.handoff(st, row, current, phase='awaiting_assess')
    assert st.mark_job_cancelled(current['id'], worker_id=current['locked_by'], attempt=current['attempts'])
    assert execution.get(st, OWNER, row['occurrence_key'])['result'] == 'cancelled'
    assert st.get_job(child['id'])['cancel_requested_at']


def test_ordinary_missing_token_still_cannot_create_adc(isolated_store, monkeypatch):
    import core
    import handlers
    import scanner
    import scheduled_scan_execution as staged
    monkeypatch.setattr(core, 'store', isolated_store)
    monkeypatch.setattr(scanner, '_drive_service', lambda *args: pytest.fail('ordinary job gained ADC'))
    assert staged.services_for_job(isolated_store, 'ordinary', {}, source='drive', user=OWNER) is None
    with pytest.raises(RuntimeError, match='token missing'):
        handlers._make_svc('drive', {})


def test_scheduled_real_failures_close_budget_and_notify_once(accepted, monkeypatch):
    import core
    import scheduled_scan_execution as staged
    execution, st, row, job, now = accepted
    monkeypatch.setattr(core, 'get_store', lambda: st)
    monkeypatch.setattr(staged, '_discovery_plan', lambda *args: (_ for _ in ()).throw(RuntimeError('offline failure')))
    payload = job['payload']
    for failure in range(1, 4):
        if failure < 3:
            with pytest.raises(RuntimeError, match='offline failure'):
                staged.run_tick(payload, job)
            with st._db.cursor() as cur:
                st._db.execute(cur, 'UPDATE jobs SET attempts=attempts+1 WHERE id=%s', (job['id'],))
            job = st.get_job(job['id'])
        else:
            staged.run_tick(payload, job)
    ended = execution.get(st, OWNER, row['occurrence_key'])
    assert ended['execution_failures'] == 3 and ended['result'] == 'failed'
    assert len(st.list_schedule_notifications(OWNER)) == 1
    assert not st.active_scheduled_sweep(OWNER)


def test_parent_replay_reuses_frozen_content_admission(accepted):
    execution, st, row, child, item = scoped_child(accepted)
    binding = json.loads(st.get_job(child['id'])['scheduled_execution_binding'])
    parent = st.get_job(binding['parent_job_id'])
    changed = {**child['payload'], 'drive_file_id': 'unlisted-item', 'file': 'other.html'}
    replay = execution.enqueue_contents(st, parent, [('scan_file', changed)])
    assert replay == {'job_ids': [child['id']], 'reused': True, 'file_count': 1}
    assert st.get_job(child['id'])['payload']['file'] == item['file']
    assert len([job for job in st.list_jobs() if job['type'] == 'scan_file']) == 1



def test_parent_replay_rejects_mutated_previously_admitted_payload(accepted):
    execution, st, row, child, item = scoped_child(accepted)
    binding = json.loads(st.get_job(child['id'])['scheduled_execution_binding'])
    parent = st.get_job(binding['parent_job_id'])
    changed = {**child['payload'], 'drive_file_id': 'different-item'}
    with st._db.cursor() as cur:
        st._db.execute(cur, 'UPDATE jobs SET payload=%s WHERE id=%s',
                       (json.dumps(changed), child['id']))
    with pytest.raises(ValueError, match='payload changed'):
        execution.enqueue_contents(st, parent, [('scan_file', child['payload'])])
    assert len([job for job in st.list_jobs() if job['type'] == 'scan_file']) == 1

def test_entire_content_admission_rolls_back_with_binding_failure(accepted, monkeypatch):
    execution, st, row, child, item = scoped_child(accepted)
    binding = json.loads(st.get_job(child['id'])['scheduled_execution_binding'])
    parent = st.get_job(binding['parent_job_id'])
    with st._db.cursor() as cur:
        st._db.execute(cur, 'DELETE FROM jobs WHERE id=%s', (child['id'],))
    original = st._db.execute
    def fail(cur, sql, params=()):
        if 'UPDATE jobs SET scheduled_execution_binding=' in sql:
            raise RuntimeError('injected binding failure')
        return original(cur, sql, params)
    monkeypatch.setattr(st._db, 'execute', fail)
    with pytest.raises(RuntimeError, match='injected binding failure'):
        execution.enqueue_contents(st, parent, [('scan_file', child['payload'])])
    assert not any(job['type'] == 'scan_file' for job in st.list_jobs())
    assert st.get_job(parent['id'])['status'] == 'running'


def test_stale_assessment_claim_cannot_bind_new_content(accepted):
    execution, st, row, child, item = scoped_child(accepted)
    binding = json.loads(st.get_job(child['id'])['scheduled_execution_binding'])
    parent = st.get_job(binding['parent_job_id'])
    with st._db.cursor() as cur:
        st._db.execute(cur, 'DELETE FROM jobs WHERE id=%s', (child['id'],))
        st._db.execute(cur, "UPDATE jobs SET attempts=attempts+1,locked_by='replacement' WHERE id=%s", (parent['id'],))
    with pytest.raises(ValueError, match='claim changed'):
        execution.enqueue_contents(st, parent, [('scan_file', child['payload'])])
    assert not any(job['type'] == 'scan_file' for job in st.list_jobs())


@pytest.mark.parametrize('timeout', [0, 299, 86401])
def test_timeout_configuration_outside_bounds_is_rejected(accepted, timeout):
    execution, st, row, job, now = accepted
    with pytest.raises(ValueError, match='between five minutes'):
        execution.accept(st, job['payload'], job, st.get_user_scan_schedule(OWNER), now=now, timeout_seconds=timeout)


def test_singleton_slot_prevents_new_occurrence_while_children_advance(isolated_store):
    st = isolated_store
    assert st.enqueue_scheduled_sweep('5:first')
    assert not st.enqueue_scheduled_sweep('5:second')
    first = st.claim_job('singleton-worker', job_types=['scheduled_sweep'])
    assert st.complete_job(first['id'], worker_id=first['locked_by'], attempt=first['attempts'])
    assert st.enqueue_scheduled_sweep('5:second')


def test_exhausted_tick_closes_occurrence_when_handler_failure_was_not_recorded(accepted):
    execution, st, row, job, now = accepted
    with st._db.cursor() as cur:
        st._db.execute(cur, 'UPDATE jobs SET attempts=3 WHERE id=%s', (job['id'],))
    assert st.fail_job(job['id'], 'offline fixture failure', worker_id=job['locked_by'], attempt=3) == 'dead'
    assert st.get_job(job['id'])['status'] == 'dead'
    assert execution.get(st, OWNER, row['occurrence_key'])['result'] == 'failed'
    assert not st.active_scheduled_sweep(OWNER)
    assert st.fail_job(job['id'], 'replay', worker_id=job['locked_by'], attempt=3) == 'stale'


def test_deployment_drain_preserves_accepted_execution_without_failure(accepted, monkeypatch):
    import core
    import scheduled_scan_execution as staged
    from worker import JobDrainingError
    execution, st, row, job, now = accepted
    monkeypatch.setattr(core, 'get_store', lambda: st)
    def draining(*args):
        raise JobDrainingError('offline deployment handoff')
    monkeypatch.setattr(staged, '_discovery_plan', draining)
    with pytest.raises(JobDrainingError):
        staged.run_tick(job['payload'], job)
    current = execution.get(st, OWNER, row['occurrence_key'])
    assert current['execution_context'] == row['execution_context']
    assert current['execution_deadline'] == row['execution_deadline']
    assert current['execution_failures'] == 0
    assert not current['completed_at']


@pytest.mark.parametrize('changed', ['library', 'app'])
def test_sharepoint_changed_accepted_library_or_app_denies_before_token(isolated_store, monkeypatch, changed):
    import sp_sync
    import scheduled_scan_execution as staged
    st = isolated_store
    monkeypatch.setattr(sp_sync, 'sp_sync_configured', lambda: True)
    monkeypatch.setattr(sp_sync, 'sync_drive_id', lambda: 'Library-A')
    monkeypatch.setattr(sp_sync, '_cfg', lambda key: {'ACP_SP_SYNC_TENANT_ID': 'tenant', 'ACP_SP_SYNC_CLIENT_ID': 'client'}[key])
    context = {'config': {'scheduled_drive_id': 'Library-A',
                         'scheduled_app_identity': st.canonical_request_fingerprint(['tenant', 'client'])}}
    monkeypatch.setattr(sp_sync, 'app_token', lambda: pytest.fail('changed accepted source obtained app token'))
    if changed == 'library':
        monkeypatch.setattr(sp_sync, 'sync_drive_id', lambda: 'Library-B')
    else:
        monkeypatch.setattr(sp_sync, '_cfg', lambda key: 'changed-app')
    with pytest.raises(ValueError):
        staged._scheduled_app_token(context)
