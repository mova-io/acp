"""A single generic worker must advance discovery/rules/Assess without parent deadlock."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('legacy_singleton', [False, True])
@pytest.mark.parametrize('late_terminal', [False, True])
@pytest.mark.parametrize('provider', ['drive', 'sharepoint'])
def test_one_worker_stages_real_lifecycle_before_content(isolated_store, monkeypatch, provider, late_terminal, legacy_singleton):
    import core
    import handlers
    import scanner
    import worker
    import scheduled_scan_execution
    st = isolated_store
    owner = 'owner@example.test'
    now = datetime.now(timezone.utc)
    if provider == 'sharepoint':
        import sp_sync
        monkeypatch.setattr(sp_sync, 'sp_sync_configured', lambda: True)
        monkeypatch.setattr(sp_sync, 'sync_drive_id', lambda: 'Drive-A')
        monkeypatch.setattr(sp_sync, 'app_token', lambda: 'offline-app-fixture')
    monkeypatch.setattr(core, 'store', st)
    monkeypatch.setattr(core, 'get_store', lambda: st)
    monkeypatch.setattr(st, 'get_ai_enabled', lambda: False)
    monkeypatch.setattr(core, 'active_rubric', lambda: SimpleNamespace(hash='fixture'))
    monkeypatch.setattr(core, 'get_scan_tokens', lambda sid: {'drive': 'offline-gis-fixture'})
    monkeypatch.setattr(core, 'finalize_scan', lambda *args: None)
    monkeypatch.setattr(handlers, '_make_svc', lambda *args: None)
    monkeypatch.setenv('ACP_DEFER_ANALYSIS_TO_ASSESS', '0')
    monkeypatch.setenv('ACP_SCAN_FILE_TIMEOUT_S', '0')
    monkeypatch.setenv('ACP_SCAN_BATCH_WORKERS', '1')
    items = [{'name': name, 'id': name, 'source_mime': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
              'mime': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
              'drive_account_id': 'Account-A' if provider == 'drive' else None,
              'driveId': 'Drive-A' if provider == 'sharepoint' else None, 'path': path, 'parent_folder': 'accepted-folder'}
             for name, path in [('archive.docx', '/Archive/archive.docx'),
                                ('delete.docx', '/Delete/delete.docx'), ('active.docx', '/Current/active.docx')]]
    monkeypatch.setattr(scanner, '_list', lambda *args, **kwargs: list(items))
    monkeypatch.setattr(scanner, '_drive_service', lambda *args: None)
    monkeypatch.setattr(scanner, 'drive_account_id', lambda svc: 'Account-A')
    def forbidden(*args, **kwargs):
        pytest.fail('scheduled monolithic/content path used before lifecycle selection')
    monkeypatch.setattr(scanner, 'run_scan', forbidden)
    reads = []
    def cache(scan_id, name, *args, **kwargs):
        reads.append(name)
        return b'<html lang="en"><title>Offline</title></html>'
    monkeypatch.setattr(scanner, 'read_cached_source', cache)
    monkeypatch.setattr(scanner, '_download', forbidden)
    monkeypatch.setattr(scanner, 'analyse_and_assess', lambda path, name, **kwargs:
                        ({'file': name, 'engine': 'html', 'status': 'certifiable', 'score': 100,
                          'compliant': True, 'skipped_rules': 0, 'issues': [], 'errors': []}, None))
    for action, prefix in [('archive', '/Archive/'), ('delete', '/Delete/')]:
        st.create_disposition_policy('policy-' + action, name=action,
            match=json.dumps([{'field': 'path', 'op': 'prefix', 'value': prefix}]), action=action,
            action_config='{}', requires_approval=False, enabled=True, owner_email=owner)
    if legacy_singleton:
        st.save_schedule(True, 60, owner=owner, source=provider)
        monkeypatch.setattr(core, '_drive_sync_plan', lambda *args: (False, None))
        monkeypatch.setattr(core, '_sp_sync_plan', lambda *args: (False, None))
        elected = {'occurrence_key': 'owner:day', 'scheduled_for': now.isoformat()}
    else:
        st.save_user_scan_schedule(owner, True, 'UTC', '09:00', list(range(7)), source=provider,
                                   source_scope={'include_ids': ['accepted-folder' if provider == 'drive' else 'Drive-A/accepted-folder']})
        elected = {'owner_email': owner, 'occurrence_key': 'owner:day', 'scheduled_for': now.isoformat()}
    st.enqueue_scheduled_sweep('owner:day', elected)
    monkeypatch.setitem(worker.HANDLERS, 'scheduled_sweep', handlers._scheduled_sweep)
    generic = worker.JobWorker(st, worker_id='only-worker')
    # Make each durable delayed successor eligible without sleeping or adding workers.
    instant = now
    terminal_receipt = None
    for turn in range(40):
        monkeypatch.setattr(st, '_now', lambda instant=instant: instant.isoformat())
        if not generic.run_once():
            instant += timedelta(seconds=31)
        if late_terminal and terminal_receipt is None:
            queued = [j for j in st.list_jobs() if j['type'] == 'scan_file' and j['status'] == 'queued']
            if queued:
                sid = queued[0]['scan_id']
                st.set_lifecycle_status(sid, 'active.docx', 'Deleted', reason='verified after enqueue')
                terminal_receipt = st.get_source_lifecycle_states(owner, provider, st.list_inventory(sid))['states']
        occurrences = st.list_schedule_occurrences(owner)
        if occurrences and occurrences[0].get('completed_at'):
            break
    else:
        pytest.fail('one shared worker did not finish: ' + repr([(j['type'],j['status'],j.get('run_after'),j.get('last_error')) for j in st.list_jobs()]) + repr(st.list_schedule_occurrences(owner)[0]['execution_context'])[:1600])
    occurrence = st.list_schedule_occurrences(owner)[0]
    scan_id = occurrence['scan_id']
    assert st.get_lifecycle_status(scan_id, 'archive.docx')['lifecycle_status'] == 'Archive Candidate'
    assert st.get_lifecycle_status(scan_id, 'delete.docx')['lifecycle_status'] == 'Delete Candidate'
    assert reads == ([] if late_terminal else ['active.docx'])
    if late_terminal:
        assert terminal_receipt is not None
        assert st.get_source_lifecycle_states(owner, provider, st.list_inventory(scan_id))['states'] == terminal_receipt
    assert st.get_scan(scan_id)['run']['status'] == 'done'
    assert not st.active_scheduled_sweep(owner)
    if legacy_singleton:
        assert st.get_last_sweep()['scan_id'] == scan_id
        assert st.get_last_sweep(owner) is None
