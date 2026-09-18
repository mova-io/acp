"""Real durable intent, incremental job and exact provider receipt presentation."""
import pytest
import automatic_release
import automatic_release_store
import progress_evidence
from test_remediation_waterfall_view import seed


def setup(store, scope=147):
    run = seed(store)
    files = [f'file-{n}.pdf' for n in range(scope)]
    with store._db.cursor() as cur:
        for file in files:
            store._db.execute(cur, 'INSERT INTO file_records(scan_id,file,corrected_sha256) VALUES(%s,%s,%s)',
                              ('scan', file, 'a' * 64))
    release = store.ensure_release_execution('scan', 'owner', 'local', scope,
        preferred_folder_name='Authorized batch')
    authorization = automatic_release_store.create(store, 'owner', 'scan', run, 'batch',
        {'files': {file: {'checksum': 'frozen-source'} for file in files},
         'destination': {'provider': 'local', 'folder_id': 'root', 'folder_name': 'Download package'},
         'release_parent_id': None, 'release_folder_name': release['folder_name']})
    entries = {file: {'state': 'waiting'} for file in files}
    for file in files[:9]:
        digest = 'sha256:' + 'a' * 64
        entries[file] = {'state': 'published', 'artifact_digest': digest}
        store.record_release_document(release['id'], 'owner',
            {'file': file, 'status': 'published', 'artifact_digest': digest})
    automatic_release_store.save(store, authorization, status='waiting', progress={'files': entries})
    execution = store.enqueue_stage_batch('scan', 'release', 'publish_file',
        [{'owner': 'owner', 'scan_id': 'scan', 'file': files[-1], 'automatic_release_id': authorization['id']}],
        snapshot_id='corrected', request_fingerprint='incremental')['batch_id']
    return execution, authorization, release, files


def test_incremental_request_retains_its_ledger_and_shows_only_approved_cumulative_scope(isolated_store):
    store = isolated_store
    execution, authorization, _, _ = setup(store)
    snapshot = store.stage_execution_snapshot(execution, owner='owner')
    assert snapshot['domain_reconciliation']['total'] == 1
    batch = progress_evidence.read(store, execution, owner='owner')['release_batch_progress']
    assert {k: v for k, v in batch.items() if k not in {'scope_id', 'buckets', 'file_membership'}} == {'available': True, 'authorization_id': authorization['id'], 'run_id': authorization['run_id'],
                     'total': 147, 'delivered': 9, 'remaining': 138, 'status': 'waiting', 'revision': 1}
    assert progress_evidence.read(store, execution, owner='intruder')['file_processing']['available'] is False


def test_actual_authorize_contract_and_settled_sharepoint_receipts(isolated_store, monkeypatch):
    """Use the real permission builder and continuation's bare digest storage."""
    import workspace_roles
    import workspace_rollout
    store = isolated_store
    owner, scan = 'actual-release@example.com', 'actual-release-contract'
    files = ['first.pptx', 'second.pptx', 'third.pptx']
    digest = 'a' * 64
    monkeypatch.setattr(workspace_rollout, 'enforcement_active', lambda: True)
    monkeypatch.setattr(workspace_roles, 'access_for_email',
                        lambda *a, **k: {'capabilities': ['release.publish']})
    with store._db.cursor() as cur:
        store._db.execute(cur, "INSERT INTO scan_runs(id,owner_email,source,status,rubric_hash) "
                          "VALUES(%s,%s,'sharepoint','done','fixture-rubric')", (scan, owner))
        for file in files:
            store._db.execute(cur, "INSERT INTO file_records(scan_id,file,engine,status,score,compliant,"
                "remediated_at,corrected_sha256,drive_file_id,source_modified,checksum) "
                "VALUES(%s,%s,'office','analysed',100,1,'2026-09-09T00:00:00Z',%s,%s,"
                "'2026-09-01T00:00:00Z','source-hash')", (scan, file, digest, file))
            store._db.execute(cur, "INSERT INTO scan_inventory(scan_id,file,drive_file_id,drive_id,"
                "source_modified,checksum) VALUES(%s,%s,%s,'library','2026-09-01T00:00:00Z','source-hash')",
                (scan, file, file))
    run = store.enqueue_stage_batch(scan, 'remediate', 'remediate_file',
        [{'owner': owner, 'scan_id': scan, 'file': file} for file in files],
        snapshot_id=store.remediation_source_revision(scan), request_fingerprint='real-contract')['batch_id']
    with store._db.cursor() as cur:
        store._db.execute(cur, "UPDATE jobs SET status='done' WHERE batch_id=%s", (run,))
    destination = automatic_release.preview(store, scan, owner, files)['destination']
    authorization = automatic_release.authorize(store, scan, owner, run, files, destination,
        'actual-contract-request', allow_remaining_issues=True)
    assert set(authorization['intent']['files']) == set(files)
    assert authorization['intent']['release_parent_id'] == 'library/root'
    release = store.ensure_release_execution(scan, owner, 'sharepoint', 3,
        preferred_folder_name=authorization['intent']['release_folder_name'],
        parent_folder_id=destination['folder_id'], parent_folder_name=destination['folder_name'])
    for file in files[:2]:
        authorization = automatic_release_store.update_file(store, authorization['id'], owner, file,
            {'state': 'publishing', 'artifact_digest': digest, 'waiting_for_delivery': True})
        store.record_release_document(release['id'], owner,
            {'file': file, 'status': 'published', 'artifact_digest': 'sha256:' + digest})
        assert automatic_release.receipt(store, authorization, file, digest)['status'] == 'published'
    # SQL receipts settle delivery even while the continuation's progress is publishing.
    assert automatic_release.public(authorization, store)['progress']['published'] == 2
    execution = store.enqueue_stage_batch(scan, 'release', 'publish_file',
        [{'owner': owner, 'scan_id': scan, 'file': files[-1],
          'automatic_release_id': authorization['id'], 'release_id': release['id'],
          'artifact_digest': 'sha256:' + digest}],
        snapshot_id='actual-corrected', request_fingerprint='actual-incremental')['batch_id']
    assert store.stage_execution_snapshot(execution, owner=owner)['domain_reconciliation']['total'] == 1
    batch = progress_evidence.read(store, execution, owner=owner)['release_batch_progress']
    assert batch['available'] is True
    assert (batch['total'], batch['delivered'], batch['remaining']) == (3, 2, 1)
    assert batch['authorization_id'] == authorization['id'] and batch['run_id'] == run
    assert automatic_release.public(authorization, store)['batch_progress'] == batch


@pytest.mark.parametrize('mutation', ['digest', 'current_artifact', 'destination', 'progress_only', 'unrelated_authorization', 'replaced_run'])
def test_unconfirmed_receipts_or_unrelated_identity_never_inflate_delivered_scope(isolated_store, mutation):
    store = isolated_store
    execution, authorization, release, files = setup(store, scope=12)
    with store._db.cursor() as cur:
        if mutation == 'digest':
            store._db.execute(cur, "UPDATE release_documents SET artifact_digest='sha256:wrong' WHERE release_id=%s", (release['id'],))
        elif mutation == 'current_artifact':
            store._db.execute(cur, 'UPDATE file_records SET corrected_sha256=%s WHERE scan_id=%s', ('b' * 64, 'scan'))
        elif mutation == 'destination':
            store._db.execute(cur, "UPDATE release_executions SET folder_name='Other destination' WHERE id=%s", (release['id'],))
        elif mutation == 'progress_only':
            store._db.execute(cur, 'DELETE FROM release_documents WHERE release_id=%s', (release['id'],))
        elif mutation == 'unrelated_authorization':
            store._db.execute(cur, "UPDATE automatic_release_authorizations SET owner_email='other' WHERE id=%s", (authorization['id'],))
        else:
            store._db.execute(cur, 'UPDATE stage_executions SET is_current=0 WHERE execution_id=%s', (authorization['run_id'],))
    batch = progress_evidence.read(store, execution, owner='owner')['release_batch_progress']
    if mutation in {'unrelated_authorization', 'replaced_run'}:
        assert batch == {'available': False, 'scope': 'automatic'}
    else:
        assert batch['total'] == 12 and batch['delivered'] == 0 and batch['remaining'] == 12


def test_completed_files_do_not_complete_pending_report_package(isolated_store):
    store = isolated_store
    execution, authorization, _, _ = setup(store, scope=3)
    job = store.enqueue_job('release_package', {'owner': 'owner', 'scan_id': 'scan'}, scan_id='scan')
    current = automatic_release_store.get(store, authorization['id'], 'owner')
    automatic_release_store.save(store, current, status='completed',
        progress={**current['progress'], '_package_job_id': job})
    batch = progress_evidence.read(store, execution, owner='owner')['release_batch_progress']
    assert batch['delivered'] == 3 and batch['remaining'] == 0
    assert batch['status'] == 'publishing'
    with store._db.cursor() as cur:
        store._db.execute(cur, "UPDATE jobs SET status='done' WHERE id=%s", (job,))
    assert progress_evidence.read(store, execution, owner='owner')['release_batch_progress']['status'] == 'completed'


def test_unavailable_delivery_projection_is_not_mistaken_for_manual_scope(isolated_store, monkeypatch):
    import release_batch_progress
    store = isolated_store
    execution, _, _, _ = setup(store, scope=3)
    stage = store.get_stage_execution(execution, owner='owner')
    def unavailable(*args, **kwargs):
        raise RuntimeError('synthetic lookup unavailable')
    monkeypatch.setattr(store, 'release_for_scan', unavailable)
    assert release_batch_progress.read(store, stage) == {'available': False, 'scope': 'unknown'}


def test_only_request_payloads_without_automatic_identity_prove_manual_scope(isolated_store):
    import release_batch_progress
    store = isolated_store
    seed(store)
    execution = store.enqueue_stage_batch('scan', 'release', 'publish_file',
        [{'owner': 'owner', 'scan_id': 'scan', 'file': 'manual.pdf'}],
        snapshot_id='corrected', request_fingerprint='manual')['batch_id']
    assert release_batch_progress.read(store, store.get_stage_execution(execution, owner='owner')) == {
        'available': False, 'scope': 'manual'}


def test_saved_plan_partition_uses_exact_receipts_and_keeps_unknown_states_visible(isolated_store):
    store = isolated_store
    execution, authorization, _, files = setup(store, scope=12)
    current = automatic_release_store.get(store, authorization['id'], 'owner')
    entries = current['progress']['files']
    entries[files[9]] = {'state': 'publishing'}
    entries[files[10]] = {'state': 'blocked'}
    entries[files[11]] = {'state': 'published'}
    automatic_release_store.save(store, current, status='waiting', progress={'files': entries})
    batch = progress_evidence.read(store, execution, owner='owner')['release_batch_progress']
    assert batch['scope_id'] == authorization['id']
    assert batch['buckets'] == dict(waiting=0, processing=1, published=9, failed=1, skipped=0, unclassified=1)
    assert sum(batch['buckets'].values()) == batch['total']
    assert batch['file_membership'][files[11]] == 'unclassified'
    assert batch['revision'] == 2


def test_same_parent_and_folder_receipts_from_other_provider_do_not_count(isolated_store):
    store = isolated_store
    execution, authorization, release, _ = setup(store, scope=3)
    with store._db.cursor() as cur:
        store._db.execute(cur, "UPDATE release_executions SET source='drive' WHERE id=%s", (release['id'],))
    batch = progress_evidence.read(store, execution, owner='owner')['release_batch_progress']
    assert batch['delivered'] == 0
    assert batch['remaining'] == 3
    assert automatic_release.receipt(store, authorization, 'file-0.pdf', 'a' * 64) is None


@pytest.mark.parametrize('destination', [None, 'legacy-default', {'provider': 'unknown'}, {}])
def test_missing_legacy_destination_identity_is_explicitly_unavailable(isolated_store, destination):
    import release_batch_progress
    store = isolated_store
    _, authorization, _, _ = setup(store, scope=3)
    authorization['intent']['destination'] = destination
    assert release_batch_progress.read_authorization(store, authorization) == {
        'available': False, 'scope': 'automatic', 'reason': 'destination_identity_unavailable'}
    assert automatic_release.receipt(store, authorization, 'file-0.pdf', 'a' * 64) is None


def test_correction_after_publication_counts_only_once_its_current_copy_is_delivered(isolated_store):
    """The plan froze and delivered V1. An approved correction then saved V2: the delivered copy is
    out of date and must not count. After the owner's explicit republish delivers V2's exact
    receipt, the CURRENT copy of that authorized document is at the destination and counts —
    the tile must not stay at 'not delivered' for a document that is now current."""
    store = isolated_store
    execution, _, release, files = setup(store, scope=12)
    delivered = lambda: progress_evidence.read(store, execution, owner='owner')['release_batch_progress']
    assert delivered()['delivered'] == 9
    with store._db.cursor() as cur:
        store._db.execute(cur, 'UPDATE file_records SET corrected_sha256=%s WHERE scan_id=%s AND file=%s',
                          ('b' * 64, 'scan', files[0]))
    batch = delivered()
    assert batch['delivered'] == 8 and batch['file_membership'][files[0]] != 'published'
    store.record_release_document(release['id'], 'owner',
        {'file': files[0], 'status': 'published', 'artifact_digest': 'sha256:' + 'b' * 64,
         'published_at': '2026-09-18T14:00:00+00:00'})
    batch = delivered()
    assert batch['delivered'] == 9 and batch['file_membership'][files[0]] == 'published'
