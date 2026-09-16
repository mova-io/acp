"""Opaque inventory ids retain their provider namespace at disposition/restore time."""
import pytest
from test_lifecycle_execution import OWNER, _seed, gated_client, drive


@pytest.mark.parametrize('source', ['sharepoint', 'onedrive', 'local', 'drive'])
def test_source_namespace_is_checked_before_google_drive_resolution(isolated_store, monkeypatch, source):
    import core
    import routes.disposition as rd
    monkeypatch.setattr(core, 'store', isolated_store)
    _seed(isolated_store)
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, 'UPDATE scan_runs SET source=%s', (source,))
    resolved = rd._lifecycle_drive_doc('scan:scan-exec-1:a.docx', OWNER)
    assert bool(resolved) is (source == 'drive')
    assert rd._lifecycle_drive_doc('scan:scan-exec-1:a.docx', 'other@example.com') is None


@pytest.mark.parametrize('source', ['sharepoint', 'onedrive'])
def test_graph_candidate_approval_never_calls_google_drive(gated_client, isolated_store, drive, source):
    st = _seed(isolated_store)
    with st._db.cursor() as cur:
        st._db.execute(cur, 'UPDATE scan_runs SET source=%s', (source,))
    st.create_disposition_audit('namespace-audit', doc_id='scan:scan-exec-1:a.docx',
        policy_id='namespace-rule', action='delete', result='pending_approval', detail='candidate', owner_email=OWNER)
    result = gated_client(OWNER).post('/disposition/approvals/namespace-audit/approve')
    assert result.status_code == 200, result.text
    assert result.json()['executed'] is False
    assert drive._files.touched == []


@pytest.mark.parametrize('source', ['sharepoint', 'onedrive'])
def test_graph_candidate_restore_never_calls_google_drive(gated_client, isolated_store, drive, source):
    st = _seed(isolated_store)
    with st._db.cursor() as cur:
        st._db.execute(cur, 'UPDATE scan_runs SET source=%s', (source,))
    st.create_disposition_audit('namespace-audit', doc_id='scan:scan-exec-1:a.docx',
        policy_id='namespace-rule', action='delete', result='applied', detail='legacy row', owner_email=OWNER)
    st.set_disposition_before_state('namespace-audit', {'action': 'delete', 'trashed': False})
    st.set_lifecycle_status('scan-exec-1','a.docx','Deleted')
    result = gated_client(OWNER).post('/disposition/approvals/namespace-audit/undo')
    assert result.status_code == 502, result.text
    assert drive._files.touched == []
