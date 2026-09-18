"""Approved tag plans reach the saved PDF through the normal application job."""
import hashlib
import io
import json

import pikepdf

from test_apply_approved_values import store, _Blob, _run_handler
from test_pdf_structure_repairs import fixture, target, CONTENT
from hitl_viewed import approve_bound


def seed(st, data, *, rule='2.4.6', plan=None, locator=None):
    st.init_scan_run('s1', 'sharepoint', 1, '2026-09-12T00:00:00Z', 'rubric', 'hash')
    st.save_file_result('s1', {'file': 'tagged.pdf', 'engine': 'pdf', 'status': 'fail',
        'score': 0, 'compliant': False, 'skipped_rules': 0,
        'issues': [{'ruleId': 'SC_2_4_6', 'wcag': '2.4.6', 'severity': 'serious'}]}, '2026-09-12T00:00:00Z')
    st.record_remediation('s1', 'tagged.pdf', blob_url='https://example.org/corrected')
    locator = locator or target(data, 'P', 'Introduction')
    item = st.enqueue_proposals('s1', 'tagged.pdf', rule, [{'locator': locator,
        'proposed_value': json.dumps(plan or {'op': 'heading', 'role': 'H1'}),
        'before': '(paragraph)', 'kind': 'pdf-tag-heading', 'source': 'existing tag heuristic'}])
    approve_bound(st, item, [])
    return item, locator


def test_normal_job_saves_exact_heading_and_credits_only_its_criterion(store, monkeypatch):
    data = fixture()
    _, locator = seed(store, data)
    assert store.has_approved_values_to_write('s1', 'tagged.pdf')
    blob = _Blob(data)
    _run_handler(monkeypatch, store, blob, residual=set(), file='tagged.pdf')
    assert len(blob.uploads) == 1
    with pikepdf.open(io.BytesIO(blob.data)) as pdf:
        assert str(pdf.Root.StructTreeRoot.K[0].K[0].S) == '/H1'
        assert pdf.pages[0].Contents.read_bytes() == CONTENT
        assert str(pdf.Root.AcroForm.Fields[0].V) == 'patient value'
    assert store.approved_pdf_structure_values('s1', 'tagged.pdf', '2.4.6') == {}
    diffs = store.get_remediation_diffs('s1', 'tagged.pdf')
    assert diffs and {d['rule_id'] for d in diffs} == {'2.4.6'}
    assert store.get_file_record('s1', 'tagged.pdf')['corrected_sha256'] == hashlib.sha256(blob.data).hexdigest()


def test_wrong_criterion_does_not_admit_a_heading_plan(store):
    data = fixture()
    seed(store, data, rule='1.3.1')
    assert store.approved_pdf_structure_values('s1', 'tagged.pdf', '1.3.1') == {}
    assert store.approved_pdf_structure_values('s1', 'tagged.pdf', '2.4.6') == {}


def test_stale_plan_leaves_approval_unwritten_and_pdf_unchanged(store, monkeypatch):
    data = fixture()
    seed(store, data)
    with pikepdf.open(io.BytesIO(data)) as pdf:
        pdf.pages[0].Contents = pdf.make_stream(CONTENT + b'\n% changed source')
        out = io.BytesIO(); pdf.save(out)
    changed = out.getvalue()
    blob = _Blob(changed)
    _run_handler(monkeypatch, store, blob, residual={'2.4.6'}, file='tagged.pdf')
    assert blob.data == changed and not blob.uploads
    assert store.approved_pdf_structure_values('s1', 'tagged.pdf', '2.4.6')
    assert not store.get_remediation_diffs('s1', 'tagged.pdf')


def test_approved_table_is_saved_without_claiming_full_criterion_verified(store, monkeypatch):
    data = fixture()
    seed(store, data, rule='1.3.1', locator=target(data, 'TH', 'Name'),
         plan={'op': 'header-scope', 'scope': 'Column'})
    blob = _Blob(data)
    _run_handler(monkeypatch, store, blob, residual={'1.3.1'}, file='tagged.pdf')
    assert blob.uploads
    with pikepdf.open(io.BytesIO(blob.data)) as pdf:
        assert str(pdf.Root.StructTreeRoot.K[0].K[2].K[0].K[0].A.Scope) == '/Column'
    assert not store.get_remediation_diffs('s1', 'tagged.pdf')
    assert store.get_file_record('s1', 'tagged.pdf')['compliant'] == 0
    assert store.approved_pdf_structure_values('s1', 'tagged.pdf', '1.3.1') == {}
