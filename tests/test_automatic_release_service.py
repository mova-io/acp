"""Synthetic tracked SharePoint records + local jobs; never provider publication."""
import json
from types import SimpleNamespace
import pytest
import automatic_release as flow
import automatic_release_store as persistence

OWNER='release-fixture@example.com'
SID='automatic-fixture'
FILE='deck.pptx'
DIGEST='a'*64


@pytest.fixture
def prepared(isolated_store,monkeypatch):
    import core
    import workspace_roles
    import workspace_rollout
    from routes import scans
    store=isolated_store
    monkeypatch.setattr(core,'store',store)
    monkeypatch.setattr(workspace_rollout,'enforcement_active',lambda:True)
    monkeypatch.setattr(workspace_roles,'access_for_email',lambda *a,**k:{'capabilities':['release.publish']})
    with store._db.cursor() as cur:
        store._db.execute(cur,"INSERT INTO scan_runs(id,owner_email,source,status,rubric_hash) VALUES(%s,%s,'sharepoint','done','fixture-rubric')",(SID,OWNER))
        store._db.execute(cur,"""INSERT INTO file_records(scan_id,file,engine,status,score,compliant,remediated_at,corrected_sha256,drive_file_id,source_modified,checksum)
            VALUES(%s,%s,'office','analysed',100,1,'2026-09-09T00:00:00Z',%s,'source-item','2026-09-01T00:00:00Z','source-hash')""",(SID,FILE,DIGEST))
        store._db.execute(cur,"INSERT INTO scan_inventory(scan_id,file,drive_file_id,drive_id,source_modified,checksum) VALUES(%s,%s,'source-item','library','2026-09-01T00:00:00Z','source-hash')",(SID,FILE))
    run=store.enqueue_stage_batch(SID,'remediate','remediate_file',[{'owner':OWNER,'scan_id':SID,'file':FILE}],snapshot_id=store.remediation_source_revision(SID),request_fingerprint='fixture')['batch_id']
    with store._db.cursor() as cur:
        store._db.execute(cur,"UPDATE jobs SET status='done' WHERE batch_id=%s",(run,))
    fixture=SimpleNamespace(store=store,run=run,calls=[],mode='queued')
    def publish(sid,request,body):
        fixture.calls.append(body)
        assert sid==SID and request.state.user_email==OWNER
        if fixture.mode=='unknown':raise RuntimeError('synthetic unknown dispatch outcome')
        if fixture.mode=='receipt':
            release=store.ensure_release_execution(SID,OWNER,'sharepoint',1,preferred_folder_name=body['release_folder_name'],parent_folder_id=body['destination']['folder_id'],parent_folder_name=body['destination']['folder_name'])
            store.record_release_document(release['id'],OWNER,dict(file=FILE,status='published',artifact_digest='sha256:'+DIGEST))
            return {'published':[dict(file=FILE,status='published',artifact_digest='sha256:'+DIGEST)]}
        batch='fixture-publish-batch'
        store.enqueue_job('publish_file',{'owner':OWNER,'scan_id':SID,'file':FILE,'artifact_digest':'sha256:'+DIGEST, 'automatic_release_id':body.get('automatic_release_id')},scan_id=SID,batch_id=batch)
        return {'batch_id':batch,'published':[{'file':FILE,'status':'queued'}]}
    monkeypatch.setattr(scans,'publish_files',publish)
    monkeypatch.setattr(flow, 'delivery_preflight', lambda row: {'ready': True})
    return fixture


def authorize(f):
    destination=flow.preview(f.store,SID,OWNER,[FILE])['destination']
    return flow.authorize(f.store,SID,OWNER,f.run,[FILE],destination,'request-one')


def tick(f,row):
    flow.advance(f.store,{'authorization_id':row['id'],'owner':OWNER,'revision':row['progress']['_tick_revision']},{})
    return persistence.get(f.store,row['id'],OWNER)


def count_jobs(f):
    with f.store._db.cursor() as cur:
        f.store._db.execute(cur,'SELECT COUNT(*) AS n FROM jobs')
        return f.store._db.fetchone(cur)['n']


def count_release_continuations(f):
    with f.store._db.cursor() as cur:
        f.store._db.execute(cur, "SELECT COUNT(*) AS n FROM jobs WHERE type='release_continue'")
        return f.store._db.fetchone(cur)['n']


def test_preview_is_read_only_and_owner_scoped(prepared):
    before=count_jobs(prepared)
    preview=flow.preview(prepared.store,SID,OWNER,[FILE])
    assert preview['available'] is True,preview
    assert preview['authorization'] is None
    assert count_jobs(prepared)==before
    assert flow.preview(prepared.store,SID,'other',[FILE])['available'] is False
    assert not prepared.calls


def test_exact_post_replay_stop_and_conflicting_permission(prepared):
    row=authorize(prepared);count=count_jobs(prepared)
    assert authorize(prepared)==row
    assert count_jobs(prepared)==count
    for files,destination in [(['different.pptx'],row['intent']['destination']),([FILE],{**row['intent']['destination'],'folder_id':'library/other'})]:
        with pytest.raises(ValueError):flow.authorize(prepared.store,SID,OWNER,prepared.run,files,destination,'request-one')
    stopped=persistence.stop(prepared.store,row['id'],OWNER)
    assert authorize(prepared)==stopped
    assert count_jobs(prepared)==count


def test_durable_tick_survives_browser_close_and_queues_exact_artifact(prepared):
    row=authorize(prepared)
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur,"SELECT payload FROM jobs WHERE type='release_continue'")
        payload=json.loads(prepared.store._db.fetchone(cur)['payload'])
    flow.advance(prepared.store,payload,{})
    assert prepared.calls[0]['expected_artifacts']=={FILE:DIGEST}
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur,"SELECT payload FROM jobs WHERE type='publish_file'")
        queued=json.loads(prepared.store._db.fetchone(cur)['payload'])
    assert queued['automatic_release_id']==row['id']
    # Replaying the same scheduled job cannot queue a second publication.
    flow.advance(prepared.store,payload,{})
    assert len(prepared.calls)==1


def test_pending_review_does_not_release_or_approve(prepared):
    row=authorize(prepared)
    item=prepared.store.enqueue_proposals(SID,FILE,'2.4.6',[dict(locator='slide 1',proposed_value='A useful title',source='fixture')])
    result=tick(prepared,row)
    assert not prepared.calls
    assert prepared.store.get_hitl_item(item)['status']=='pending'
    assert flow.public(result)['progress']['blocked']==1


@pytest.mark.parametrize('change',['source','run','destination','grants'])
def test_changed_authority_blocks_publication(prepared,monkeypatch,change):
    row=authorize(prepared)
    if change=='grants':
        import workspace_roles
        monkeypatch.setattr(workspace_roles,'access_for_email',lambda *a,**k:{'capabilities':[]})
    elif change=='destination':
        prepared.store.ensure_release_execution(SID,OWNER,'sharepoint',1,preferred_folder_name='Changed',parent_folder_id='library/other')
    else:
        with prepared.store._db.cursor() as cur:
            if change=='source':prepared.store._db.execute(cur,"UPDATE file_records SET source_modified='changed' WHERE scan_id=%s",(SID,))
            else:prepared.store._db.execute(cur,'UPDATE stage_executions SET is_current=0 WHERE execution_id=%s',(prepared.run,))
    result=tick(prepared,row)
    assert not prepared.calls
    assert flow.public(result)['progress']['blocked']==1


def test_stopped_queued_publish_never_calls_provider_callback(prepared):
    from worker import FatalJobError
    row=authorize(prepared)
    flow.publish_admission(prepared.store,row['id'],OWNER,SID,FILE,DIGEST)
    persistence.stop(prepared.store,row['id'],OWNER)
    with pytest.raises(FatalJobError):
        flow.publish_job(prepared.store,dict(automatic_release_id=row['id'],owner=OWNER,scan_id=SID,file=FILE,artifact_digest='sha256:'+DIGEST),{},lambda *a:pytest.fail('stop must block callback'))


def test_admitted_before_stop_may_finish_without_reactivation(prepared):
    row=authorize(prepared)
    flow.publish_admission(prepared.store,row['id'],OWNER,SID,FILE,DIGEST)
    def admitted(payload,job):
        stopped=persistence.stop(prepared.store,row['id'],OWNER)
        assert stopped['status']=='stopped'
        return 'in-flight callback completed'
    assert flow.publish_job(prepared.store,dict(automatic_release_id=row['id'],owner=OWNER,scan_id=SID,file=FILE,artifact_digest='sha256:'+DIGEST),{},admitted)=='in-flight callback completed'
    assert persistence.get(prepared.store,row['id'],OWNER)['status']=='stopped'


def test_receipt_recovers_delivery_and_unknown_dispatch_is_not_repeated(prepared):
    row=authorize(prepared);prepared.mode='unknown'
    failed=tick(prepared,row)
    tick(prepared,failed)
    assert len(prepared.calls)==1
    release=prepared.store.ensure_release_execution(SID,OWNER,'sharepoint',1,preferred_folder_name=row['intent']['release_folder_name'],parent_folder_id=row['intent']['destination']['folder_id'])
    prepared.store.record_release_document(release['id'],OWNER,dict(file=FILE,status='published',artifact_digest='sha256:'+DIGEST))
    public=flow.public(persistence.get(prepared.store,row['id'],OWNER),prepared.store)
    assert public['progress']['published']==1
    assert public['file_progress'][FILE]['receipt']['artifact_digest']=='sha256:'+DIGEST


def test_successful_exact_receipt_completes_run(prepared):
    row=authorize(prepared);prepared.mode='receipt'
    result=tick(prepared,row)
    assert result['status']=='completed'
    assert flow.public(result,prepared.store)['progress']['published']==1
    assert len(prepared.calls)==1


def test_unknown_dispatch_keeps_reconciling_and_late_receipt_completes_reports(prepared, monkeypatch):
    import release_report_delivery
    destination=flow.preview(prepared.store,SID,OWNER,[FILE])['destination']
    row=flow.authorize(prepared.store,SID,OWNER,prepared.run,[FILE],destination,'late-receipt',include_reports=True)
    prepared.mode='unknown'
    waiting=tick(prepared,row)
    assert waiting['status'] in flow.ACTIVE
    assert waiting['progress']['files'][FILE]['state']=='blocked'
    assert len(prepared.calls)==1
    release=prepared.store.ensure_release_execution(SID,OWNER,'sharepoint',1,preferred_folder_name=row['intent']['release_folder_name'],parent_folder_id=destination['folder_id'])
    prepared.store.record_release_document(release['id'],OWNER,dict(file=FILE,status='published',artifact_digest='sha256:'+DIGEST))
    reports=[]
    monkeypatch.setattr(release_report_delivery,'queue_release_reports',lambda *args:reports.append(args[3]))
    completed=tick(prepared,waiting)
    assert completed['status']=='completed'
    assert reports==[release['id']]
    assert len(prepared.calls)==1


def test_late_exact_receipts_correct_failed_public_status_without_reactivating(prepared):
    row=authorize(prepared)
    row=flow.publish_admission(prepared.store,row['id'],OWNER,SID,FILE,DIGEST)
    row=persistence.save(prepared.store,row,status='failed',progress={**row['progress'],'files':{FILE:{'state':'failed','artifact_digest':DIGEST}}})
    release=prepared.store.ensure_release_execution(SID,OWNER,'sharepoint',1,preferred_folder_name=row['intent']['release_folder_name'],parent_folder_id=row['intent']['destination']['folder_id'])
    before=count_jobs(prepared)
    assert flow.public(row,prepared.store)['status']=='failed'
    prepared.store.record_release_document(release['id'],OWNER,dict(file=FILE,status='published',artifact_digest='sha256:'+'b'*64))
    assert flow.public(row,prepared.store)['status']=='failed'
    prepared.store.record_release_document(release['id'],OWNER,dict(file=FILE,status='published',artifact_digest='sha256:'+DIGEST))
    status=flow.public(row,prepared.store)
    assert status['status']=='completed'
    assert status['progress']['published']==1
    assert persistence.get(prepared.store,row['id'],OWNER)==row
    assert count_jobs(prepared)==before


def test_concurrent_duplicate_ticks_admit_one_delivery(prepared,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    row=authorize(prepared)
    barrier=threading.Barrier(2)
    original=flow.publish_admission
    def simultaneous(*args,**kwargs):
        barrier.wait(timeout=5)
        return original(*args,**kwargs)
    monkeypatch.setattr(flow,'publish_admission',simultaneous)
    def advance(_):
        try:
            return tick(prepared,row)
        except ValueError:
            return None  # A losing CAS worker can defer to the durable winner.
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(advance,range(2)))
    assert len(prepared.calls)==1
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur,"SELECT COUNT(*) AS n FROM jobs WHERE type='release_continue'")
        assert prepared.store._db.fetchone(cur)['n']==2  # original + one successor


@pytest.mark.parametrize('blocker',['missing_digest','unverified','unfinished_job','assessment_changed'])
def test_incomplete_or_changed_readiness_never_dispatches(prepared,blocker):
    row=authorize(prepared)
    with prepared.store._db.cursor() as cur:
        if blocker=='missing_digest':prepared.store._db.execute(cur,'UPDATE file_records SET corrected_sha256=NULL WHERE scan_id=%s',(SID,))
        elif blocker=='unverified':prepared.store._db.execute(cur,'UPDATE file_records SET compliant=0 WHERE scan_id=%s',(SID,))
        elif blocker=='unfinished_job':prepared.store._db.execute(cur,"UPDATE jobs SET status='queued' WHERE batch_id=%s",(prepared.run,))
        else:prepared.store._db.execute(cur,"UPDATE scan_runs SET rubric_hash='new-assessment' WHERE id=%s",(SID,))
    result=tick(prepared,row)
    assert not prepared.calls
    assert flow.public(result)['progress']['blocked']==1


def test_frozen_artifact_does_not_dispatch_new_version_or_accept_wrong_receipt(prepared):
    row=authorize(prepared)
    queued=tick(prepared,row)
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur,'UPDATE file_records SET corrected_sha256=%s WHERE scan_id=%s',('b'*64,SID))
    release=prepared.store.ensure_release_execution(SID,OWNER,'sharepoint',1,preferred_folder_name=row['intent']['release_folder_name'],parent_folder_id=row['intent']['destination']['folder_id'])
    prepared.store.record_release_document(release['id'],OWNER,dict(file=FILE,status='published',artifact_digest='sha256:'+'b'*64))
    result=tick(prepared,queued)
    assert len(prepared.calls)==1
    assert flow.public(result,prepared.store)['progress']['published']==0
    assert result['progress']['files'][FILE]['artifact_digest']==DIGEST


def test_late_exact_receipt_visible_after_stop_without_new_work(prepared):
    row=authorize(prepared)
    flow.publish_admission(prepared.store,row['id'],OWNER,SID,FILE,DIGEST)
    stopped=persistence.stop(prepared.store,row['id'],OWNER)
    before=count_jobs(prepared)
    release=prepared.store.ensure_release_execution(SID,OWNER,'sharepoint',1,preferred_folder_name=row['intent']['release_folder_name'],parent_folder_id=row['intent']['destination']['folder_id'])
    prepared.store.record_release_document(release['id'],OWNER,dict(file=FILE,status='published',artifact_digest='sha256:'+DIGEST))
    status=flow.public(stopped,prepared.store)
    assert status['status']=='stopped' and status['progress']['published']==1
    assert persistence.get(prepared.store,row['id'],OWNER)==stopped
    assert count_jobs(prepared)==before


def test_expired_request_replay_does_not_extend_authorization(prepared,monkeypatch):
    from datetime import datetime,timedelta
    row=authorize(prepared)
    class Later(datetime):
        @classmethod
        def now(cls,tz=None):
            return datetime.now(tz)+timedelta(days=2)
    monkeypatch.setattr(flow,'datetime',Later)
    assert authorize(prepared)==row
    final=tick(prepared,row)
    assert final['status']=='failed'
    assert not prepared.calls
    assert authorize(prepared)==final


@pytest.mark.parametrize('files',[[],[FILE,FILE],['x']*501])
def test_invalid_authorization_scope_does_not_schedule(prepared,files):
    before=count_jobs(prepared)
    with pytest.raises(ValueError):
        flow.authorize(prepared.store,SID,OWNER,prepared.run,files,dict(provider='sharepoint',folder_id='library/root',folder_name='Source library root'),'new-request')
    assert count_jobs(prepared)==before


def test_automatic_release_folder_includes_owner_and_matches_created_execution(prepared):
    row = authorize(prepared)
    name = row['intent']['release_folder_name']
    assert name.endswith(' - ' + OWNER)
    prepared.mode = 'receipt'
    tick(prepared, row)
    assert prepared.calls[0]['release_folder_name'] == name
    assert prepared.store.release_for_scan(SID, OWNER)['folder_name'] == name


def partial_authorize(f):
    destination = flow.preview(f.store, SID, OWNER, [FILE])['destination']
    return flow.authorize(f.store, SID, OWNER, f.run, [FILE], destination, 'partial-plan',
                          allow_remaining_issues=True, include_reports=True)


def test_automatic_partial_plan_publishes_saved_copy_preserves_pending_review(prepared):
    item = prepared.store.enqueue_proposals(SID, FILE, '2.4.6', [dict(locator='slide 1', proposed_value='Title', source='fixture')])
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur, 'UPDATE file_records SET compliant=0 WHERE scan_id=%s', (SID,))
    row = partial_authorize(prepared)
    prepared.mode = 'receipt'
    result = tick(prepared, row)
    assert result['status'] == 'completed'
    assert prepared.calls[0]['allow_remaining_issues'] is True
    assert prepared.store.get_hitl_item(item)['status'] == 'pending'
    assert not prepared.store.get_file_record(SID, FILE)['compliant']
    assert flow.public(result)['include_reports'] is True
    assert flow.public(result)['allow_remaining_issues'] is True


@pytest.mark.parametrize('change', ['allow_remaining_issues', 'include_reports'])
def test_replaying_authorization_cannot_change_report_or_partial_permission(prepared, change):
    row = partial_authorize(prepared)
    flags = dict(allow_remaining_issues=True, include_reports=True)
    flags[change] = False
    with pytest.raises(ValueError, match='different release permission'):
        flow.authorize(prepared.store, SID, OWNER, prepared.run, [FILE], row['intent']['destination'], 'partial-plan', **flags)


def test_partial_plan_waits_for_active_apply_job(prepared):
    row = partial_authorize(prepared)
    prepared.store.enqueue_job('apply_approved_values', dict(owner=OWNER, scan_id=SID, file=FILE), scan_id=SID)
    result = tick(prepared, row)
    assert not prepared.calls
    assert 'active corrections' in result['progress']['files'][FILE]['message']


def test_partial_plan_still_requires_saved_artifact(prepared):
    row = partial_authorize(prepared)
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur, 'UPDATE file_records SET corrected_sha256=NULL WHERE scan_id=%s', (SID,))
    tick(prepared, row)
    assert not prepared.calls


def test_missing_copy_diagnosis_does_not_admit_upload_or_change_failure_identity(prepared, monkeypatch):
    import missing_corrected_copy
    row = partial_authorize(prepared)
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur, 'UPDATE file_records SET corrected_sha256=NULL,remediated_at=NULL WHERE scan_id=%s', (SID,))
    captured = []
    def diagnose(store, sid, owner, file, record):
        captured.append((sid, owner, file))
        assert record['corrected_sha256'] is None
        return 'Unsupported structure; repair and explicitly approve a new plan.'
    monkeypatch.setattr(missing_corrected_copy, 'explanation', diagnose)
    result = tick(prepared, row)
    entry = result['progress']['files'][FILE]
    assert captured == [(SID, OWNER, FILE)]
    assert entry['state'] == 'failed' and entry['failure_category'] == 'no_corrected_copy'
    assert entry['message'] == 'Unsupported structure; repair and explicitly approve a new plan.'
    assert not prepared.calls and not entry.get('artifact_digest')


def test_completed_file_without_copy_does_not_hold_other_files_or_reports(prepared, monkeypatch):
    import release_report_delivery
    other = 'no-copy.pptx'
    store = prepared.store
    with store._db.cursor() as cur:
        store._db.execute(cur, "INSERT INTO file_records(scan_id,file,engine,status,score,compliant,drive_file_id,source_modified,checksum) VALUES(%s,%s,'office','analysed',60,0,'other-item','2026-09-01T00:00:00Z','other-hash')", (SID, other))
        store._db.execute(cur, "INSERT INTO scan_inventory(scan_id,file,drive_file_id,drive_id,source_modified,checksum) VALUES(%s,%s,'other-item','library','2026-09-01T00:00:00Z','other-hash')", (SID, other))
        store._db.execute(cur, 'UPDATE stage_executions SET is_current=0 WHERE execution_id=%s', (prepared.run,))
    prepared.run = store.enqueue_stage_batch(SID, 'remediate', 'remediate_file',
        [dict(owner=OWNER, scan_id=SID, file=f) for f in [FILE, other]],
        snapshot_id=store.remediation_source_revision(SID), request_fingerprint='two-files')['batch_id']
    with store._db.cursor() as cur:
        store._db.execute(cur, "UPDATE jobs SET status='done' WHERE batch_id=%s", (prepared.run,))
    destination = flow.preview(store, SID, OWNER, [FILE, other])['destination']
    row = flow.authorize(store, SID, OWNER, prepared.run, [FILE, other], destination, 'both-files', allow_remaining_issues=True, include_reports=True)
    captured = []
    def reports(store, sid, owner, release_id):
        captured.append(store.release_status(release_id, owner))
    monkeypatch.setattr(release_report_delivery, 'queue_release_reports', reports)
    prepared.mode = 'receipt'
    result = tick(prepared, row)
    assert result['status'] == 'failed'
    assert result['progress']['files'][FILE]['state'] == 'published'
    assert result['progress']['files'][other]['failure_category'] == 'no_corrected_copy'
    assert len(captured) == 1
    assert captured[0]['published'] == 1 and captured[0]['failed'] == 1
    assert captured[0]['documents_total'] == 2
    failure = next(d for d in captured[0]['documents'] if d['file'] == other)
    assert failure['released_document_id'] is None
    assert 'original is unchanged' in failure['explanation']


@pytest.mark.parametrize('state', ['dead', 'cancelled'])
def test_terminal_file_job_can_be_reported_without_weakening_run_authority(prepared, state):
    row = partial_authorize(prepared)
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur, 'UPDATE jobs SET status=%s WHERE batch_id=%s', (state, prepared.run))
    result = tick(prepared, row)
    assert result['progress']['files'][FILE]['failure_category'] == 'no_corrected_copy'
    assert not prepared.calls


def test_stopped_run_without_copy_is_not_reinterpreted_as_publish_authority(prepared):
    row = partial_authorize(prepared)
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur, "UPDATE stage_executions SET state='cancelled' WHERE execution_id=%s", (prepared.run,))
        prepared.store._db.execute(cur, 'UPDATE file_records SET corrected_sha256=NULL WHERE scan_id=%s', (SID,))
    result = tick(prepared, row)
    assert result['progress']['files'][FILE]['state'] == 'blocked'
    assert not prepared.calls


def test_automatic_folder_uses_owner_timezone_and_freezes_on_replay(prepared, monkeypatch):
    import publish
    from datetime import datetime, timezone
    real_name = publish.release_folder_name
    monkeypatch.setattr(publish, 'release_folder_name', lambda at=None, timezone_name='UTC', **kw:
        real_name(datetime(2026, 9, 11, 1, 35, tzinfo=timezone.utc), timezone_name, **kw))
    prepared.store.set_user_setting(OWNER, 'release_timezone', 'America/Chicago')
    row = authorize(prepared)
    assert row['intent']['release_folder_name'] == '2026-09-10 20-35 CDT - ' + OWNER
    prepared.store.set_user_setting(OWNER, 'release_timezone', 'Asia/Kolkata')
    assert authorize(prepared)['intent']['release_folder_name'] == row['intent']['release_folder_name']


def test_delivery_stall_ignores_ticks_backs_off_and_recovers_exact_receipt(prepared, monkeypatch):
    from datetime import datetime, timezone, timedelta
    clock = [datetime.now(timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr(flow, 'datetime', Clock)
    row = authorize(prepared)
    row = tick(prepared, row)
    # Reproduce the uncertain queued receipt with no publish job to recover it.
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur, "DELETE FROM jobs WHERE type='publish_file'")
    row = tick(prepared, row)
    first_progress = row['progress']['_delivery_watch']['last_progress_at']
    assert not flow.public(row)['needs_attention']
    clock[0] += timedelta(seconds=601)
    row = tick(prepared, row)
    shown = flow.public(row)
    assert shown['needs_attention'] and row['status'] == 'blocked'
    assert shown['progress'] == dict(published=0, pending=0, blocked=1, failed=0)
    assert shown['last_progress_at'] == first_progress
    assert 'copy may already exist' in shown['attention_reason']
    assert len(prepared.calls) == 1
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur, "SELECT run_after FROM jobs WHERE type='release_continue' ORDER BY run_after DESC LIMIT 1")
        scheduled = prepared.store._db.fetchone(cur)['run_after']
    # Persistence uses its real clock; stalled polling has a five minute delay.
    assert (datetime.fromisoformat(scheduled) - datetime.now(timezone.utc)).total_seconds() > 250
    prepared.mode = 'receipt'
    release = prepared.store.ensure_release_execution(SID, OWNER, 'sharepoint', 1,
        preferred_folder_name=row['intent']['release_folder_name'],
        parent_folder_id=row['intent']['release_parent_id'])
    prepared.store.record_release_document(release['id'], OWNER,
        dict(file=FILE, status='published', artifact_digest='sha256:' + DIGEST))
    row = tick(prepared, row)
    assert row['status'] == 'completed'
    assert not flow.public(row)['needs_attention']
    assert len(prepared.calls) == 1


def test_waiting_for_human_is_not_a_delivery_stall(monkeypatch):
    from datetime import datetime, timezone, timedelta
    progress = {'files': {FILE: {'state': 'blocked', 'message': 'Waiting for approval'}}}
    progress['_delivery_watch'] = flow.delivery_watch(progress, {})
    progress['_delivery_watch']['last_progress_at'] = (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
    assert not flow.delivery_watch(progress, {})['needs_attention']


def test_ready_copy_waits_for_current_delivery_with_correct_reason(prepared):
    row = authorize(prepared)
    prepared.store.enqueue_stage_batch(SID, 'release', 'publish_file',
        [{'owner': OWNER, 'scan_id': SID, 'file': 'other.docx'}],
        snapshot_id='release-fixture', request_fingerprint='busy-release')
    row = tick(prepared, row)
    entry = flow.public(row)['file_progress'][FILE]
    assert entry['waiting_for_delivery']
    assert entry['message'] == 'Corrected copy is ready. Waiting for the current delivery to finish.'
    assert not prepared.calls


def test_delivery_job_transition_resets_stall_but_heartbeat_does_not():
    from datetime import datetime, timezone, timedelta
    progress = {'files': {FILE: {'state': 'publishing', 'artifact_digest': DIGEST}}}
    progress['_delivery_watch'] = flow.delivery_watch(progress, {FILE: ['queued']})
    progress['_delivery_watch']['last_progress_at'] = (datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat()
    progress['_tick_revision'] = 99
    assert flow.delivery_watch(progress, {FILE: ['queued']})['needs_attention']
    changed = flow.delivery_watch(progress, {FILE: ['running']})
    assert not changed['needs_attention']
    assert changed['last_progress_at'] != progress['_delivery_watch']['last_progress_at']


def test_optional_automatic_inspection_does_not_block_strict_saved_copy(prepared):
    item = prepared.store.queue_hitl_deferral(SID, FILE, 'Automatic fix applied — verify the result', 1, rule_id='auto/verify')
    row = authorize(prepared)
    assert flow.ready(prepared.store, row, FILE)['corrected_sha256'] == DIGEST
    assert prepared.store.get_hitl_item(item)['status'] == 'pending'
    assert not prepared.store.get_hitl_item(item).get('validated')
    prepared.store.queue_hitl_deferral(SID, FILE, 'Missing faithful alt text', 1, rule_id='1.1.1')
    with pytest.raises(ValueError, match='review or manual work remains'):
        flow.ready(prepared.store, row, FILE)


def test_missing_sharepoint_delivery_job_has_specific_receipt_inspection_action(prepared):
    row = authorize(prepared)
    row = tick(prepared, row)
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur, "DELETE FROM jobs WHERE type='publish_file'")
    row = tick(prepared, row)
    entry = flow.public(row)['file_progress'][FILE]
    assert entry['state'] == 'blocked'
    assert entry['failure_category'] == 'delivery_record_missing'
    assert 'No delivery job or receipt is recorded' in entry['message']
    assert 'Inspect the destination before retrying' in entry['message']
    assert entry['artifact_digest'] == DIGEST
    assert not entry['requires_reconnect']
    assert not flow.public(row)['can_resume']
    assert len(prepared.calls) == 1


def test_provider_preflight_failure_does_not_freeze_an_undispatched_copy(prepared, monkeypatch):
    monkeypatch.setattr(flow, 'delivery_preflight', lambda row: {'ready': False, 'message': 'Reconnect Microsoft to check this folder.'})
    row = tick(prepared, authorize(prepared))
    entry = row['progress']['files'][FILE]
    assert entry['state'] == 'blocked'
    assert entry['failure_category'] == 'delivery_preflight_blocked'
    assert not entry.get('artifact_digest')
    assert not prepared.calls
    monkeypatch.setattr(flow, 'delivery_preflight', lambda row: {'ready': True})
    row = tick(prepared, row)
    assert row['progress']['files'][FILE]['artifact_digest'] == DIGEST
    assert len(prepared.calls) == 1


def test_unknown_dispatch_keeps_bounded_diagnostic_after_missing_job_reconciliation(prepared, monkeypatch):
    from fastapi import HTTPException
    from routes import scans
    def unavailable(*args, **kwargs):
        raise HTTPException(403, 'synthetic write grant unavailable')
    monkeypatch.setattr(scans, 'publish_files', unavailable)
    row = tick(prepared, authorize(prepared))
    assert row['progress']['files'][FILE]['dispatch_error'] == {'error_type': 'HTTPException', 'http_status': 403}
    row = tick(prepared, row)
    assert row['progress']['files'][FILE]['failure_category'] == 'delivery_record_missing'
    assert row['progress']['files'][FILE]['dispatch_error']['http_status'] == 403
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur, "SELECT detail FROM decision_log WHERE scan_id=%s AND action='release.dispatch_outcome_unknown'", (SID,))
        audit = prepared.store._db.fetchone(cur)
    assert json.loads(audit['detail'])['http_status'] == 403


@pytest.mark.parametrize('status', [object(), '403', True, 99, 600])
def test_unknown_dispatch_diagnostic_rejects_non_http_status(prepared, monkeypatch, status):
    from routes import scans
    error = type('SyntheticError' * 20, (Exception,), {})('private provider payload')
    error.status_code = status
    def unavailable(*args, **kwargs):
        raise error
    monkeypatch.setattr(scans, 'publish_files', unavailable)
    row = tick(prepared, authorize(prepared))
    diagnostic = row['progress']['files'][FILE]['dispatch_error']
    assert diagnostic['http_status'] is None
    assert len(diagnostic['error_type']) == 80
    assert 'private provider payload' not in json.dumps(diagnostic)


def test_missing_microsoft_preflight_exposes_reconnect_and_resumes_same_permission(prepared, monkeypatch):
    monkeypatch.setattr(flow, 'delivery_preflight', lambda row: {
        'ready': False, 'credential_valid': False,
        'message': 'Reconnect Microsoft to check this folder.'})
    row = tick(prepared, authorize(prepared))
    identity = row['id']
    view = flow.public(row, prepared.store)
    assert view['requires_reconnect'] is True
    assert row['status'] == 'blocked'
    assert not row['progress']['files'][FILE].get('artifact_digest')
    assert not prepared.calls
    assert count_release_continuations(prepared) == 1
    assert view['needs_attention'] is True
    assert 'reconnect' in view['attention_reason'].lower()
    assert prepared.store.active_workflows(OWNER)[0]['stage'] == 'publish'
    # Replaying the already-claimed tick remains harmless and cannot create a polling chain.
    row = tick(prepared, row)
    assert count_release_continuations(prepared) == 1
    assert not prepared.calls
    monkeypatch.setattr(flow, 'delivery_preflight', lambda row: {'ready': True, 'credential_valid': True})
    row = flow.resume(prepared.store, identity, OWNER, SID)
    assert count_release_continuations(prepared) == 2
    row = tick(prepared, row)
    assert row['id'] == identity
    assert row['progress']['files'][FILE]['artifact_digest'] == DIGEST
    assert not flow.public(row, prepared.store)['requires_reconnect']
    assert len(prepared.calls) == 1


def test_expired_reconnect_pause_is_not_resumable_or_an_active_slot(prepared, monkeypatch):
    monkeypatch.setattr(flow, 'delivery_preflight', lambda row: {
        'ready': False, 'credential_valid': False,
        'message': 'Reconnect Microsoft to check this folder.'})
    row = tick(prepared, authorize(prepared))
    intent = {**row['intent'], 'expires_at': '2020-01-01T00:00:00+00:00'}
    with prepared.store._db.cursor() as cur:
        prepared.store._db.execute(cur,
            'UPDATE automatic_release_authorizations SET intent=%s WHERE id=%s',
            (json.dumps(intent), row['id']))
    expired = persistence.get(prepared.store, row['id'], OWNER)
    view = flow.public(expired, prepared.store)
    assert view['status'] == 'failed'
    assert view['requires_reconnect'] is False
    assert view['needs_attention'] is False
    with pytest.raises(ValueError, match='expired'):
        flow.resume(prepared.store, row['id'], OWNER, SID)
    assert count_release_continuations(prepared) == 1

    replacement = flow.authorize(prepared.store, SID, OWNER, prepared.run, [FILE],
        expired['intent']['destination'], 'replacement-request')
    assert replacement['id'] != row['id']
    assert persistence.get(prepared.store, row['id'], OWNER)['status'] == 'failed'
    assert count_release_continuations(prepared) == 2


def test_reconnected_but_unwritable_folder_is_not_reported_as_missing_access(prepared, monkeypatch):
    monkeypatch.setattr(flow, 'delivery_preflight', lambda row: {
        'ready': False, 'credential_valid': False, 'message': 'Reconnect Microsoft.'})
    row = tick(prepared, authorize(prepared))
    monkeypatch.setattr(flow, 'delivery_preflight', lambda row: {
        'ready': False, 'credential_valid': True, 'write_permission': False,
        'message': 'Microsoft sign-in is missing a files or sites write grant.'})
    row = tick(prepared, row)
    assert row['status'] == 'blocked'
    assert not flow.public(row, prepared.store)['requires_reconnect']
    assert row['progress']['files'][FILE]['message'].endswith('write grant.')
    assert not prepared.calls
