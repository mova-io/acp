"""Exact-intent migration of interrupted Drive delivery into durable queue work."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
import pytest
import automatic_release as flow
import automatic_release_store as persistence
from routes.scans import publish_files as real_publish_files
from test_automatic_release_service import prepared, authorize, tick, OWNER, SID, FILE, DIGEST


@pytest.fixture
def drive(prepared, monkeypatch):
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur, "UPDATE scan_runs SET source='drive' WHERE id=%s", (SID,))
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur, 'UPDATE stage_executions SET input_snapshot_id=%s WHERE execution_id=%s', (prepared.store.remediation_source_revision(SID), prepared.run))
    monkeypatch.setattr(flow, 'request_for', lambda owner, sid: SimpleNamespace(state=SimpleNamespace(user_email=owner), headers={'x-drive-token': 'fixture-token'}))
    return prepared


def admitted(f):
    row = authorize(f)
    row = flow.publish_admission(f.store, row['id'], OWNER, SID, FILE, DIGEST)
    release = f.store.ensure_release_execution(SID, OWNER, 'drive', 1,
        preferred_folder_name=row['intent']['release_folder_name'],
        parent_folder_id=row['intent']['release_parent_id'])
    return row, release


def payload(row, release, file=FILE):
    return dict(scan_id=SID, owner=OWNER, automatic_release_id=row['id'], release_id=release['id'],
        file=file, artifact_digest='sha256:' + DIGEST, remediated_at='2026-09-09T00:00:00Z')


def queue(f, row, release, file=FILE):
    return f.store.enqueue_automatic_drive_release(SID, [payload(row, release, file)],
        snapshot_id='fixture-snapshot', request_fingerprint='fixture-publish')


def legacy(f, row, release, *, seconds=-1):
    stage = f.store.ensure_synchronous_stage_execution(scan_id=SID, stage='release', input_ids=[FILE],
        input_snapshot_id='fixture-snapshot', request_fingerprint='legacy-sync')
    effect = f.store.reserve_side_effect(execution_id=stage['execution_id'], work_item_id=stage['items'][FILE],
        effect_type='drive.publish', destination=f"google:me:{release['id']}:{FILE}",
        content_digest=DIGEST, worker_id='sync-release', now=(datetime.now(timezone.utc)+timedelta(seconds=seconds-300)).isoformat())
    return stage, effect


def test_adopts_expired_sync_lease_without_changing_receipt_identity(drive):
    row, release = admitted(drive)
    stage, effect = legacy(drive, row, release)
    result = queue(drive, row, release)
    assert result['batch_id'] == stage['execution_id']
    with drive.store._db.cursor() as cur:
        drive.store._db.execute(cur, 'SELECT * FROM stage_work_items WHERE execution_id=%s', (stage['execution_id'],))
        work = drive.store._db.fetchone(cur)
        assert work['work_item_id'] == stage['items'][FILE]
        assert work['job_id'] == result['job_ids'][0]
        drive.store._db.execute(cur, 'SELECT * FROM side_effect_receipts WHERE effect_id=%s', (effect['effect_id'],))
        preserved = drive.store._db.fetchone(cur)
        assert preserved['reservation_token'] == effect['reservation_token']
    assert queue(drive, row, release)['job_ids'] == result['job_ids']


def test_live_legacy_lease_is_not_requeued(drive):
    row, release = admitted(drive)
    legacy(drive, row, release, seconds=300)
    with pytest.raises(ValueError, match='reservation to expire'):
        queue(drive, row, release)


def test_mismatched_legacy_digest_is_not_adopted(drive):
    row, release = admitted(drive)
    stage, effect = legacy(drive, row, release)
    with drive.store._db.cursor() as cur:
        drive.store._db.execute(cur, 'UPDATE side_effect_receipts SET content_digest=%s WHERE effect_id=%s', ('b'*64, effect['effect_id']))
    with pytest.raises(ValueError, match='reservation does not match'):
        queue(drive, row, release)


def test_missing_worker_is_dispatched_with_original_frozen_digest(drive):
    row, release = admitted(drive)
    row = tick(drive, row)
    assert len(drive.calls) == 1
    assert drive.calls[0]['expected_artifacts'] == {FILE: DIGEST}
    row = tick(drive, row)
    assert len(drive.calls) == 1


def test_missing_drive_grant_requests_reconnect_without_upload(drive, monkeypatch):
    row, release = admitted(drive)
    monkeypatch.setattr(flow, 'request_for', lambda *a: SimpleNamespace(headers={}))
    row = tick(drive, row)
    assert flow.public(row)['requires_reconnect']
    assert flow.public(row)['needs_attention']
    assert not drive.calls
    assert row['status'] == 'blocked'
    assert row['progress']['_tick_revision'] < row['revision']


def test_resume_preserves_intent_and_cannot_revive_stopped_permission(drive):
    row, release = admitted(drive)
    before = row['intent']
    row = flow.resume(drive.store, row['id'], OWNER, SID)
    assert row['intent'] == before
    assert row['progress']['files'][FILE]['resume_requested']
    persistence.stop(drive.store, row['id'], OWNER)
    with pytest.raises(ValueError, match='cannot be resumed'):
        flow.resume(drive.store, row['id'], OWNER, SID)


def test_resume_rejects_changed_bytes_without_clearing_admission(drive):
    row, release = admitted(drive)
    with drive.store._db.cursor() as cur:
        drive.store._db.execute(cur, 'UPDATE file_records SET corrected_sha256=%s WHERE scan_id=%s', ('b'*64, SID))
    with pytest.raises(ValueError, match='corrected copy changed'):
        flow.resume(drive.store, row['id'], OWNER, SID)
    assert persistence.get(drive.store, row['id'], OWNER)['progress']['files'][FILE]['artifact_digest'] == DIGEST


def test_failed_worker_requires_explicit_resume_before_requeue(drive):
    row, release = admitted(drive)
    result = queue(drive, row, release)
    with drive.store._db.cursor() as cur:
        drive.store._db.execute(cur, "UPDATE jobs SET status='dead' WHERE id=%s", (result['job_ids'][0],))
    with pytest.raises(ValueError, match='resume explicitly'):
        queue(drive, row, release)
    row = flow.resume(drive.store, row['id'], OWNER, SID)
    assert queue(drive, row, release)['job_ids'] == result['job_ids']


def test_recorded_exact_receipt_finishes_without_reupload(drive):
    row, release = admitted(drive)
    drive.store.record_release_document(release['id'], OWNER, dict(file=FILE, status='published', artifact_digest='sha256:' + DIGEST))
    row = tick(drive, row)
    assert row['status'] == 'completed'
    assert not drive.calls


def add_second(f):
    other = 'second.pptx'
    with f.store.transaction():
        with f.store._db.cursor() as cur:
            f.store._db.execute(cur, "INSERT INTO file_records(scan_id,file,engine,status,score,compliant,remediated_at,corrected_sha256,drive_file_id,source_modified,checksum) SELECT scan_id,%s,engine,status,score,compliant,remediated_at,corrected_sha256,'second-source',source_modified,checksum FROM file_records WHERE scan_id=%s AND file=%s", (other, SID, FILE))
            f.store._db.execute(cur, "INSERT INTO scan_inventory(scan_id,file,drive_file_id,drive_id,source_modified,checksum) SELECT scan_id,%s,'second-source',drive_id,source_modified,checksum FROM scan_inventory WHERE scan_id=%s AND file=%s", (other, SID, FILE))
            job = f.store.enqueue_job('remediate_file', dict(scan_id=SID, owner=OWNER, file=other), scan_id=SID, batch_id=f.run)
            f.store._db.execute(cur, "UPDATE jobs SET status='done' WHERE id=%s", (job,))
            f.store._db.execute(cur, "INSERT INTO stage_work_items(work_item_id,execution_id,input_id,job_id,state,revision,attempt,created_at,updated_at) VALUES(%s,%s,%s,%s,'completed',1,0,%s,%s)", (f.store._work_item_identity(f.run, other), f.run, other, job, f.store._now(), f.store._now()))
            f.store._db.execute(cur, 'UPDATE stage_executions SET input_snapshot_id=%s WHERE execution_id=%s', (f.store.remediation_source_revision(SID), f.run))
    return other


def test_ready_second_file_joins_same_execution_while_first_waits(drive):
    other = add_second(drive)
    destination = flow.preview(drive.store, SID, OWNER, [FILE, other])['destination']
    row = flow.authorize(drive.store, SID, OWNER, drive.run, [FILE, other], destination, 'both')
    row = flow.publish_admission(drive.store, row['id'], OWNER, SID, FILE, DIGEST)
    release = drive.store.ensure_release_execution(SID, OWNER, 'drive', 2,
        preferred_folder_name=row['intent']['release_folder_name'], parent_folder_id=row['intent']['release_parent_id'])
    stage, effect = legacy(drive, row, release, seconds=300)
    # Admission and queue for the second file can proceed while the first lease is live.
    row = flow.publish_admission(drive.store, row['id'], OWNER, SID, other, DIGEST)
    result = queue(drive, row, release, other)
    assert result['batch_id'] == stage['execution_id']
    with drive.store._db.cursor() as cur:
        drive.store._db.execute(cur, 'SELECT expected_items FROM stage_executions WHERE execution_id=%s', (stage['execution_id'],))
        assert drive.store._db.fetchone(cur)['expected_items'] == 2
    with pytest.raises(ValueError, match='reservation to expire'):
        queue(drive, row, release)


def test_foreign_automatic_job_cannot_join_same_stage(drive):
    row, release = admitted(drive)
    result = queue(drive, row, release)
    with drive.store._db.cursor() as cur:
        wrong = dict(payload(row, release), automatic_release_id='other-authorization')
        drive.store._db.execute(cur, 'UPDATE jobs SET payload=%s WHERE id=%s', (json.dumps(wrong), result['job_ids'][0]))
    with pytest.raises(ValueError, match='different delivery intent'):
        queue(drive, row, release)


def test_explicit_resume_preserves_failed_stage_and_reserved_provider_identity(drive):
    row, release = admitted(drive)
    result = queue(drive, row, release)
    with drive.store._db.cursor() as cur:
        drive.store._db.execute(cur, "UPDATE jobs SET status='dead' WHERE id=%s", (result['job_ids'][0],))
        drive.store._db.execute(cur, "UPDATE stage_executions SET state='failed' WHERE execution_id=%s", (result['batch_id'],))
    row = flow.resume(drive.store, row['id'], OWNER, SID)
    retry = drive.store.enqueue_automatic_drive_release(SID, [payload(row, release)], snapshot_id='fixture-snapshot', request_fingerprint='different-request-shape')
    assert retry['batch_id'] == result['batch_id']
    assert retry['job_ids'] == result['job_ids']


def test_manual_explicit_retry_adopts_legacy_exact_receipt(drive):
    row, release = admitted(drive)
    stage, effect = legacy(drive, row, release)
    request = payload(row, release)
    request.pop('automatic_release_id')
    result = drive.store.enqueue_manual_drive_release(SID, [request], owner=OWNER,
        snapshot_id='fixture-snapshot', request_fingerprint='explicit-manual-retry')
    assert result['batch_id'] == stage['execution_id']
    with drive.store._db.cursor() as cur:
        drive.store._db.execute(cur, 'SELECT execution_id,work_item_id FROM side_effect_receipts WHERE effect_id=%s', (effect['effect_id'],))
        assert drive.store._db.fetchone(cur) == dict(execution_id=stage['execution_id'], work_item_id=stage['items'][FILE])


def test_manual_retry_cannot_take_an_automatic_worker(drive):
    row, release = admitted(drive)
    queue(drive, row, release)
    request = payload(row, release)
    request.pop('automatic_release_id')
    with pytest.raises(ValueError, match='different delivery intent'):
        drive.store.enqueue_manual_drive_release(SID, [request], owner=OWNER,
            snapshot_id='fixture-snapshot', request_fingerprint='explicit-manual-retry')


def test_cancelled_release_stage_is_not_silently_revived(drive):
    row, release = admitted(drive)
    result = queue(drive, row, release)
    with drive.store._db.cursor() as cur:
        drive.store._db.execute(cur, "UPDATE jobs SET status='cancelled' WHERE id=%s", (result['job_ids'][0],))
        drive.store._db.execute(cur, "UPDATE stage_executions SET state='cancelled' WHERE execution_id=%s", (result['batch_id'],))
    row = flow.resume(drive.store, row['id'], OWNER, SID)
    with pytest.raises(ValueError, match='cannot accept additional'):
        queue(drive, row, release)


def test_manual_subset_retry_keeps_unselected_completed_sibling(drive):
    other = add_second(drive)
    release = drive.store.ensure_release_execution(SID, OWNER, 'drive', 2, preferred_folder_name='fixture', parent_folder_id='root')
    def manual(file):
        return dict(scan_id=SID, owner=OWNER, release_id=release['id'], file=file,
                    artifact_digest='sha256:' + DIGEST, remediated_at='2026-09-09T00:00:00Z')
    result = drive.store.enqueue_manual_drive_release(SID, [manual(FILE), manual(other)], owner=OWNER,
        snapshot_id='fixture-snapshot', request_fingerprint='manual-both')
    with drive.store._db.cursor() as cur:
        drive.store._db.execute(cur, "UPDATE jobs SET status='done' WHERE id=%s", (result['job_ids'][0],))
        drive.store._db.execute(cur, "UPDATE stage_work_items SET state='completed' WHERE job_id=%s", (result['job_ids'][0],))
        drive.store._db.execute(cur, "UPDATE jobs SET status='dead' WHERE id=%s", (result['job_ids'][1],))
        drive.store._db.execute(cur, "UPDATE stage_executions SET state='failed' WHERE execution_id=%s", (result['batch_id'],))
    retry = drive.store.enqueue_manual_drive_release(SID, [manual(other)], owner=OWNER,
        snapshot_id='fixture-snapshot', request_fingerprint='manual-subset')
    assert retry['batch_id'] == result['batch_id']
    assert retry['job_ids'] == [result['job_ids'][1]]
    with drive.store._db.cursor() as cur:
        drive.store._db.execute(cur, 'SELECT status FROM jobs WHERE id=%s', (result['job_ids'][0],))
        assert drive.store._db.fetchone(cur)['status'] == 'done'


def test_manual_new_file_joins_legacy_live_sibling_without_touching_lease(drive):
    other = add_second(drive)
    row, release = admitted(drive)
    stage, effect = legacy(drive, row, release, seconds=300)
    request = payload(row, release, other)
    request.pop('automatic_release_id')
    result = drive.store.enqueue_manual_drive_release(SID, [request], owner=OWNER,
        snapshot_id='fixture-snapshot', request_fingerprint='manual-new-file')
    assert result['batch_id'] == stage['execution_id']
    with drive.store._db.cursor() as cur:
        drive.store._db.execute(cur, 'SELECT reservation_token FROM side_effect_receipts WHERE effect_id=%s', (effect['effect_id'],))
        assert drive.store._db.fetchone(cur)['reservation_token'] == effect['reservation_token']


def test_route_worker_receipt_and_automatic_completion_round_trip(drive, monkeypatch):
    import hashlib
    import core
    import handlers
    import publish
    from routes import scans
    from test_drive_publish_worker import Drive, DATA
    digest = hashlib.sha256(DATA).hexdigest()
    with drive.store._db.cursor() as cur:
        drive.store._db.execute(cur, 'UPDATE file_records SET corrected_sha256=%s WHERE scan_id=%s', (digest, SID))
    row = authorize(drive)
    release = drive.store.ensure_release_execution(SID, OWNER, 'drive', 1,
        preferred_folder_name=row['intent']['release_folder_name'], parent_folder_id=row['intent']['release_parent_id'],
        parent_folder_name=row['intent']['destination']['folder_name'])
    drive.store.record_release_root(release['id'], OWNER, 'drive', 'google:me', 'output-root', release['folder_name'], 'https://drive/output-root')
    svc = Drive()
    svc.items['root'] = ({'id': 'root', 'mimeType': 'application/vnd.google-apps.folder', 'capabilities': {'canAddChildren': True}}, b'')
    svc.items['source-item'] = ({'id': 'source-item', 'modifiedTime': '2026-09-01T00:00:00Z'}, b'')
    monkeypatch.setattr(scans, 'publish_files', real_publish_files)
    monkeypatch.setattr(scans, '_register_scan_tokens', lambda *a, **k: None)
    monkeypatch.setattr(scans, '_sealed_stage_input', lambda *a: ('fixture-snapshot', None))
    monkeypatch.setattr(core, 'get_scan_tokens', lambda *a: {'drive': 'fixture-token'})
    monkeypatch.setattr(core, 'drive_service', lambda *a: svc)
    monkeypatch.setattr(handlers, '_drive_client', lambda *a: svc)
    monkeypatch.setattr(publish._blob, 'download_remediated', lambda *a: DATA)
    row = tick(drive, row)
    with drive.store._db.cursor() as cur:
        drive.store._db.execute(cur, "SELECT * FROM jobs WHERE type='publish_file'")
        job = drive.store._db.fetchone(cur)
    assert job is not None, row['progress']
    queued = json.loads(job['payload'])
    flow.publish_job(drive.store, queued, job, handlers._publish_file_guarded)
    receipt = drive.store.get_release_document(release['id'], FILE, OWNER)
    assert receipt['status'] == 'published'
    assert receipt['artifact_digest'] == 'sha256:' + digest
    row = tick(drive, row)
    assert row['status'] == 'completed'
    assert svc.created == ['planned-1']
    # A continuation replay consumes the receipt, never issues another create.
    tick(drive, row)
    assert svc.created == ['planned-1']


@pytest.fixture(autouse=True)
def _candidate_assessment_for_delivery_fixture(monkeypatch):
    # Identity/recovery fixtures use sentinel A/B or corrected fixture bytes.
    # Full saved-document scanner gates are proven in test_release_candidate_assessment.
    import release_candidate_assessment
    monkeypatch.setattr(release_candidate_assessment, 'assess_candidate',
                        lambda *args, **kwargs: {'fixture_assessment': True})
