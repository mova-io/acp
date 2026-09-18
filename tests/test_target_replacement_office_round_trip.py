"""End to end: an approved 1.4.5 OCR replacement retires the pending 1.1.1 row for that picture.

Real python-docx package, the production approved writer (`handlers._apply_approved_values`),
the production 1.4.5 image replacement, and the real re-scan by the real OfficeCLI analyser (and
OCR when installed). The only thing patched is blob storage, which stores bytes verbatim.

WHY THIS IS NOT A `test_remediation_verified_*` MODULE. That prefix is the lane-proof family:
tests/test_capability_assisted_contract.py requires each member to declare the (format,
criterion) lane it PROVES, and a lane may be declared by one fixture only. This proves no new
lane — (docx, 1.4.5) is already test_remediation_verified_office_image_replacement's — it proves
what happens to a DIFFERENT row after that lane runs. Nor may it use conftest's no-CLI stand-in:
the 1.1.1 finding location (`docx:drawing:{id}:paragraph:{i}`) exists only in the real .NET
analyser's output, so a stand-in run could not reproduce the production finding at all. It
therefore needs the built OfficeCLI (CI builds it) and skips, saying so, where it is absent.

What is asserted is the whole production story in one pass: the picture is replaced and saved,
the 1.4.5 row is applied and its finding verified, and the 1.1.1 row that asked a human to
describe the SAME picture is withdrawn from the work list on the job's own complete re-scan —
still `pending` in the database, with no alt text written anywhere, its finding
`superseded_by_reassessment`, and one decision_log line naming the evidence.
"""
from __future__ import annotations

import json
import sys
from hashlib import sha256

import pytest

import review_target_reconciliation as rtr
from test_remediation_verified_office_image_replacement import TEXT, document, members

SID = 'rv-target-replacement'
FILE = 'UTSW_Discharge_Summary.docx'
OWNER = 'owner@example.com'


class _Blob:
    def __init__(self, data):
        self.data, self.uploads = data, []

    def download_remediated(self, owner, sid, f):
        return self.data

    def upload_remediated(self, owner, sid, f, data, mime):
        self.data = data
        self.uploads.append(f)
        return 'http://b/2'


@pytest.fixture
def store(isolated_store):
    return isolated_store


def _seed(store, source):
    """Assessment from the REAL scan of the source, a ledger, and the two production rows."""
    from proposals import verify_residual
    assessed = verify_residual(source, FILE)
    assert assessed.ok, assessed.reason
    issues = assessed.assessment['issues']
    alt = [i for i in issues if i.get('wcag') == 'SC_1_1_1']
    assert [i['location'] for i in alt] == ['docx:drawing:1:paragraph:2'], alt
    with store._db.cursor() as cur:
        store._db.execute(cur,
            "INSERT INTO scan_runs(id,source,status,workflow_id,workflow_revision,owner_email) "
            "VALUES(%s,'local','done',%s,1,%s)", (SID, SID, OWNER))
        store._db.execute(cur, "INSERT INTO file_records(scan_id,file,checksum) VALUES(%s,%s,'c')",
                          (SID, FILE))
        for sc, name, count in (('1.1.1', 'Non-text Content', 1), ('1.4.5', 'Images of Text', 1)):
            store._db.execute(cur,
                "INSERT INTO scan_rule_traces(scan_id,file,rule_id,rule_name,plain_name,level,"
                "fix_mode,outcome,finding_count) VALUES(%s,%s,%s,%s,%s,'A','ai','REVIEW',%s)",
                (SID, FILE, sc, name, name, count))
        for issue in alt:
            store._db.execute(cur,
                "INSERT INTO issue_records(scan_id,file,rule_id,wcag,severity,detail,location) "
                "VALUES(%s,%s,%s,%s,'CRITICAL','d',%s)",
                (SID, FILE, issue.get('ruleId'), issue['wcag'], issue['location']))
    batch = store.enqueue_stage_batch(SID, 'remediate', 'remediate_file',
        [{'scan_id': SID, 'file': FILE}], snapshot_id=SID, request_fingerprint=SID)['batch_id']
    store.seed_finding_dispositions(SID, batch)
    store.record_remediation(SID, FILE, blob_url='http://b/1', corrected_sha256=sha256(source).hexdigest())
    alt_id = store.enqueue_proposals(SID, FILE, '1.1.1', [
        {'locator': 'word/document.xml#Picture 1', 'before': '',
         'proposed_value': 'Synthetic discharge instructions banner', 'source': 'vision'}],
        rule_name='Non-text Content')
    fixer = store.enqueue_proposals(SID, FILE, '1.4.5', [
        {'locator': 'image 1', 'before': 'text baked into an image', 'proposed_value': TEXT,
         'source': 'OCR'}], rule_name='Images of Text')
    # The production decision path for the 1.4.5 approval.
    store.complete_hitl_decision(fixer, 'approved', None, None, resolution=None,
                                 approved_values=[TEXT], actor=OWNER, detail=None)
    return batch, alt_id, fixer


def test_approved_ocr_replacement_retires_the_pending_alt_row(store, monkeypatch):
    import core
    import engines
    import handlers
    if not engines.OFFICE_OK:
        pytest.skip('needs the built OfficeCLI analyser: the docx:drawing finding location '
                    'exists only in its output')
    source = document('docx')
    batch, alt_id, fixer = _seed(store, source)
    blob = _Blob(source)
    monkeypatch.setattr(core, 'store', store)
    monkeypatch.setitem(sys.modules, 'blob', blob)

    handlers._apply_approved_values({'scan_id': SID, 'file': FILE}, {})

    # The replacement happened and was credited on the real re-scan.
    assert blob.uploads == [FILE]
    assert not any('/media/' in p for p in members(blob.data))
    assert store.get_hitl_item(fixer)['applied']
    dispositions = {f['rule_id']: f for f in store.list_finding_dispositions(SID, batch)}
    assert dispositions['1.4.5']['disposition'] == 'resolved_verified'
    corrected = sha256(blob.data).hexdigest()
    assert store.get_file_record(SID, FILE)['corrected_sha256'] == corrected

    # The 1.1.1 row for the SAME picture is withdrawn — and only withdrawn.
    row = store.get_hitl_item(alt_id)
    assert row['status'] == 'pending' and not row.get('applied')
    assert not any(p.get('approved_value') for p in row['proposals'])
    xml = members(blob.data)['word/document.xml'].decode()
    assert 'Synthetic discharge instructions banner' not in xml     # no alt text written
    assert alt_id not in {r['id'] for r in store.list_hitl_queue(scan_id=SID)}
    audit = next(r for r in store.list_hitl_queue(scan_id=SID, include_superseded=True)
                 if r['id'] == alt_id)
    assert audit['superseded_reason'] == 'target_removed_by_verified_fix'
    evidence = audit['superseded_evidence']
    assert evidence['assessment'] == 'apply.verification'
    assert evidence['assessment_status'] == 'analysed' and evidence['skipped_rules'] == 0
    assert evidence['removed_by_item_id'] == fixer and evidence['removed_by_rule_id'] == '1.4.5'
    assert evidence['corrected_artifact_sha256'] == corrected
    assert evidence['source_artifact_sha256'] == sha256(source).hexdigest()
    assert dispositions['1.1.1']['disposition'] == 'superseded_by_reassessment'
    assert evidence['finding_ids'] == [dispositions['1.1.1']['finding_id']]
    lines = [d for d in store.list_decisions(scan_id=SID) if d['action'] == rtr.ACTION]
    assert len(lines) == 1
    assert dispositions['1.1.1']['fix_evidence_ids'] == [f"target_removed:{lines[0]['id']}"]
    assert json.loads(lines[0]['detail'])['source_kind'] == 'prior_corrected_copy'

    # Nothing can bring the stale row back into a writer.
    assert store.retry_approved_write(alt_id)['accepted'] is False
    with pytest.raises(ValueError, match='stale proposal selection'):
        store.complete_hitl_decision(alt_id, 'approved', None, None, resolution=None,
                                     approved_values=None, actor=OWNER, detail=None)
