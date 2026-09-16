"""The monolithic/scheduled boundary must precede cache, download and analysis."""
from types import SimpleNamespace

import pytest

import scanner
from lifecycle_identity import source_identity


@pytest.fixture
def scan_boundary(monkeypatch):
    import activity
    import classify
    import core
    import ocr
    import office_structure
    import textchecks
    calls = {'cache': [], 'download': [], 'analysis': [], 'lookup': []}
    ledger = {}
    items = []
    class Store:
        def list_scope_rules(self, **kwargs): return []
        def get_source_lifecycle_states(self, owner, provider, rows):
            calls['lookup'].append((owner, provider, rows))
            return {'states': {key: value for key, value in ledger.items()
                               if any(source_identity(owner, provider, row) == key for row in rows)},
                    'unavailable': [i for i, row in enumerate(rows)
                                    if source_identity(owner, provider, row) is None]}
    st = Store()
    monkeypatch.setattr(core, 'store', st)
    monkeypatch.setattr(scanner, '_scope_for_listing', lambda user: None)
    monkeypatch.setattr(scanner, '_list', lambda *args, **kwargs: list(items))
    monkeypatch.setattr(scanner, '_drive_service', lambda token: None)
    def cached(scan_id, name, *args, **kwargs):
        calls['cache'].append(name)
        return None
    def download(item, temporary, *args, **kwargs):
        calls['download'].append(item['name'])
        (temporary / item['name']).write_text('<html lang="en"><title>Fixture</title></html>')
    def analyse(path):
        calls['analysis'].append(path.name)
        return {'succeeded': True, 'issues': [], 'errors': []}
    monkeypatch.setattr(scanner, 'read_cached_source', cached)
    monkeypatch.setattr(scanner, '_download', download)
    monkeypatch.setattr(scanner, 'cache_source_bytes', lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner, '_analyse_office', lambda *args, **kwargs: {})
    monkeypatch.setattr(scanner, '_analyse_html', analyse)
    monkeypatch.setattr(classify, 'classify', lambda *args, **kwargs: {})
    monkeypatch.setattr(ocr, 'images_of_text', lambda *args: [])
    monkeypatch.setattr(ocr, 'images_of_text_no_exception', lambda *args: [])
    monkeypatch.setattr(textchecks, 'content_findings', lambda *args: [])
    monkeypatch.setattr(office_structure, 'checks_for', lambda *args: [])
    monkeypatch.setattr(scanner._lf_mod, 'file_trace', lambda *args, **kwargs: None)
    monkeypatch.setattr(scanner._lf_mod, 'discover_span', lambda *args: SimpleNamespace(end=lambda **kwargs: None))
    monkeypatch.setattr(scanner._lf_mod, 'flush', lambda: None)
    for name in ('record', 'record_file', 'finish_file'):
        monkeypatch.setattr(activity, name, lambda *args, **kwargs: None)
    return core, st, items, ledger, calls


def item(name, identifier, provider='drive', namespace='Account-A'):
    return {'name': name, 'id': identifier, 'source_mime': 'text/html',
            ('drive_account_id' if provider == 'drive' else 'driveId'): namespace}


def key(owner, provider, raw):
    return source_identity(owner, provider, dict(raw, drive_file_id=raw.get('id'),
                                                drive_id=raw.get('driveId')))


@pytest.mark.parametrize('provider', ['drive', 'sharepoint', 'onedrive'])
@pytest.mark.parametrize('status', ['Archived', 'Already archived', 'Deleted'])
def test_terminal_sources_never_reach_content_and_full_inventory_is_retained(scan_boundary, provider, status):
    _, _, items, ledger, calls = scan_boundary
    items.extend([item('renamed-terminal.html', 'terminal-id', provider),
                  item('active.html', 'other-id', provider)])
    ledger[key('owner@example.test', provider, items[0])] = {'lifecycle_status': status, 'reason': 'verified source action'}
    report = scanner.run_scan(provider, user='owner@example.test', ai_enabled=False)
    assert calls['cache'] == calls['download'] == calls['analysis'] == ['active.html']
    assert len(calls['lookup']) == 1
    assert [row['name'] for row in report['_inventory_items']] == [row['name'] for row in items]
    assert report['summary']['files'] == 1
    assert [row['file'] for row in report['files']] == ['active.html']
    evidence = report['scope']['lifecycle_gate']
    assert evidence['listed'] == 2 and evidence['excluded'] == 1
    assert evidence['files'][0]['lifecycle_status'] == status
    assert status in evidence['files'][0]['exclusion_reason']
    assert evidence['files'][1]['identity_state'] == 'ledger_absent'


def test_identity_ambiguity_and_no_history_are_explicit_not_inferred_from_name(scan_boundary):
    _, _, items, ledger, calls = scan_boundary
    items.extend([item('same-name.html', 'same-id', namespace='Other-Account'),
                  item('missing-namespace.html', 'same-id', namespace=None)])
    ledger[('owner@example.test', 'drive', 'Account-A', 'same-id')] = {'lifecycle_status': 'Deleted'}
    report = scanner.run_scan('drive', user='owner@example.test', ai_enabled=False)
    assert calls['download'] == [row['name'] for row in items]
    evidence = report['scope']['lifecycle_gate']
    assert evidence['excluded'] == 0 and evidence['unknown'] == 2
    assert [r['identity_state'] for r in evidence['files']] == ['ledger_absent', 'missing_identity']
    assert all(r['lifecycle_status'] is None for r in evidence['files'])


def test_lookup_failure_stops_before_cache_or_download(scan_boundary, monkeypatch):
    _, st, items, _, calls = scan_boundary
    items.append(item('document.html', 'id'))
    def fail(*args): raise RuntimeError('lookup failed')
    monkeypatch.setattr(st, 'get_source_lifecycle_states', fail)
    with pytest.raises(RuntimeError, match='lookup failed'):
        scanner.run_scan('drive', user='owner@example.test', ai_enabled=False)
    assert calls['cache'] == calls['download'] == calls['analysis'] == []


@pytest.mark.parametrize('owner_occurrence', [False, True])
def test_durable_scheduled_sweep_reaches_shared_pre_content_gate(scan_boundary, monkeypatch, owner_occurrence):
    import handlers
    core, st, items, ledger, calls = scan_boundary
    items.append(item('archived.html', 'id'))
    ledger[key('owner@example.test', 'drive', items[0])] = {'lifecycle_status': 'Archived'}
    monkeypatch.setattr(core, 'get_store', lambda: st)
    monkeypatch.setattr(st, 'get_schedule', lambda: {'enabled': True, 'owner_email': 'owner@example.test',
                                                   'source': 'drive', 'source_scope': {'include_ids': ['folder']}}, raising=False)
    monkeypatch.setattr(st, 'get_ai_enabled', lambda: False, raising=False)
    saved, outcomes = [], []
    monkeypatch.setattr(st, 'save_scan', lambda report: saved.append(report) or 'scheduled-id', raising=False)
    monkeypatch.setattr(st, 'record_sweep_outcome', lambda **kwargs: outcomes.append(kwargs), raising=False)
    monkeypatch.setattr(core, 'finalize_scan', lambda *args: None)
    payload = {}
    if owner_occurrence:
        from datetime import date
        from scan_schedule import occurrence_key
        cfg = st.get_schedule()
        monkeypatch.setattr(st, 'get_user_scan_schedule', lambda owner: cfg, raising=False)
        monkeypatch.setattr(core, '_scheduled_scan_admission', lambda payload: {'admit': True})
        payload = {'owner_email': 'owner@example.test', 'local_date': '2026-09-15',
                   'occurrence_key': occurrence_key(cfg, date(2026, 9, 15))}
    handlers._scheduled_sweep(payload, {})
    assert calls['cache'] == calls['download'] == calls['analysis'] == []
    assert len(saved) == 1 and saved[0]['scope']['lifecycle_gate']['excluded'] == 1
    assert saved[0]['_inventory_items'][0]['id'] == 'id'
    assert outcomes[-1]['ok'] is True and outcomes[-1]['files'] == 0


@pytest.mark.parametrize('status', ['Active', 'Archive Candidate', 'Delete Candidate'])
def test_restored_retained_state_is_not_terminal(scan_boundary, status):
    _, _, items, ledger, calls = scan_boundary
    items.append(item('restored.html', 'id'))
    ledger[key('owner@example.test', 'drive', items[0])] = {'lifecycle_status': status}
    report = scanner.run_scan('drive', user='owner@example.test', ai_enabled=False)
    assert calls['download'] == ['restored.html']
    evidence = report['scope']['lifecycle_gate']
    assert evidence['excluded'] == 0 and evidence['unknown'] == 0
    assert evidence['files'][0]['lifecycle_status'] == status


@pytest.mark.parametrize('provider', ['drive', 'sharepoint', 'onedrive'])
def test_real_ledger_gates_before_content_and_saved_inventory_keeps_terminal_row(scan_boundary, isolated_store, monkeypatch, provider):
    core, _, items, _, calls = scan_boundary
    st = isolated_store
    monkeypatch.setattr(core, 'store', st)
    st.init_scan_run('prior', provider, 1, '2026-09-15', 'default', 'r', owner='owner@example.test')
    st.add_inventory('prior', [{'file': 'old-name.html', 'drive_file_id': 'terminal-id',
                               'drive_account_id': 'Account-A' if provider == 'drive' else None,
                               'drive_id': 'Account-A' if provider != 'drive' else None}])
    st.set_lifecycle_status('prior', 'old-name.html', 'Archived', reason='verified action')
    items.extend([item('new-name.html', 'terminal-id', provider), item('active.html', 'active-id', provider)])
    report = scanner.run_scan(provider, user='owner@example.test', ai_enabled=False)
    assert calls['cache'] == calls['download'] == calls['analysis'] == ['active.html']
    evidence = report['scope']['lifecycle_gate']
    sid = st.save_scan(report)
    assert st.get_lifecycle_status(sid, 'new-name.html')['lifecycle_status'] == 'Archived'
    assert evidence['listed'] == 2 and evidence['excluded'] == 1
    with st._db.cursor() as cur:
        st._db.execute(cur, 'SELECT file FROM scan_inventory WHERE scan_id=%s ORDER BY file', (sid,))
        assert [row['file'] for row in st._db.fetchall(cur)] == ['active.html', 'new-name.html']
        st._db.execute(cur, 'SELECT file FROM file_records WHERE scan_id=%s', (sid,))
        assert [row['file'] for row in st._db.fetchall(cur)] == ['active.html']
