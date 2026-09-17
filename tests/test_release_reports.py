"""Real store reports retain unresolved work without inventing publication or audit."""
import pytest
from release_reports import build_release_report_sources as build_release_reports

OWNER = 'owner@example.com'


def setup(store):
    store.init_scan_run('scan', 'sharepoint', 2, '2026-09-09T10:00:00Z', 'rubric', 'hash', owner=OWNER, status='completed')
    release = store.ensure_release_execution('scan', OWNER, 'sharepoint', 2)
    for name, status in [('one.pdf', 'published'), ('two.pdf', 'failed')]:
        store.record_release_document(release['id'], OWNER, {'file': name, 'status': status, 'published_url': 'https://example.com/one' if status == 'published' else 'javascript:alert(1)', 'explanation': '<script>bad</script>'})
    return release['id']


def test_real_release_honest_outcomes_and_unknown_audit(isolated_store):
    release = setup(isolated_store)
    assets = build_release_reports(isolated_store, 'scan', OWNER, release)
    assert len(assets) == 6
    summary = assets[0]['content'].decode()
    assert 'Files published in this release</td><td>1' in summary
    assert 'Publication failures</td><td>1' in summary
    assert 'Original assessment findings (immutable, whole scan)</td><td>Not recorded' in summary
    assert 'Original findings fixed and verified</td><td>Not recorded' in summary
    assert 'https://example.com/one' in summary
    assert 'javascript:' not in summary
    assert 'not certify full accessibility compliance' in summary
    failed = next(a['content'].decode() for a in assets if a['name'].startswith('checklist-two'))
    assert 'Not published' in failed
    assert '&lt;script&gt;bad&lt;/script&gt;' in failed
    assert '<script>' not in failed


def test_owner_and_scan_scope(isolated_store):
    release = setup(isolated_store)
    with pytest.raises(KeyError):
        build_release_reports(isolated_store, 'scan', 'other@example.com', release)
    with pytest.raises(KeyError):
        build_release_reports(isolated_store, 'other-scan', OWNER, release)


def test_evidence_identity_required_and_remaining_checklist(isolated_store, monkeypatch):
    release = setup(isolated_store)
    # Use actual persisted scan/issues/diffs, with a minimal sealed execution fixture.
    isolated_store.save_file_result('scan', {'file': 'one.pdf', 'engine': 'pdf', 'status': 'fail', 'score': 0, 'compliant': False, 'skipped_rules': 0, 'issues': [{'ruleId': 'SC_1_1_1', 'wcag': '1.1.1', 'severity': 'critical', 'detail': '=Missing <alt>', 'page': 3}]}, '2026-09-09T10:00:00Z')
    isolated_store.record_remediation_diffs('scan', 'one.pdf', [{'rule_id': 'SC_2_4_2', 'before': '', 'after': 'Title'}])
    monkeypatch.setattr(isolated_store, 'canonical_stage_lineage', lambda *a, **kw: {'stages': [{'stage': 'remediate', 'execution_id': 'batch', 'input_manifest_id': 'manifest'}]})
    monkeypatch.setattr(isolated_store, 'get_stage_output_manifest', lambda *a, **kw: {'entries': [{'assessment_summary': {'findings_recorded': 2, 'finding_groups': [{'file': 'one.pdf', 'rule_id': 'SC_2_4_2', 'finding_count': 1}, {'file': 'one.pdf', 'rule_id': 'SC_1_1_1', 'finding_count': 1}]}}]})
    monkeypatch.setattr(isolated_store, 'list_finding_dispositions', lambda *a: [
        {'finding_id': 'a', 'file': 'one.pdf', 'rule_id': 'SC_2_4_2', 'disposition': 'resolved_verified', 'verified_at': '2026-09-09', 'fix_evidence_ids': ['remediation_diff:one.pdf:SC_2_4_2:0']},
        {'finding_id': 'b', 'file': 'one.pdf', 'rule_id': 'SC_1_1_1', 'disposition': 'resolved_verified', 'verified_at': '2026-09-09', 'fix_evidence_ids': ['nonexistent']},
    ])
    assets = build_release_reports(isolated_store, 'scan', OWNER, release)
    summary = assets[0]['content'].decode()
    assert 'Original findings fixed and verified</td><td>1' in summary
    assert 'Original findings not yet verified fixed</td><td>1' in summary
    checklist = next(a['content'].decode() for a in assets if a['name'].startswith('checklist-one'))
    assert '=Missing &lt;alt&gt;' in checklist
    assert '<strong>Location:</strong> Page: 3</p>' in checklist
    csv = assets[-1]['content'].decode('utf-8-sig')
    assert "'=Missing <alt>" in csv


def test_only_release_documents_and_selected_unfinished_checks(isolated_store, monkeypatch):
    release = setup(isolated_store)
    isolated_store.save_file_result('scan', {'file': 'unselected.pdf', 'engine': 'pdf', 'status': 'pass', 'score': 100, 'compliant': True, 'skipped_rules': 0}, '2026-09-09T10:00:00Z')
    monkeypatch.setattr(isolated_store, 'get_scan_traces', lambda *a: [
        {'file': 'one.pdf', 'rule_id': 'SC_1_1_1', 'outcome': 'REVIEW'},
        {'file': 'one.pdf', 'rule_id': 'SC_1_4_3', 'outcome': 'NOT_EVALUATED'},
        {'file': 'one.pdf', 'rule_id': 'SC_2_4_4', 'outcome': 'ERROR'},
    ])
    monkeypatch.setattr(isolated_store, 'get_scan_scope', lambda *a: {'1.1.1': {'pdf'}, '2.4.4': {'pdf'}})
    assets = build_release_reports(isolated_store, 'scan', OWNER, release)
    assert len(assets) == 6
    summary = assets[0]['content'].decode()
    assert 'unselected.pdf' not in summary
    assert 'Checks not completed (current recorded traces)</td><td>1' in summary
    checklist = next(a['content'].decode() for a in assets if a['name'].startswith('checklist-one'))
    assert 'Check not completed: ERROR' in checklist
    assert 'Check not completed: REVIEW' not in checklist
    assert '1.4.3' not in checklist


def test_verified_applied_approval_is_not_reported_unverified_but_failures_remain(isolated_store):
    release = setup(isolated_store)
    isolated_store.save_file_result('scan', {
        'file': 'one.pdf', 'engine': 'pdf', 'status': 'fail', 'score': 80,
        'compliant': False, 'skipped_rules': 0,
        'issues': [{'ruleId': 'SC_2_4_2', 'wcag': '2.4.2', 'severity': 'serious',
                    'detail': 'Remaining title issue in another location', 'page': 3}],
    }, '2026-09-09T10:00:00Z')
    isolated_store.record_remediation_diffs('scan', 'one.pdf', [
        {'rule_id': 'SC_2_4_2', 'before': '', 'after': 'Title'},
        {'rule_id': 'SC_3_1_1', 'before': '', 'after': 'en'},
    ])
    with isolated_store._db.cursor() as cur:
        for item_id, rule_id, name in [('title', '2.4.2', 'Page titled'), ('language', '3.1.1', 'Language corrected')]:
            isolated_store._db.execute(cur,
                "INSERT INTO hitl_queue(id,scan_id,file,rule_id,rule_name,status,applied) VALUES(%s,'scan','one.pdf',%s,%s,'approved',1)",
                (item_id, rule_id, name))
    assets = build_release_reports(isolated_store, 'scan', OWNER, release)
    summary = assets[0]['content'].decode()
    checklist = next(a['content'].decode() for a in assets if a['name'].startswith('checklist-one'))
    assert 'Applied, verification not recorded' not in checklist
    assert 'Language corrected' not in checklist
    assert 'Remaining title issue in another location' in checklist
    assert 'Remaining issue' in checklist
    assert 'Applied review records without matching verification evidence (not findings)</td><td>0' in summary


@pytest.mark.parametrize('status', ['approved', 'resolved'])
def test_applied_review_status_without_verification_evidence_remains_in_checklist(isolated_store, status):
    release = setup(isolated_store)
    # A different criterion's verified record cannot credit this review item.
    isolated_store.record_remediation_diffs('scan', 'one.pdf', [
        {'rule_id': 'SC_3_1_1', 'before': '', 'after': 'en'},
    ])
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur,
            "INSERT INTO hitl_queue(id,scan_id,file,rule_id,rule_name,status,applied) VALUES('unverified','scan','one.pdf','2.4.2','Page title',%s,1)",
            (status,))
    assets = build_release_reports(isolated_store, 'scan', OWNER, release)
    summary = assets[0]['content'].decode()
    checklist = next(a['content'].decode() for a in assets if a['name'].startswith('checklist-one'))
    assert 'Applied, verification not recorded' in checklist
    assert 'Page title' in checklist
    assert 'Applied review records without matching verification evidence (not findings)</td><td>1' in summary


def test_branded_documents_categories_and_escaped_change_details(isolated_store, monkeypatch):
    release = setup(isolated_store)
    isolated_store.save_file_result('scan', {'file': 'one.pdf', 'engine': 'pdf', 'status': 'fail', 'score': 0, 'compliant': False, 'skipped_rules': 0, 'issues': [{'ruleId': 'SC_1_1_1', 'wcag': '1.1.1', 'severity': 'critical', 'detail': 'Missing alt'}]}, '2026-09-09T10:00:00Z')
    isolated_store.record_remediation_diffs('scan', 'one.pdf', [{'rule_id': 'SC_2_4_2', 'before': '<unsafe>', 'after': 'Document title'}])
    monkeypatch.setattr(isolated_store, 'get_scan_traces', lambda *a: [{'file': 'one.pdf', 'rule_id': 'SC_1_1_1', 'outcome': 'FAIL', 'fix_mode': 'ai-assisted', 'finding_count': 1}])
    assets = build_release_reports(isolated_store, 'scan', OWNER, release)
    summary = assets[0]['content'].decode()
    assert 'alt="Mova iO"' in summary
    assert 'data:image/png;base64,' in summary
    assert 'Remediation category / SC' in summary
    assert 'AI suggestion needed' in summary
    assert 'SC 1.1.1' in summary
    detail = next(a['content'].decode() for a in assets if a['name'].startswith('checklist-one'))
    assert '<th scope="col">Severity</th>' not in detail
    assert '<strong>Severity:</strong> critical' in detail
    assert '&lt;unsafe&gt;' not in detail
    assert 'Recorded changes by success criterion' not in detail
    detail = next(a['content'].decode() for a in assets if a['name'].startswith('changes-one'))
    assert '&lt;unsafe&gt;' in detail
    assert 'SC 2.4.2' in detail
    assert 'Document title' in detail


@pytest.mark.parametrize('row, expected', [
    ({'fix_mode': 'auto'}, 'automatic'), ({'fix_mode': 'ai-assisted'}, 'suggestion'),
    ({'fix_mode': 'human'}, 'manual'), ({'remediation_supported': False}, 'unsupported'),
    ({'outcome': 'ERROR'}, 'blocked'),
])
def test_report_remediation_categories(row, expected):
    from release_reports import _category
    assert _category(row) == expected


def test_approval_does_not_establish_verification():
    from release_reports import _category
    assert _category(task={'status': 'approved', 'applied': True}) == 'applied'
    assert _category(verified=True) == 'verified'


def test_report_and_ui_share_approved_category_names():
    from pathlib import Path
    from release_reports import CATEGORIES
    frontend = (Path(__file__).resolve().parent.parent / 'frontend/src/remediationCategories.js').read_text()
    assert len(CATEGORIES) == 8
    for label in CATEGORIES.values():
        assert repr(label) in frontend


def test_pdf_reports_include_brand_evidence_and_no_csv(isolated_store, tmp_path):
    from release_reports import build_release_reports as build_pdfs
    from pypdf import PdfReader
    import io
    release = setup(isolated_store)
    assets = build_pdfs(isolated_store, 'scan', OWNER, release)
    assert len(assets) == 5
    for asset in assets:
        assert asset['name'].endswith('.pdf')
        assert asset['content_type'] == 'application/pdf'
        reader = PdfReader(io.BytesIO(asset['content']))
        text = '\n'.join(page.extract_text() for page in reader.pages)
        assert 'Not recorded' in text or 'No change records' in text
        assert 'Mova iO' in text
        assert 'Publication' in text
        assert reader.trailer['/Root'].get('/StructTreeRoot')
        assert any(page.images for page in reader.pages)
        (tmp_path / asset['name']).write_bytes(asset['content'])


def test_printable_pdf_preserves_locations_all_criteria_and_human_checkboxes(isolated_store, monkeypatch, tmp_path):
    from release_reports import build_release_reports as build_pdfs
    from pypdf import PdfReader
    import io
    release = setup(isolated_store)
    isolated_store.save_file_result('scan', {'file': 'one.pdf', 'engine': 'pdf', 'status': 'fail', 'score': 0,
        'compliant': False, 'skipped_rules': 0, 'issues': [
            {'ruleId': 'SC_1_1_1', 'wcag': '1.1.1', 'severity': 'critical', 'detail': 'Figure missing alt text', 'page': 3, 'location': 'Figure 2'},
            {'ruleId': 'SC_1_1_1', 'wcag': '1.1.1', 'severity': 'critical', 'detail': 'Second figure needs context', 'page': 5},
        ]}, '2026-09-09T10:00:00Z')
    monkeypatch.setattr(isolated_store, 'get_scan_traces', lambda *a: [
        {'file': 'one.pdf', 'rule_id': 'SC_1_1_1', 'outcome': 'FAIL', 'finding_count': 2},
        {'file': 'one.pdf', 'rule_id': 'SC_3_1_1', 'outcome': 'PASS', 'finding_count': 0},
    ])
    isolated_store.record_remediation_diffs('scan', 'one.pdf', [{'rule_id': 'SC_3_1_1', 'before': 'Not declared', 'after': 'en-US', 'note': 'Document language declaration rechecked.'}])
    for asset in build_pdfs(isolated_store, 'scan', OWNER, release):
        (tmp_path / asset['name']).write_bytes(asset['content'])
        if asset['name'].startswith(('checklist-two', 'changes-')):
            continue
        text = '\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(asset['content'])).pages)
        for expected in ['Figure missing alt text', 'Second figure needs context', 'Page: 3', 'Page: 5',
                         'Figure 2', 'Remediated and rechecked', 'Reviewer / date / notes:',
                         'Non-text Content']:
            assert expected in text


def test_checklist_omits_automatic_review_coverage_and_unselected_issues(isolated_store, monkeypatch):
    release = setup(isolated_store)
    isolated_store.save_file_result('scan', {'file': 'one.pdf', 'engine': 'pdf', 'status': 'fail', 'score': 0,
        'compliant': False, 'skipped_rules': 0, 'issues': [
            {'ruleId': 'PDF-LANG-001', 'wcag': '3.1.1', 'severity': 'serious', 'detail': 'Language missing', 'location': 'Location: document properties'},
            {'ruleId': 'SC_1_4_3', 'wcag': '1.4.3', 'severity': 'serious', 'detail': 'Unselected contrast issue'},
        ]}, '2026-09-09T10:00:00Z')
    monkeypatch.setattr(isolated_store, 'get_scan_scope', lambda *a: {'3.1.1': {'pdf'}, '2.4.6': {'pdf'}})
    monkeypatch.setattr(isolated_store, 'get_scan_traces', lambda *a: [
        {'file': 'one.pdf', 'rule_id': 'SC_2_4_6', 'outcome': 'REVIEW', 'fix_mode': 'auto', 'plain_name': 'Automatic heading review'},
    ])
    assets = build_release_reports(isolated_store, 'scan', OWNER, release)
    checklist = next(a['content'].decode() for a in assets if a['name'].startswith('checklist-one'))
    assert 'Language missing' in checklist
    assert 'data-criterion="3.1.1"' in checklist
    assert 'PDF-LANG-001' not in checklist
    assert 'Location: Location:' not in checklist
    assert 'Automatic heading review' not in checklist
    assert 'Unselected contrast issue' not in checklist
    assert 'Success criteria coverage' not in checklist
    assert 'Recorded changes' not in checklist


def test_verified_original_issue_removed_only_with_complete_ledger(isolated_store, monkeypatch):
    release = setup(isolated_store)
    isolated_store.save_file_result('scan', {'file': 'one.pdf', 'engine': 'pdf', 'status': 'fail', 'score': 0,
        'compliant': False, 'skipped_rules': 0, 'issues': [
            {'wcag': '2.4.2', 'ruleId': 'SC_2_4_2', 'severity': 'serious', 'detail': 'Original missing title'},
        ]}, '2026-09-09T10:00:00Z')
    isolated_store.record_remediation_diffs('scan', 'one.pdf', [{'rule_id': 'SC_2_4_2', 'before': '', 'after': 'Title'}])
    monkeypatch.setattr(isolated_store, 'canonical_stage_lineage', lambda *a, **kw: {'stages': [{'stage': 'remediate', 'execution_id': 'batch', 'input_manifest_id': 'manifest'}]})
    monkeypatch.setattr(isolated_store, 'get_stage_output_manifest', lambda *a, **kw: {'entries': [{'assessment_summary': {'findings_recorded': 1, 'finding_groups': [{'file': 'one.pdf', 'rule_id': 'SC_2_4_2', 'finding_count': 1}]}}]})
    monkeypatch.setattr(isolated_store, 'list_finding_dispositions', lambda *a: [
        {'finding_id': 'a', 'file': 'one.pdf', 'rule_id': 'SC_2_4_2', 'disposition': 'resolved_verified', 'verified_at': '2026-09-09', 'fix_evidence_ids': ['remediation_diff:one.pdf:SC_2_4_2:0']},
    ])
    assets = build_release_reports(isolated_store, 'scan', OWNER, release)
    checklist = next(a['content'].decode() for a in assets if a['name'].startswith('checklist-one'))
    assert 'Original missing title' not in checklist
    assert 'data-criterion="2.4.2"' not in checklist


def test_report_scope_uses_per_file_effective_selection(isolated_store, monkeypatch):
    release = setup(isolated_store)
    monkeypatch.setattr(isolated_store, 'get_scan_scope', lambda *a: {'1.1.1': {'pdf'}})
    monkeypatch.setattr(isolated_store, 'scope_for_file', lambda sid, name, scope: {'2.4.4': {'pdf'}} if name == 'one.pdf' else scope)
    monkeypatch.setattr(isolated_store, 'get_scan_traces', lambda *a: [
        {'file': 'one.pdf', 'rule_id': 'SC_1_1_1', 'outcome': 'FAIL', 'finding_count': 1},
        {'file': 'one.pdf', 'rule_id': 'SC_2_4_4', 'outcome': 'FAIL', 'finding_count': 1},
    ])
    assets = build_release_reports(isolated_store, 'scan', OWNER, release)
    checklist = next(a['content'].decode() for a in assets if a['name'].startswith('checklist-one'))
    assert 'data-criterion="2.4.4"' in checklist
    assert 'data-criterion="1.1.1"' not in checklist


def test_change_report_shows_saved_unverified_ai_without_credit(isolated_store, monkeypatch, tmp_path):
    import unverified_changes
    release = setup(isolated_store)
    monkeypatch.setattr(isolated_store, 'get_scan_scope', lambda *a: {'1.1.1': {'pdf'}})
    monkeypatch.setattr(unverified_changes, 'saved_changes', lambda store, sid, name: [
        {'file': name, 'rule_id': 'SC_1_1_1', 'locator': 'Page 3, figure 2',
         'before': 'Missing', 'after': '<Suggested description>', 'reason': 'Human meaning check required',
         'verified': False, 'verification': 'not_verified'},
        {'file': name, 'rule_id': 'SC_3_1_1', 'before': 'Unselected original', 'after': 'Unselected language'},
    ] if name == 'one.pdf' else [])
    assets = build_release_reports(isolated_store, 'scan', OWNER, release)
    change = next(a['content'].decode() for a in assets if a['name'].startswith('changes-one'))
    from release_report_pdf import render_report_pdf
    (tmp_path / 'unverified-change.pdf').write_bytes(render_report_pdf(change))
    assert 'Applied AI changes - not verified' in change
    assert 'Page 3, figure 2' in change
    assert '&lt;Suggested description&gt;' in change
    assert 'Human meaning check required' in change
    assert 'Unselected language' not in change
    assert 'No change records' not in change
    assert 'Original findings fixed and verified</td><td>Not recorded' in assets[0]['content'].decode()
    checklist = next(a['content'].decode() for a in assets if a['name'].startswith('checklist-one'))
    assert 'Suggested description' not in checklist


def test_fresh_unknown_copy_report_and_manifest_keep_exact_artifact_uncertainty(isolated_store, monkeypatch):
    import json
    import core
    from routes import scans
    release_id = setup(isolated_store)
    digest = 'a' * 64
    isolated_store.record_release_document(release_id, OWNER, {'file': 'one.pdf',
        'status': 'published', 'artifact_digest': 'sha256:' + digest})
    evidence = {'artifact_sha256': digest, 'release_id': release_id,
        'assessment_ok': False, 'assessment_status': 'unavailable',
        'remaining_issues': None, 'remaining_criteria': [], 'reason': 'engine unavailable'}
    isolated_store.log_decision(OWNER, 'release.corrected_copy_assessed', scan_id='scan',
        file='one.pdf', detail=json.dumps(evidence))
    assets = build_release_reports(isolated_store, 'scan', OWNER, release_id)
    report = next(asset['content'].decode() for asset in assets if asset['name'].startswith('checklist-one'))
    assert 'Current remaining findings are unknown' in report
    assert 'No remaining issues are recorded' not in report
    assert 'engine unavailable' in report and digest in report
    exported = json.loads(next(asset['content'] for asset in assets if asset['name'] == 'saved-copy-assessments.json'))
    assert exported['documents'][0]['remaining_issues'] is None
    monkeypatch.setattr(core, 'store', isolated_store)
    manifest = scans._release_manifest_payload(isolated_store.release_status(release_id, OWNER),
        scan_id='scan', owner=OWNER, snapshot_id='snapshot')
    document = next(row for row in manifest['documents'] if row['file'] == 'one.pdf')
    assert document['corrected_copy_assessment'] == evidence


def _two_findings(store, monkeypatch):
    release = setup(store)
    store.save_file_result('scan', {'file': 'one.pdf', 'engine': 'pdf', 'status': 'fail', 'score': 0,
        'compliant': False, 'skipped_rules': 0, 'issues': [
            {'ruleId': 'SC_1_1_1', 'wcag': '1.1.1', 'severity': 'critical', 'detail': 'Figure missing alt text', 'page': 3, 'location': 'Figure 2'},
            {'ruleId': 'SC_1_1_1', 'wcag': '1.1.1', 'severity': 'critical', 'detail': 'Second figure needs context', 'page': 5},
        ]}, '2026-09-09T10:00:00Z')
    monkeypatch.setattr(store, 'get_scan_traces', lambda *a: [
        {'file': 'one.pdf', 'rule_id': 'SC_1_1_1', 'outcome': 'FAIL', 'finding_count': 2}])
    return release


def test_each_remaining_finding_is_one_card_with_its_instructions(isolated_store, monkeypatch):
    """The checklist used to print every finding twice: a checklist card, then the same finding
    again in a separate offline-guide card (two findings -> four cards, three pages). One card per
    finding now carries the checklist facts AND the specific steps, verification and follow-up."""
    import re
    release = _two_findings(isolated_store, monkeypatch)
    assets = build_release_reports(isolated_store, 'scan', OWNER, release)
    checklist = next(a['content'].decode() for a in assets if a['name'].startswith('checklist-one'))
    cards = re.findall(r'<section class="report-card finding-card"[^>]*>(.*?)</section>', checklist, re.S)
    assert len(cards) == 2, f'expected one card per finding, got {len(cards)}'
    for detail, location in [('Figure missing alt text', 'Page: 3; Figure 2'), ('Second figure needs context', 'Page: 5')]:
        assert checklist.count(detail) == 1, f'{detail!r} is printed more than once'
        card = next(c for c in cards if detail in c)
        # Every piece of information the two copies used to carry, together in one card.
        for expected in [f'<strong>Location:</strong> {location}</p>', '<strong>Severity:</strong> critical',
                         'Remediation category:', '<strong>Recommended action:</strong>',
                         '<strong>How to fix:</strong>', 'Acrobat Pro',
                         'Technical verification not recorded · Human confirmation of meaning not recorded',
                         'Remediated and rechecked', 'Reviewer / date / notes:',
                         'does not record anything in ACP', '<strong>Owner:</strong> Unassigned']:
            assert expected in card, (expected, card)
    assert checklist.count('Offline remediation guide') == 1


def test_unlinked_guide_recommendation_still_gets_its_own_card(isolated_store, monkeypatch):
    release = setup(isolated_store)
    monkeypatch.setattr(isolated_store, 'list_hitl_queue', lambda **kw: [
        {'id': 't', 'file': 'one.pdf', 'rule_id': '4.1.2', 'status': 'pending', 'locator': 'pdf:field:x',
         'proposals': [{'value': 'Patient full name'}]}])
    assets = build_release_reports(isolated_store, 'scan', OWNER, release)
    checklist = next(a['content'].decode() for a in assets if a['name'].startswith('checklist-one'))
    assert checklist.count('Patient full name') == 1
    assert checklist.count('class="report-card finding-card"') == checklist.count('data-criterion="4.1.2"') >= 1


def test_two_finding_checklist_pdf_is_shorter_and_not_duplicated(isolated_store, monkeypatch, tmp_path):
    from io import BytesIO
    from pypdf import PdfReader
    from release_reports import build_release_reports as build_pdfs
    release = _two_findings(isolated_store, monkeypatch)
    asset = next(a for a in build_pdfs(isolated_store, 'scan', OWNER, release) if a['name'].startswith('checklist-one'))
    (tmp_path / 'checklist.pdf').write_bytes(asset['content'])
    reader = PdfReader(BytesIO(asset['content']))
    text = '\n'.join(p.extract_text() for p in reader.pages)
    # Three pages before the cards were merged; the same evidence now fits in fewer.
    assert len(reader.pages) < 3, len(reader.pages)
    assert text.count('Second figure needs context') == 1
    assert text.count('Remediated and rechecked') == 2
    assert f'Page 1 of {len(reader.pages)}' in text
    assert '☐' in text
