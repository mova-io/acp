"""Archive/delete after dispatch must precede cache reuse and content access."""
from types import SimpleNamespace
import pytest
import core
import handlers
import scanner
from lifecycle_identity import source_identity

OWNER = 'owner@example.test'

@pytest.fixture
def queued(isolated_store, monkeypatch):
    st = isolated_store
    monkeypatch.setattr(core, 'store', st)
    def forbidden(*args, **kwargs):
        pytest.fail('content or cached findings accessed after terminal disposition')
    for name in ('find_by_checksum', 'find_prior_analysis'):
        monkeypatch.setattr(st, name, forbidden)
    for name in ('read_cached_source', '_download', 'analyse_and_assess'):
        monkeypatch.setattr(scanner, name, forbidden)
    return st

def setup(st, provider='drive'):
    item = {'file': 'file.html', 'drive_file_id': 'opaque-item', 'checksum': 'cached',
            'drive_account_id': 'Account-A' if provider == 'drive' else None,
            'drive_id': 'Drive-A' if provider != 'drive' else None}
    st.init_scan_run('scan', provider, 1, '2026-09-15', 'default', 'r', owner=OWNER)
    st.add_inventory('scan', [item])
    return item

@pytest.mark.parametrize('provider', ['drive', 'sharepoint', 'onedrive'])
@pytest.mark.parametrize('status', ['Already archived', 'Archived', 'Deleted'])
@pytest.mark.parametrize('batch', [False, True])
@pytest.mark.parametrize('other_scan', [False, True])
def test_terminal_after_enqueue_records_skip_without_pass_or_content(queued, monkeypatch, provider, status, batch, other_scan):
    st = queued
    item = setup(st, provider)
    dispatched = []
    monkeypatch.setattr(st, 'enqueue_job', lambda kind, payload, **kwargs: dispatched.append((kind, payload)))
    handlers._enqueue_analysis('scan', provider, [item], ai=False, pii=False, user=OWNER,
                               incremental=True, exclude_remediated=True, force_batch=batch)
    kind, payload = dispatched.pop()
    frozen_item = payload['items'][0] if batch else payload
    assert frozen_item['drive_account_id'] == item['drive_account_id']
    operation_scan, operation_file = 'scan', item['file']
    if other_scan:
        operation_scan, operation_file = 'older', 'old-name.html'
        st.init_scan_run(operation_scan, provider, 1, '2026-09-14', 'default', 'r', owner=OWNER)
        st.add_inventory(operation_scan, [{**item, 'file': operation_file}])
    st.set_lifecycle_status(operation_scan, operation_file, status, reason='verified action', evidence_id='receipt')
    before = st.get_source_lifecycle_states(OWNER, provider, [item])['states']
    monkeypatch.setattr(core, 'get_scan_tokens', lambda sid: {})
    monkeypatch.setattr(core, 'active_rubric', lambda: SimpleNamespace(hash='r'))
    monkeypatch.setattr(handlers, '_make_svc', lambda *args: None)
    monkeypatch.setattr(handlers, 'scan_paused', lambda sid: False)
    monkeypatch.setenv('ACP_SCAN_FILE_TIMEOUT_S', '0')
    monkeypatch.setenv('ACP_SCAN_BATCH_WORKERS', '1')
    (handlers._scan_batch if batch else handlers._scan_file)(payload, {'id': 'child', 'attempts': 1})
    with st._db.cursor() as cur:
        st._db.execute(cur, "SELECT status,compliant,engine FROM file_records WHERE scan_id='scan'")
        assert st._db.fetchone(cur) == {'status': 'skipped', 'compliant': 0, 'engine': 'lifecycle'}
        st._db.execute(cur, "SELECT COUNT(*) AS n FROM scan_rule_traces WHERE scan_id='scan' AND outcome!='NOT_EVALUATED'")
        assert st._db.fetchone(cur)['n'] == 0
        st._db.execute(cur, "SELECT COUNT(*) AS n FROM scan_file_manifests WHERE scan_id='scan' AND status='PASS'")
        assert st._db.fetchone(cur)['n'] == 0
    assert st.get_source_lifecycle_states(OWNER, provider, [item])['states'] == before
    assert dispatched[-1][0] == 'scan_finalize'
    assert st.finalize_scan_run('scan', 'now')['files'] == 0
    overview = st._build_overview_snapshot('scan', OWNER, 0, 'r')
    assert overview['documents']['assessed'] == 0
    assert overview['documents']['excluded'] == 1
    assert st.get_scan_manifest('scan')['rules_checked_total'] == 0

@pytest.mark.parametrize('field,value', [('drive_file_id','other'), ('drive_account_id','other')])
def test_rebound_identity_refused_before_cache(queued, field, value):
    item = setup(queued)
    item[field] = value
    with pytest.raises(RuntimeError, match='binding changed'):
        handlers._analyse_and_persist_one_impl('scan', item, 'drive', False, None, {}, 'now', None, user=OWNER)

@pytest.mark.parametrize('owner,provider', [('foreign@example.test','drive'), (OWNER,'onedrive')])
def test_payload_cannot_change_scan_owner_or_provider(queued, owner, provider):
    item = setup(queued)
    with pytest.raises(RuntimeError, match='ownership changed'):
        handlers._analyse_and_persist_one_impl('scan', item, provider, False, None, {}, 'now', None, user=owner)

def test_retained_restoration_precedes_old_terminal_projection(queued):
    item = setup(queued)
    queued.set_lifecycle_status('scan', item['file'], 'Archived')
    key = source_identity(OWNER, 'drive', item)
    queued.get_source_lifecycle_states = lambda *args: {'states': {key: {'lifecycle_status': 'Active'}}}
    assert handlers._queued_terminal_exclusion('scan', item, 'drive', OWNER) is None

def test_missing_namespace_does_not_guess_cross_scan_identity(queued):
    item = setup(queued)
    item.pop('drive_account_id')
    queued.get_source_lifecycle_states = lambda *args: pytest.fail('incomplete identity looked up')
    assert handlers._queued_terminal_exclusion('scan', item, 'drive', OWNER) is None


def test_stale_skip_cannot_replace_later_attempt_or_emit_skip_audit(queued):
    item = setup(queued)
    queued.save_file_result('scan', {'file': item['file'], 'engine': 'html', 'status': 'uncertain',
                                   'score': 42, 'compliant': False, 'skipped_rules': 0,
                                   'issues': []}, 'now', job={'id': 'child', 'attempts': 2})
    queued.set_lifecycle_status('scan', item['file'], 'Deleted', evidence_id='receipt')
    handlers._analyse_and_persist_one_impl('scan', item, 'drive', False, None, {}, 'now', None,
                                         user=OWNER, job={'id': 'child', 'attempts': 1})
    with queued._db.cursor() as cur:
        queued._db.execute(cur, "SELECT status,score FROM file_records WHERE scan_id='scan'")
        assert queued._db.fetchone(cur) == {'status': 'uncertain', 'score': 42}
    decisions = queued.list_decisions(scan_id='scan')
    assert not any(d['action'] == 'analysis.lifecycle_skipped' for d in decisions)


def test_missing_namespace_still_blocks_own_scan_terminal_row(queued):
    item = setup(queued)
    queued.set_lifecycle_status('scan', item['file'], 'Archived')
    item.pop('drive_account_id')
    queued.get_source_lifecycle_states = lambda *args: pytest.fail('incomplete identity looked up')
    assert handlers._queued_terminal_exclusion('scan', item, 'drive', OWNER)['identity_state'] == 'missing_identity'


def test_ledger_read_failure_stops_before_content(queued):
    item = setup(queued)
    def unavailable(*args):
        raise RuntimeError('ledger unavailable')
    queued.get_source_lifecycle_states = unavailable
    with pytest.raises(RuntimeError, match='ledger unavailable'):
        handlers._analyse_and_persist_one_impl('scan', item, 'drive', False, None, {}, 'now', None, user=OWNER)
    assert queued.count_files_done('scan')[0] == 0


def test_complete_job_identity_requires_current_inventory_binding(queued):
    item = setup(queued)
    with queued._db.cursor() as cur:
        queued._db.execute(cur, "DELETE FROM scan_inventory WHERE scan_id='scan'")
    with pytest.raises(RuntimeError, match='binding is absent'):
        handlers._analyse_and_persist_one_impl('scan', item, 'drive', False, None, {}, 'now', None, user=OWNER)


def test_terminal_skip_removes_previous_pii_findings(queued):
    item = setup(queued)
    with queued._db.cursor() as cur:
        queued._db.execute(cur,
            "INSERT INTO pii_findings(scan_id,file,pii_type,label,count,severity,samples) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            ('scan', item['file'], 'EMAIL', 'email', 1, 'HIGH', '["private@example.test"]'))
    queued.set_lifecycle_status('scan', item['file'], 'Deleted')
    handlers._analyse_and_persist_one_impl('scan', item, 'drive', False, None, {}, 'now', None, user=OWNER)
    with queued._db.cursor() as cur:
        queued._db.execute(cur, "SELECT COUNT(*) AS n FROM pii_findings WHERE scan_id='scan'")
        assert queued._db.fetchone(cur)['n'] == 0
