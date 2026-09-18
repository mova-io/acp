"""Exact saved bytes recover drafts, never writers, review decisions, or finding counts."""
from hashlib import sha256
import json
import pytest
import vision_recovery as recovery
from ai_run_policy import run_context

OWNER, SID, FILE = 'vision@example.test', 'vision-recovery', 'report.docx'
DATA = b'synthetic corrected artifact'
DIGEST = sha256(DATA).hexdigest()


def seed(store):
    with store._db.cursor() as cur:
        store._db.execute(cur, "INSERT INTO scan_runs(id,owner_email,status,source) VALUES(%s,%s,'done','local')", (SID, OWNER))
        store._db.execute(cur, "INSERT INTO file_records(scan_id,file,corrected_sha256,remediated_at) VALUES(%s,%s,%s,'2026-09-13')", (SID, FILE, DIGEST))
    batch = store.enqueue_stage_batch(SID, 'remediate', 'remediate_file', [{
        'scan_id': SID, 'file': FILE, 'owner': OWNER,
        'remediation_impact_policy': {'ai': 1, 'ai_zone': 'local', 'ai_budget_usd': '0.00'}}],
        snapshot_id=store.remediation_source_revision(SID), request_fingerprint='vision-recovery')
    job = store.get_job(batch['job_ids'][0])
    store.enqueue_proposals(SID, FILE, '1.1.1', [
        {'locator':'word/document.xml#r1','proposed_value':'','before':'(no alt text)'},
        {'locator':'word/document.xml#r2','proposed_value':'Author supplied caption','before':'(no alt text)'}], finding_count=8)
    with run_context(store, job['payload'], job) as context:
        recovery.schedule(store, context, job, ['vision_timeout'])
    rows = store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')
    payload = rows[0]['payload']
    return job, json.loads(payload) if isinstance(payload, str) else payload


def draft(monkeypatch):
    import blob, remediate_office, ai_standing_approval
    monkeypatch.setattr(blob, 'download_remediated', lambda *args: DATA)
    monkeypatch.setattr(ai_standing_approval, 'approve_file', lambda *args: None)
    monkeypatch.setattr(remediate_office, 'alt_proposals_for_office', lambda data, *args, **kwargs:
        ([{'locator':'word/document.xml#r1','before':'(no alt text)','proposed_value':'A useful recovered caption','model':'local-vision'}], []))


def test_exact_bytes_recover_only_pending_proposals(isolated_store, monkeypatch):
    _, payload = seed(isolated_store)
    draft(monkeypatch)
    before = isolated_store.get_file_record(SID, FILE)
    recovery.process(isolated_store, payload)
    item = isolated_store.get_hitl_item(payload['item_id'])
    assert item['status'] == 'pending'
    assert item['finding_count'] == 8
    assert item['proposals'][0]['proposed_value'] == 'A useful recovered caption'
    assert item['proposals'][1]['proposed_value'] == 'Author supplied caption'
    assert isolated_store.get_file_record(SID, FILE) == before


def test_review_changed_during_generation_is_preserved(isolated_store, monkeypatch):
    _, payload = seed(isolated_store)
    draft(monkeypatch)
    import remediate_office
    def changed(*args, **kwargs):
        with isolated_store._db.cursor() as cur:
            isolated_store._db.execute(cur, "UPDATE hitl_queue SET status='approved' WHERE id=%s", (payload['item_id'],))
        return [{'locator':'word/document.xml#r1', 'proposed_value':'Recovered draft'}], []
    monkeypatch.setattr(remediate_office, 'alt_proposals_for_office', changed)
    recovery.process(isolated_store, payload)
    item = isolated_store.get_hitl_item(payload['item_id'])
    assert item['status'] == 'approved'
    assert item['proposals'][0]['proposed_value'] == ''


@pytest.mark.parametrize('mutation', ['artifact', 'cancelled', 'approved', 'revised', 'disabled'])
def test_changed_input_or_review_never_dispatches(isolated_store, monkeypatch, mutation):
    _, payload = seed(isolated_store)
    import remediate_office
    monkeypatch.setattr(remediate_office, 'alt_proposals_for_office', lambda *a, **k: pytest.fail('unsafe dispatch'))
    with isolated_store._db.cursor() as cur:
        if mutation == 'artifact':
            isolated_store._db.execute(cur, "UPDATE file_records SET corrected_sha256='changed' WHERE scan_id=%s", (SID,))
        elif mutation == 'cancelled':
            isolated_store._db.execute(cur, "UPDATE stage_executions SET cancel_requested_at='now' WHERE execution_id=%s", (payload['run_id'],))
        elif mutation == 'approved':
            isolated_store._db.execute(cur, "UPDATE hitl_queue SET status='approved' WHERE id=%s", (payload['item_id'],))
        elif mutation == 'revised':
            isolated_store._db.execute(cur, "UPDATE hitl_queue SET proposals='[]' WHERE id=%s", (payload['item_id'],))
    if mutation == 'disabled':
        monkeypatch.setattr(isolated_store, 'get_ai_enabled', lambda: False)
    recovery.process(isolated_store, payload)
    assert len(isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')) == 1


def test_transient_retries_are_delayed_and_stop_after_two(isolated_store, monkeypatch):
    _, payload = seed(isolated_store)
    draft(monkeypatch)
    import remediate_office
    def unavailable(*args, **kwargs):
        recovery.record('circuit_open')
        return [], []
    monkeypatch.setattr(remediate_office, 'alt_proposals_for_office', unavailable)
    recovery.process(isolated_store, payload)
    rows = isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')
    assert len(rows) == 2
    second = next(json.loads(r['payload']) for r in rows if json.loads(r['payload'])['retry'] == 2)
    recovery.process(isolated_store, second)
    recovery.process(isolated_store, payload)
    assert len(isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')) == 2
    assert isolated_store.get_hitl_item(payload['item_id'])['proposals'][0]['proposed_value'] == ''


def test_capture_is_task_local_and_ignores_permanent_denials():
    with recovery.capture() as outer:
        recovery.record('vision_timeout')
        with recovery.capture() as inner:
            recovery.record('budget_exhausted')
            recovery.record('shared_capacity_busy')
        recovery.record('circuit_open')
    assert inner == ['shared_capacity_busy']
    assert outer == ['vision_timeout', 'circuit_open']


def test_publication_waits_only_for_current_file_and_run(isolated_store, monkeypatch):
    _, payload = seed(isolated_store)
    import automatic_release
    row = {'scan_id': SID, 'run_id': payload['run_id']}
    monkeypatch.setattr(automatic_release, 'require_authority', lambda *a: {})
    with pytest.raises(ValueError, match='bounded vision retry'):
        automatic_release.ready(isolated_store, row, FILE)
    assert not recovery.pending_for_file(isolated_store, SID, 'obsolete-run', FILE)
    assert not recovery.pending_for_file(isolated_store, SID, payload['run_id'], 'another.docx')
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, "UPDATE jobs SET status='done' WHERE type='vision_proposal_retry'")
    assert not recovery.pending_for_file(isolated_store, SID, payload['run_id'], FILE)


def test_pdf_recovery_never_transcribes_page_body_text_into_figure_alt(monkeypatch):
    import io
    import pikepdf
    import ai, remediate_pdf
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(300, 200))
    figure = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name('/StructElem'),
        S=pikepdf.Name('/Figure'), Pg=pdf.pages[0].obj, K=0))
    pdf.Root.StructTreeRoot = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name('/StructTreeRoot'), K=pikepdf.Array([figure])))
    stream = io.BytesIO()
    pdf.save(stream)
    data = stream.getvalue()
    monkeypatch.setattr(remediate_pdf, '_render_page_png', lambda *a: b'body text and a vector figure')
    monkeypatch.setattr(ai, 'describe_image_structured', lambda *a, **k: pytest.fail('page body text must not become figure alt'))
    proposals = remediate_pdf.alt_proposals_for_pdf(data, scan_id=SID, context_file='report.pdf')
    assert len(proposals) == 1 and proposals[0]['automatic_write_blocked'] is True
    assert not proposals[0]['proposed_value'] and not proposals[0].get('thumb')
    with pikepdf.open(io.BytesIO(data)) as original:
        assert '/Alt' not in original.Root.StructTreeRoot.K[0]


def test_office_failed_inference_consumes_attempt_budget(monkeypatch):
    import re
    import ai, remediate_office
    xml = '<wp:docPr id="1"/>'
    match = re.search(r'<wp:docPr\b([^>]*?)(/?)>', xml)
    calls = []
    monkeypatch.setattr(remediate_office, '_image_bytes_for', lambda *a: ('r1', b'image' * 1000))
    monkeypatch.setattr(ai, 'describe_image_structured', lambda *a, **k: calls.append('attempt') or None)
    budget = [1]
    for _ in range(2):
        assert remediate_office._vision_alt(xml, match, 'wp:docPr', '/', None, {},
            'word/document.xml', True, FILE, None, SID, budget) is None
    assert calls == ['attempt']
    assert budget == [0]


def test_pdf_multi_figure_page_never_receives_shared_page_caption(monkeypatch):
    import io
    import pikepdf
    import ai, remediate_pdf
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(300, 200))
    figures = []
    for index in range(2):
        figure = pikepdf.Dictionary(Type=pikepdf.Name('/StructElem'), S=pikepdf.Name('/Figure'),
                                   Pg=pdf.pages[0].obj, K=index)
        if index == 1:
            figure.Alt = pikepdf.String('Existing authored alt')
        figures.append(pdf.make_indirect(figure))
    pdf.Root.StructTreeRoot = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name('/StructTreeRoot'), K=pikepdf.Array(figures)))
    stream = io.BytesIO()
    pdf.save(stream)
    monkeypatch.setattr(ai, 'describe_image_structured', lambda *a, **k: pytest.fail('ambiguous inference'))
    proposals = remediate_pdf.alt_proposals_for_pdf(stream.getvalue(), scan_id=SID, context_file='report.pdf')
    assert len(proposals) == 1 and proposals[0]['automatic_write_blocked'] is True
    assert not proposals[0]['proposed_value'] and not proposals[0].get('thumb')


def test_pdf_pending_vision_is_blocked_without_queuing_page_caption(isolated_store):
    from types import SimpleNamespace
    context = SimpleNamespace(scan_id=SID, file='report.pdf', owner_id=OWNER,
        run_id='pdf-run', enabled=True, local_drafting=False, deferred=[])
    recovery.schedule(isolated_store, context, {'id':'parent'}, ['vision_timeout'])
    assert isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry') == []
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, 'SELECT action,detail FROM decision_log WHERE scan_id=%s', (SID,))
        row = isolated_store._db.fetchone(cur)
    assert row['action'] == 'vision.recovery.blocked'
    assert 'stored corrected copy and pending review' in json.loads(row['detail'])['reason']


def test_pending_and_recovered_activity_follow_durable_work(isolated_store, monkeypatch):
    _, payload = seed(isolated_store)
    events = [event for event in isolated_store.list_scan_events(SID)
              if event['kind'].startswith('remediate.vision_retry_')]
    assert [event['kind'] for event in events] == ['remediate.vision_retry_pending']
    assert events[0]['document'] == FILE
    assert events[0]['correlation_id'] == payload['run_id']
    assert events[0]['detail']['retry'] == 1
    assert len(isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')) == 1
    draft(monkeypatch)
    recovery.process(isolated_store, payload)
    events = [event for event in isolated_store.list_scan_events(SID)
              if event['kind'].startswith('remediate.vision_retry_')]
    assert events[-1]['kind'] == 'remediate.vision_retry_recovered'
    # A saved draft, not a document edit. 8 findings, 2 drafted locators: 6 still lack a draft.
    assert events[-1]['detail'] == {'drafts': 1, 'awaiting_review': 0, 'uncertain': 0, 'missing': 6,
                                    'coverage_complete': False, 'document_write': False,
                                    'item_id': payload['item_id']}
    assert isolated_store.get_hitl_item(payload['item_id'])['proposals'][0]['proposed_value']


def test_activity_detail_has_no_document_content_or_raw_error(isolated_store):
    recovery._decision(isolated_store, SID, 'sensitive-filename.docx', 'blocked',
        run_id='run-private', reason='body text secret-token sensitive-filename.docx',
        source_sha256='sensitive-hash', retry=2)
    event = isolated_store.list_scan_events(SID)[-1]
    assert event['document'] == 'sensitive-filename.docx'
    assert event['detail'] == {'retry': 2, 'reason_code': 'vision_recovery_unresolved'}
    assert event['correlation_id'] == 'run-private'


@pytest.mark.parametrize('reasons,blocked,available,expected', [
    ([{'reason': 'attempts_exhausted', 'kind': 'text'}], False, 100, 'vision_generated_output_unusable'),
    ([{'reason': 'attempts_exhausted'}, {'reason': 'provider_usage_unknown'}], False, 100, 'vision_spending_reconciliation_required'),
    ([{'reason': 'attempts_exhausted'}], True, 100, 'vision_spending_reconciliation_required'),
    ([{'reason': 'attempts_exhausted'}], False, 0, 'vision_budget_exhausted'),
    ([{'reason': 'vision_timeout'}], False, 100, None),
])
def test_settled_unusable_generation_is_not_a_spending_block(reasons, blocked, available, expected):
    from types import SimpleNamespace
    context = SimpleNamespace(deferred=reasons, enabled=True, owner_id=OWNER, run_id='run',
        ledger=SimpleNamespace(snapshot=lambda *_: {'blocked': blocked, 'available_units': available}))
    assert recovery._recovery_block(context) == expected


def test_unusable_generation_event_is_sanitized(isolated_store):
    seed(isolated_store)
    recovery._decision(isolated_store, SID, FILE, 'blocked',
        reason_code='vision_generated_output_unusable', reason='private provider output')
    events = isolated_store.list_scan_events(SID)
    assert events[-1]['detail'] == {'reason_code': 'vision_generated_output_unusable'}


def test_unusable_generation_stops_without_dispatching_another_retry(isolated_store):
    job, payload = seed(isolated_store)
    before = len(isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry'))
    with run_context(isolated_store, job['payload'], job) as context:
        context.deferred.append({'reason': 'attempts_exhausted', 'kind': 'text'})
        recovery.schedule(isolated_store, context, job, ['vision_timeout'])
    assert len(isolated_store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')) == before
    assert isolated_store.list_scan_events(SID)[-1]['detail'] == {
        'reason_code': 'vision_generated_output_unusable', 'item_id': payload['item_id']}


def test_local_endpoint_refusal_is_not_a_cloud_spending_block():
    from types import SimpleNamespace
    context = SimpleNamespace(deferred=[{'reason': 'local_endpoint_required'}, {'reason': 'ai_disabled_or_budget_zero'}], enabled=False, local_drafting=True)
    assert recovery._recovery_block(context) == 'vision_local_endpoint_required'


def test_unknown_local_failure_does_not_become_a_budget_block():
    from types import SimpleNamespace
    context = SimpleNamespace(deferred=[{'reason': 'local_draft_unavailable'}], enabled=False, local_drafting=True)
    assert recovery._recovery_block(context) == 'vision_recovery_unresolved'


def test_local_cloud_budget_refusal_does_not_block_private_vision_timeout():
    from types import SimpleNamespace
    context = SimpleNamespace(enabled=False, local_drafting=True,
        deferred=[{'reason': 'ai_disabled_or_budget_zero'}, {'reason': 'vision_timeout'}])
    assert recovery._recovery_block(context) is None


def test_exhausted_empty_image_response_retains_actual_failure():
    with recovery.capture() as misses:
        recovery.record('empty_response')
    assert misses == ['empty_response']


def test_local_empty_image_response_is_not_an_unknown_permission_block():
    from types import SimpleNamespace
    context = SimpleNamespace(enabled=False, local_drafting=True,
        deferred=[{'reason': 'ai_disabled_or_budget_zero'}])
    assert recovery._recovery_block(context, ['empty_response']) == 'vision_response_empty'


def test_private_recovery_survives_expected_cloud_budget_refusal(isolated_store, monkeypatch):
    _, payload = seed(isolated_store)
    draft(monkeypatch)
    import remediate_office
    original = remediate_office.alt_proposals_for_office
    def propose(*args, **kwargs):
        from ai_run_policy import optional_current_run_context
        optional_current_run_context().deferred.append({'reason': 'ai_disabled_or_budget_zero'})
        return original(*args, **kwargs)
    monkeypatch.setattr(remediate_office, 'alt_proposals_for_office', propose)
    recovery.process(isolated_store, payload)
    assert isolated_store.get_hitl_item(payload['item_id'])['proposals'][0]['proposed_value'] == 'A useful recovered caption'
    assert isolated_store.list_scan_events(SID)[-1]['kind'] == 'remediate.vision_retry_recovered'


def test_empty_local_recovery_preserves_review_and_emits_specific_safe_cause(isolated_store, monkeypatch):
    _, payload = seed(isolated_store)
    draft(monkeypatch)
    import remediate_office
    def propose(*args, **kwargs):
        recovery.record('empty_response')
        return [], []
    monkeypatch.setattr(remediate_office, 'alt_proposals_for_office', propose)
    recovery.process(isolated_store, payload)
    assert isolated_store.get_hitl_item(payload['item_id'])['proposals'] == json.loads(payload['proposals_before'])
    assert isolated_store.list_scan_events(SID)[-1]['detail'] == {'reason_code': 'vision_response_empty',
                                                                  'item_id': payload['item_id']}


def test_cloud_empty_output_does_not_buy_an_extra_recovery_attempt():
    from types import SimpleNamespace
    context = SimpleNamespace(enabled=True, local_drafting=False, deferred=[], owner_id=OWNER, run_id='run',
        ledger=SimpleNamespace(snapshot=lambda *_: {'blocked': False, 'available_units': 100}))
    assert recovery._recovery_block(context, ['empty_response']) == 'vision_generated_output_unusable'


def test_successful_paid_recovery_can_save_draft_after_spending_last_available_units():
    from types import SimpleNamespace
    context = SimpleNamespace(enabled=True, local_drafting=False, deferred=[], owner_id=OWNER, run_id='run',
        ledger=SimpleNamespace(snapshot=lambda *_: {'blocked': False, 'available_units': 0}))
    assert recovery._recovery_block(context) == 'vision_budget_exhausted'
    assert recovery._recovery_block(context, [], check_admission=False) is None
    context.ledger.snapshot = lambda *_: {'blocked': True, 'available_units': 0}
    assert recovery._recovery_block(context, [], check_admission=False) == 'vision_spending_reconciliation_required'
