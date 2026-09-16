"""SharePoint reconnection retains the original job and exact delivery evidence."""
import json
import pytest
import automatic_release as flow
import automatic_release_store as persistence
from test_automatic_release_service import prepared, authorize, tick, OWNER, SID, FILE, DIGEST


def interrupted(f):
    row = authorize(f)
    row = flow.publish_admission(f.store, row['id'], OWNER, SID, FILE, DIGEST)
    release = f.store.ensure_release_execution(SID, OWNER, 'sharepoint', 1,
        preferred_folder_name=row['intent']['release_folder_name'], parent_folder_id=row['intent']['release_parent_id'])
    payload = dict(release_id=release['id'], scan_id=SID, owner=OWNER, file=FILE, automatic_release_id=row['id'],
                   artifact_digest='sha256:' + DIGEST, remediated_at='2026-09-09T00:00:00Z')
    batch = f.store.enqueue_stage_batch(SID, 'release', 'publish_file', [payload],
        snapshot_id='saved-release-snapshot', request_fingerprint='saved-delivery')
    with f.store._db.cursor() as cur:
        f.store._db.execute(cur, "UPDATE jobs SET status='dead' WHERE id=%s", (batch['job_ids'][0],))
        f.store._db.execute(cur, "UPDATE stage_executions SET state='failed' WHERE execution_id=%s", (batch['batch_id'],))
    return row, batch


def test_sharepoint_dead_worker_stays_blocked_until_resume(prepared):
    row, batch = interrupted(prepared)
    row = tick(prepared, row)
    assert row['progress']['files'][FILE]['state'] == 'blocked'
    assert flow.public(row)['can_resume']
    assert not prepared.calls
    assert prepared.store.get_job(batch['job_ids'][0])['status'] == 'dead'


def test_resume_revives_same_job_payload_and_reservation(prepared):
    row, batch = interrupted(prepared)
    job_before = prepared.store.get_job(batch['job_ids'][0])
    effect = prepared.store.reserve_side_effect(execution_id=batch['batch_id'],
        work_item_id=prepared.store._work_item_identity(batch['batch_id'], FILE),
        effect_type='sharepoint.publish', destination='graph:library:folder:' + FILE,
        content_digest=DIGEST, worker_id='stopped-worker')
    row = flow.resume(prepared.store, row['id'], OWNER, SID)
    row = tick(prepared, row)
    job_after = prepared.store.get_job(batch['job_ids'][0])
    assert job_after['status'] == 'queued'
    assert job_after['payload'] == job_before['payload']
    assert job_after['batch_id'] == job_before['batch_id']
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur, 'SELECT reservation_token FROM side_effect_receipts WHERE effect_id=%s', (effect['effect_id'],))
        assert prepared.store._db.fetchone(cur)['reservation_token'] == effect['reservation_token']
    assert not prepared.calls
    tick(prepared, row)
    assert not prepared.calls


@pytest.mark.parametrize('change', ['cancelled', 'superseded', 'digest'])
def test_changed_or_stopped_job_is_not_revived(prepared, change):
    row, batch = interrupted(prepared)
    row = flow.resume(prepared.store, row['id'], OWNER, SID)
    with prepared.store._db.cursor() as cur:
        if change == 'cancelled':
            prepared.store._db.execute(cur, "UPDATE jobs SET status='cancelled' WHERE id=%s", (batch['job_ids'][0],))
        elif change == 'superseded':
            prepared.store._db.execute(cur, 'UPDATE stage_executions SET is_current=0 WHERE execution_id=%s', (batch['batch_id'],))
        else:
            payload = dict(prepared.store.get_job(batch['job_ids'][0])['payload'])
            payload['artifact_digest'] = 'sha256:' + 'b' * 64
            prepared.store._db.execute(cur, 'UPDATE jobs SET payload=%s WHERE id=%s', (json.dumps(payload), batch['job_ids'][0]))
    row = tick(prepared, row)
    assert row['progress']['files'][FILE]['state'] == 'blocked'
    assert prepared.store.get_job(batch['job_ids'][0])['status'] in {'dead', 'cancelled'}
    assert not prepared.calls


def test_exact_receipt_finishes_without_reviving_dead_job(prepared):
    row, batch = interrupted(prepared)
    release = prepared.store.ensure_release_execution(SID, OWNER, 'sharepoint', 1,
        preferred_folder_name=row['intent']['release_folder_name'],
        parent_folder_id=row['intent']['release_parent_id'])
    prepared.store.record_release_document(release['id'], OWNER,
        dict(file=FILE, status='published', artifact_digest='sha256:' + DIGEST))
    row = flow.resume(prepared.store, row['id'], OWNER, SID)
    row = tick(prepared, row)
    assert row['status'] == 'completed'
    assert prepared.store.get_job(batch['job_ids'][0])['status'] == 'dead'
    assert not prepared.calls


def test_legacy_active_failed_file_is_projected_and_resumed(prepared):
    row, batch = interrupted(prepared)
    row = persistence.update_file(prepared.store, row['id'], OWNER, FILE,
                                 dict(state='failed', message='Delivery job stopped or failed.'))
    projected = flow.public(row, prepared.store)
    assert projected['file_progress'][FILE]['state'] == 'blocked'
    assert projected['can_resume'] and projected['requires_reconnect']
    assert persistence.get(prepared.store, row['id'], OWNER)['progress']['files'][FILE]['state'] == 'failed'
    row = tick(prepared, row)
    assert row['status'] == 'blocked'
    assert row['progress']['_tick_revision'] < row['revision']
    assert flow.public(row, prepared.store)['needs_attention']
    assert row['progress']['files'][FILE]['state'] == 'blocked'
    assert prepared.store.get_job(batch['job_ids'][0])['status'] == 'dead'
    row = flow.resume(prepared.store, row['id'], OWNER, SID)
    row = tick(prepared, row)
    assert prepared.store.get_job(batch['job_ids'][0])['status'] == 'queued'
    assert not prepared.calls


def test_legacy_globally_failed_permission_remains_terminal(prepared):
    row, batch = interrupted(prepared)
    row = persistence.update_file(prepared.store, row['id'], OWNER, FILE, dict(state='failed'))
    row = persistence.save(prepared.store, row, status='failed', progress=row['progress'], schedule=False)
    assert flow.public(row, prepared.store)['file_progress'][FILE]['state'] == 'failed'
    assert not flow.public(row, prepared.store)['can_resume']
    with pytest.raises(ValueError, match='cannot be resumed'):
        flow.resume(prepared.store, row['id'], OWNER, SID)
    assert prepared.store.get_job(batch['job_ids'][0])['status'] == 'dead'
