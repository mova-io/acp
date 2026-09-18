"""Vision retries spend AI only on targets a regeneration can fix, and stale retries say so.

THE SEQUENCE (production, sanitized; reproduced synthetically here). A Word file's single 1.1.1
image got a vision draft on retry 1. The draft was valid text whose only blocker was a human
meaning check: quality_source_review.status 'needs_manual', caption_validation
['pixel_semantics_unsupported'], automatic_write_blocked, reason_code
'quality_source_meaning_unverified'. `usable_draft()` is False for that shape, so process()
queued retry 2 for a draft that already existed. A human then approved a 1.4.5 replacement of
the same picture; the corrected sha changed; retry 2 ran, `_validate` refused it, and the refusal
was reported as a generic `vision_recovery_unresolved` block ("AI generation paused") although it
was an obsolete retry that never made an AI request.

Every test stubs the generator and the blob read and COUNTS both.
"""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json

import pytest

import vision_recovery as recovery

OWNER, SID, FILE = 'draft-states@example.test', 'draft-states-scan', 'synthetic.docx'
DATA = b'synthetic corrected artifact for draft states'
DIGEST = sha256(DATA).hexdigest()
R1, R2 = 'word/document.xml#r1', 'word/document.xml#r2'


def human_only(locator=R1, text='A synthetic line drawing of a bridge over water', **extra):
    """The retained production shape: valid text, only a human meaning gate outstanding."""
    return {'locator': locator, 'before': '(no alt text)', 'proposed_value': text,
            'model': 'fixture-vision', 'grounded': False, 'approval_required': True,
            'automatic_write_blocked': True, 'review_status': 'needs_review',
            'reason_code': 'quality_source_meaning_unverified',
            'quality_source_review': {'version': 'quality-source.v1', 'status': 'needs_manual',
                'passed': False, 'validation': {'approved': False, 'status': 'needs_manual',
                    'reason_codes': ['pixel_semantics_unsupported'], 'evidence': {'method': 'unsupported'}},
                'cloud_review': {'verdict': 'unable', 'reason': 'independent_source_reviewer_unavailable'}},
            'caption_validation': {'approved': False, 'status': 'needs_manual',
                'reason_codes': ['pixel_semantics_unsupported'], 'evidence': {'method': 'unsupported'}},
            **extra}


def missing(locator):
    return {'locator': locator, 'before': '(no alt text)', 'proposed_value': ''}


class Harness:
    def __init__(self, store, monkeypatch, *, quality=True):
        import blob, remediate_office, ai_standing_approval, ai_run_policy
        self.store = store
        self.ai_calls = []
        self.blob_reads = []
        self.generated = []

        def download(*args):
            self.blob_reads.append(args)
            return DATA

        def generate(data, ext, **kwargs):
            self.ai_calls.append(kwargs)
            return [dict(p) for p in self.generated], []

        monkeypatch.setattr(blob, 'download_remediated', download)
        monkeypatch.setattr(remediate_office, 'alt_proposals_for_office', generate)
        monkeypatch.setattr(ai_standing_approval, 'approve_file', lambda *a: None)
        if quality:
            original = ai_run_policy.run_context

            @contextmanager
            def quality_context(*args):
                with original(*args) as context:
                    yield replace(context, policy={**context.policy,
                                                   'quality_first': True, 'auto_approve_ai': True})
            monkeypatch.setattr(ai_run_policy, 'run_context', quality_context)

    def seed(self, proposals, finding_count):
        store = self.store
        with store._db.cursor() as cur:
            store._db.execute(cur, "INSERT INTO scan_runs(id,owner_email,status,source) VALUES(%s,%s,'done','local')", (SID, OWNER))
            store._db.execute(cur, "INSERT INTO file_records(scan_id,file,corrected_sha256,remediated_at) VALUES(%s,%s,%s,'2026-09-13')", (SID, FILE, DIGEST))
        batch = store.enqueue_stage_batch(SID, 'remediate', 'remediate_file', [{
            'scan_id': SID, 'file': FILE, 'owner': OWNER,
            'remediation_impact_policy': {'ai': 1, 'ai_zone': 'local', 'ai_budget_usd': '0.00'}}],
            snapshot_id=store.remediation_source_revision(SID), request_fingerprint='draft-states')
        self.job = store.get_job(batch['job_ids'][0])
        self.item_id = store.enqueue_proposals(SID, FILE, '1.1.1', proposals, finding_count=finding_count)
        return self.job

    def schedule(self, misses=('vision_timeout',), inspect_pending=False):
        from ai_run_policy import run_context
        with run_context(self.store, self.job['payload'], self.job) as context:
            recovery.schedule(self.store, context, self.job, list(misses), inspect_pending=inspect_pending)
        return self.retries()

    def retries(self):
        return [json.loads(r['payload']) if isinstance(r['payload'], str) else r['payload']
                for r in self.store.list_scan_jobs_of_type(SID, 'vision_proposal_retry')]

    def row(self):
        with self.store._db.cursor() as cur:
            self.store._db.execute(cur, 'SELECT * FROM hitl_queue WHERE id=%s', (self.item_id,))
            return dict(self.store._db.fetchone(cur))

    def events(self):
        return [e for e in self.store.list_scan_events(SID) if e['kind'].startswith('remediate.vision_retry_')]

    def decisions(self, action):
        with self.store._db.cursor() as cur:
            self.store._db.execute(cur, 'SELECT detail FROM decision_log WHERE scan_id=%s AND action=%s ORDER BY id', (SID, action))
            return [json.loads(r['detail']) for r in self.store._db.fetchall(cur)]


@pytest.fixture
def h(isolated_store, monkeypatch):
    return Harness(isolated_store, monkeypatch)


def test_production_sequence_human_only_draft_does_not_buy_retry_2(h):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    assert payload['retry'] == 1
    h.generated = [human_only()]
    recovery.process(h.store, payload)
    assert len(h.ai_calls) == 1
    assert h.row()['status'] == 'pending'
    assert json.loads(h.row()['proposals'])[0]['proposed_value'] == human_only()['proposed_value']
    # The draft exists and only needs a human; a second paid attempt cannot confirm meaning.
    assert [p['retry'] for p in h.retries()] == [1]
    assert h.events()[-1]['kind'] == 'remediate.vision_retry_recovered'


def test_production_step_4_stale_retry_is_obsolete_not_a_paused_generation(h):
    """Retry 2 after an approved replacement changed the corrected sha: no read, no AI, no write."""
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    stale = dict(payload, retry=2)
    with h.store._db.cursor() as cur:
        h.store._db.execute(cur, 'UPDATE file_records SET corrected_sha256=%s WHERE scan_id=%s AND file=%s',
                            (sha256(b'after an approved 1.4.5 replacement').hexdigest(), SID, FILE))
    before = h.row()
    recovery.process(h.store, stale)
    assert (h.ai_calls, h.blob_reads) == ([], [])
    assert h.row() == before
    event = h.events()[-1]
    assert event['kind'] == 'remediate.vision_retry_obsolete'
    assert event['detail'] == {'retry': 2, 'reason_code': 'vision_retry_input_changed',
                               'no_ai_request': True, 'item_id': h.item_id}
    assert not [e for e in h.events() if e['kind'] == 'remediate.vision_retry_blocked']


# ── draft_state ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('proposal,state', [
    (missing(R1), 'missing'),
    ({'locator': R1, 'proposed_value': '   '}, 'missing'),
    ({'locator': R1, 'proposed_value': None}, 'missing'),
    ({'locator': R1, 'proposed_value': 'Template text', 'is_template': True}, 'missing'),
    ({'locator': R1, 'proposed_value': 'A usable authored caption'}, 'usable'),
    (human_only(), 'awaiting_human'),
    (human_only(quality_source_review={'status': 'needs_manual', 'cloud_review': {'verdict': 'accept'}}), 'awaiting_human'),
    ({'locator': R1, 'proposed_value': 'A bar chart of sales', 'automatic_write_blocked': True,
      'review_status': 'needs_review', 'reason_code': 'chart_relationships_unverified'}, 'awaiting_human'),
    ({'locator': R1, 'proposed_value': 'A caption', 'review_status': 'needs_review'}, 'awaiting_human'),
    # Unknown blocker: conservative, never spend AI on a gate nobody here can name.
    ({'locator': R1, 'proposed_value': 'A caption', 'automatic_write_blocked': True,
      'reason_code': 'some_future_gate'}, 'awaiting_human'),
    ({'locator': R1, 'proposed_value': 'A caption', 'automatic_write_blocked': True}, 'awaiting_human'),
    ({'locator': R1, 'proposed_value': 'A chart showing values …', 'automatic_write_blocked': True,
      'reason_code': 'incomplete_description'}, 'repairable'),
    ({'locator': R1, 'proposed_value': "I'm sorry, I can't describe this image.", 'automatic_write_blocked': True,
      'reason_code': 'provider_refusal'}, 'repairable'),
    # Quality-first overwrites a refusal's reason_code; the text itself still says what it is.
    (human_only(text="I'm sorry, but I cannot describe this image."), 'repairable'),
    ({'locator': R1, 'proposed_value': 'The chart has no numerical values', 'automatic_write_blocked': True,
      'reason_code': 'ocr_numeric_values_denied'}, 'repairable'),
    (human_only(caption_validation={'status': 'rejected', 'reason_codes': ['pixel_color_contradiction']}), 'repairable'),
    (human_only(quality_source_review={'status': 'rejected', 'validation': {
        'status': 'rejected', 'reason_codes': ['pixel_shape_or_color_contradiction']}}), 'repairable'),
    (human_only(quality_source_review={'status': 'unsupported', 'checks': [{'validation': {
        'status': 'rejected', 'reason_codes': ['visible_numbers_denied']}}]}), 'repairable'),
    (human_only(quality_source_review={'status': 'needs_manual', 'cloud_review': {'verdict': 'revise'}}), 'repairable'),
])
def test_draft_state_is_derived_from_real_fields(proposal, state):
    assert recovery.draft_state(proposal) == state


def test_usable_draft_semantics_are_unchanged():
    assert recovery.usable_draft({'proposed_value': 'Caption'}) is True
    assert not recovery.usable_draft(human_only())
    assert not recovery.usable_draft({'proposed_value': 'Caption', 'review_status': 'needs_review'})
    assert not recovery.usable_draft({'proposed_value': 'Caption', 'is_template': True})


# ── scheduling and regeneration ───────────────────────────────────────────────────────────────

def test_schedule_skips_a_row_whose_only_draft_awaits_a_human(h):
    h.seed([human_only()], 1)
    assert h.schedule(misses=(), inspect_pending=True) == []
    assert h.ai_calls == []


def test_missing_target_is_scheduled(h):
    h.seed([human_only(R1), missing(R2)], 2)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    assert payload['retry'] == 1


def test_uncovered_finding_is_scheduled(h):
    h.seed([human_only(R1)], 2)
    assert len(h.schedule(misses=(), inspect_pending=True)) == 1


def test_repairable_draft_is_regenerated_with_guidance_for_that_target_only(h):
    refusal = {'locator': R2, 'before': '(no alt text)', 'proposed_value': 'Refusal-shaped synthetic draft',
               'automatic_write_blocked': True, 'reason_code': 'provider_refusal',
               'quality_source_review': {'status': 'needs_manual', 'cloud_review': {'verdict': 'unable',
                                         'reason': 'repairable-note-marker'}}}
    human = human_only(R1, quality_source_review={'status': 'needs_manual', 'cloud_review': {
        'verdict': 'unable', 'reason': 'human-note-marker'}})
    h.seed([human, refusal], 2)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    h.generated = [{'locator': R2, 'proposed_value': 'A regenerated synthetic caption'}]
    recovery.process(h.store, payload)
    assert len(h.ai_calls) == 1
    call = h.ai_calls[0]
    assert call['skip_locators'] == {R1}
    assert 'repairable-note-marker' in call['guidance'] and R2 in call['guidance']
    assert 'human-note-marker' not in call['guidance'] and R1 not in call['guidance']
    proposals = json.loads(h.row()['proposals'])
    assert proposals[0] == human
    assert proposals[1]['proposed_value'] == 'A regenerated synthetic caption'
    assert h.events()[-1]['detail'] == {'drafts': 1, 'awaiting_review': 1, 'uncertain': 0, 'missing': 0,
                                        'coverage_complete': True, 'document_write': False,
                                        'item_id': h.item_id}
    assert [p['retry'] for p in h.retries()] == [1]


def test_unknown_blocker_stops_retry_but_is_counted_uncertain_not_awaiting_review(h):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    h.generated = [{'locator': R1, 'proposed_value': 'A synthetic caption', 'automatic_write_blocked': True,
                    'reason_code': 'some_future_gate'}]
    recovery.process(h.store, payload)
    assert len(h.ai_calls) == 1
    assert [p['retry'] for p in h.retries()] == [1]
    assert h.events()[-1]['detail'] == {'drafts': 1, 'awaiting_review': 0, 'uncertain': 1, 'missing': 0,
                                        'coverage_complete': True, 'document_write': False,
                                        'item_id': h.item_id}


@pytest.mark.parametrize('proposal,known', [
    (human_only(), True),
    ({'proposed_value': 'A bar chart', 'automatic_write_blocked': True, 'review_status': 'needs_review',
      'reason_code': 'chart_relationships_unverified'}, True),
    ({'proposed_value': 'A caption', 'automatic_write_blocked': True, 'requires_semantic_review': True,
      'caption_validation': {'status': 'needs_manual', 'reason_codes': ['pixel_semantics_unsupported']}}, True),
    ({'proposed_value': 'A caption', 'automatic_write_blocked': True}, False),
    ({'proposed_value': 'A caption', 'automatic_write_blocked': True, 'review_status': 'needs_review',
      'reason_code': 'some_future_gate'}, False),
    ({'proposed_value': 'A caption', 'automatic_write_blocked': True, 'requires_semantic_review': True,
      'caption_validation': {'status': 'needs_manual', 'reason_codes': ['figure_image_changed']}}, False),
    (human_only(quality_source_review={'status': 'source_changed'}), False),
    ({'proposed_value': 'A usable caption'}, False),
    (missing(R1), False),
])
def test_human_gate_known_names_only_recognised_gates(proposal, known):
    assert recovery.human_gate_known(proposal) is known


def test_repairable_draft_left_after_retry_1_still_gets_retry_2(h):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    h.generated = [{'locator': R1, 'proposed_value': 'A synthetic chart of values …',
                    'automatic_write_blocked': True, 'reason_code': 'incomplete_description'}]
    recovery.process(h.store, payload)
    assert sorted(p['retry'] for p in h.retries()) == [1, 2]
    recovered = [e for e in h.events() if e['kind'] == 'remediate.vision_retry_recovered']
    assert recovered[-1]['detail']['missing'] == 1


def test_mixed_row_regenerates_only_the_missing_target(h):
    human = human_only(R1)
    h.seed([human, missing(R2)], 2)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    # Even if a generator returned a replacement for the human-awaiting target, it is not used.
    h.generated = [{'locator': R1, 'proposed_value': 'Unwanted replacement'},
                   {'locator': R2, 'proposed_value': 'A synthetic caption for the second image'}]
    recovery.process(h.store, payload)
    assert len(h.ai_calls) == 1 and h.ai_calls[0]['skip_locators'] == {R1}
    proposals = json.loads(h.row()['proposals'])
    assert [p['locator'] for p in proposals] == [R1, R2]
    assert proposals[0] == human
    assert proposals[1]['proposed_value'] == 'A synthetic caption for the second image'
    assert [p['retry'] for p in h.retries()] == [1]
    assert h.events()[-1]['detail'] == {'drafts': 2, 'awaiting_review': 1, 'uncertain': 0, 'missing': 0,
                                        'coverage_complete': True, 'document_write': False,
                                        'item_id': h.item_id}


def test_queued_retry_with_only_human_gated_drafts_is_review_only_obsolete(h):
    h.seed([human_only(R1), human_only(R2)], 2)
    [payload] = h.schedule(misses=('vision_timeout',))
    assert h.events()[-1]['detail']['item_id'] == h.item_id      # pending binds to its row too
    before = h.row()
    recovery.process(h.store, payload)
    assert (h.ai_calls, h.blob_reads) == ([], [])
    assert h.row() == before
    assert h.events()[-1]['kind'] == 'remediate.vision_retry_obsolete'
    counts = {'usable': 0, 'awaiting_review': 2, 'uncertain': 0}
    assert h.events()[-1]['detail'] == {'retry': 1, 'reason_code': 'vision_retry_review_only',
                                        'no_ai_request': True, 'item_id': h.item_id, **counts}
    assert h.decisions('vision.recovery.obsolete') == [{'run_id': payload['run_id'], 'retry': 1,
        'item_id': h.item_id, 'reason_code': 'vision_retry_review_only', 'no_ai_request': True, **counts}]


# ── obsolete retries ──────────────────────────────────────────────────────────────────────────

def _assert_obsolete(h, payload, code, before):
    assert (h.ai_calls, h.blob_reads) == ([], [])
    assert h.row() == before
    events = h.events()
    assert events[-1]['kind'] == 'remediate.vision_retry_obsolete'
    assert events[-1]['detail'] == {'retry': payload['retry'], 'reason_code': code, 'no_ai_request': True,
                                    'item_id': h.item_id}
    assert events[-1]['correlation_id'] == payload['run_id'] and events[-1]['document'] == FILE
    assert not [e for e in events if e['kind'] in ('remediate.vision_retry_blocked',
                                                    'remediate.vision_retry_recovered')]
    assert not h.decisions('vision.recovery.blocked') and not h.decisions('vision.recovery.recovered')
    assert h.decisions('vision.recovery.obsolete')[-1]['reason_code'] == code


def _record_target_removal(h, corrected_sha):
    """A faithful copy of what review_target_reconciliation._commit records, read back through
    the real current_removals (so every binding it checks is exercised, not stubbed)."""
    import review_target_reconciliation as rtr
    row = h.store.get_hitl_item(h.item_id)
    detail = {'item_id': str(row['id']), 'file': FILE, 'rule_id': '1.1.1', 'criterion': '1.1.1',
              'decision_version': int(row.get('decision_version') or 0),
              'proposal_snapshot_ids': rtr._snapshots(row), 'targets': rtr._targets(row),
              'removed_by_item_id': 'synthetic-145-row', 'removed_by_rule_id': '1.4.5',
              'finding_ids': ['synthetic-finding'], 'batch_id': rtr.current_batch(h.store, SID),
              'corrected_artifact_sha256': corrected_sha, 'assessment': rtr.SAVED_EVIDENCE,
              'assessment_status': 'analysed', 'skipped_rules': 0,
              'assessment_scope': rtr._scope(h.store, SID, FILE)[1]}
    h.store.log_decision('system', rtr.ACTION, scan_id=SID, file=FILE, rule_id='1.1.1',
                         detail=json.dumps(detail, sort_keys=True))


def _replace_artifact(h):
    new = sha256(b'after an approved 1.4.5 replacement').hexdigest()
    with h.store._db.cursor() as cur:
        h.store._db.execute(cur, 'UPDATE file_records SET corrected_sha256=%s WHERE scan_id=%s AND file=%s',
                            (new, SID, FILE))
    return new


def test_retired_row_is_target_replaced(h):
    import review_target_reconciliation as rtr
    h.seed([human_only(R1), missing(R2)], 2)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    _record_target_removal(h, _replace_artifact(h))
    assert rtr.removed_item_ids(h.store, SID, FILE) == {h.item_id}
    before = h.row()
    recovery.process(h.store, payload)
    _assert_obsolete(h, payload, 'vision_retry_target_replaced', before)


def test_retired_row_at_the_same_sha_is_still_never_regenerated(h):
    """A retry queued AFTER the removal carries the new sha: only the removal evidence stops it."""
    h.seed([human_only(R1), missing(R2)], 2)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    _record_target_removal(h, DIGEST)
    before = h.row()
    recovery.process(h.store, payload)
    _assert_obsolete(h, payload, 'vision_retry_target_replaced', before)


def test_lapsed_removal_evidence_is_input_changed_not_target_replaced(h):
    h.seed([human_only(R1), missing(R2)], 2)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    _record_target_removal(h, sha256(b'an older corrected copy').hexdigest())
    _replace_artifact(h)
    before = h.row()
    recovery.process(h.store, payload)
    _assert_obsolete(h, payload, 'vision_retry_input_changed', before)


@pytest.mark.parametrize('mutation', ['revision', 'parent'])
def test_changed_input_is_obsolete(h, mutation, monkeypatch):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    if mutation == 'revision':
        monkeypatch.setattr(h.store, 'remediation_source_revision', lambda sid: 'another-revision')
    else:
        payload = dict(payload, parent_job_id='another-parent-job')
    before = h.row()
    recovery.process(h.store, payload)
    _assert_obsolete(h, payload, 'vision_retry_input_changed', before)


@pytest.mark.parametrize('mutation', ['approved', 'rejected', 'proposals', 'deleted'])
def test_review_decided_or_changed_is_obsolete(h, mutation):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    with h.store._db.cursor() as cur:
        if mutation in ('approved', 'rejected'):
            h.store._db.execute(cur, 'UPDATE hitl_queue SET status=%s WHERE id=%s', (mutation, h.item_id))
        elif mutation == 'proposals':
            h.store._db.execute(cur, 'UPDATE hitl_queue SET proposals=%s WHERE id=%s',
                                (json.dumps([{'locator': R1, 'proposed_value': 'A human typed caption'}]), h.item_id))
        else:
            h.store._db.execute(cur, 'DELETE FROM hitl_queue WHERE id=%s', (h.item_id,))
    if mutation == 'deleted':
        h.row = lambda: None
    before = h.row()
    recovery.process(h.store, payload)
    _assert_obsolete(h, payload, 'vision_retry_review_changed', before)


@pytest.mark.parametrize('column,value', [('cancel_requested_at', '2026-09-17T00:00:00Z'),
                                          ('is_current', 0), ('state', 'cancelled'), ('state', 'failed')])
def test_inactive_run_is_obsolete(h, column, value):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    with h.store._db.cursor() as cur:
        h.store._db.execute(cur, f'UPDATE stage_executions SET {column}=%s WHERE execution_id=%s',
                            (value, payload['run_id']))
    before = h.row()
    recovery.process(h.store, payload)
    _assert_obsolete(h, payload, 'vision_retry_run_inactive', before)


# ── genuine failures stay blocked ─────────────────────────────────────────────────────────────

def _assert_blocked(h, code):
    events = h.events()
    assert events[-1]['kind'] == 'remediate.vision_retry_blocked'
    assert events[-1]['detail']['reason_code'] == code
    assert not [e for e in events if e['kind'] == 'remediate.vision_retry_obsolete']
    assert not h.decisions('vision.recovery.obsolete')


def test_owner_access_denial_stays_blocked(h, monkeypatch):
    import ai_standing_approval
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    def deny(*a):
        raise ValueError('Review permission is required for automatic AI approval')
    monkeypatch.setattr(ai_standing_approval, 'require_access', deny)
    recovery.process(h.store, payload)
    assert (h.ai_calls, h.blob_reads) == ([], [])
    _assert_blocked(h, 'vision_recovery_unresolved')


def test_ai_disabled_stays_blocked(h, monkeypatch):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    monkeypatch.setattr(h.store, 'get_ai_enabled', lambda: False)
    recovery.process(h.store, payload)
    assert h.ai_calls == []
    _assert_blocked(h, 'vision_recovery_unresolved')


def test_execution_this_owner_cannot_read_stays_blocked(h):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    recovery.process(h.store, dict(payload, owner='someone-else@example.test'))
    assert h.ai_calls == []
    _assert_blocked(h, 'vision_recovery_unresolved')


@pytest.mark.parametrize('code', ['vision_budget_exhausted', 'vision_spending_reconciliation_required',
                                  'vision_run_permission_unavailable', 'vision_provider_access_denied',
                                  'vision_budget_admission_denied'])
def test_budget_run_and_provider_denials_stay_blocked(h, monkeypatch, code):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    monkeypatch.setattr(recovery, '_recovery_block', lambda context, misses=(), check_admission=True: code)
    recovery.process(h.store, payload)
    assert h.ai_calls == []
    _assert_blocked(h, code)


def test_denial_after_generation_stays_blocked(h, monkeypatch):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    h.generated = [{'locator': R1, 'proposed_value': 'A synthetic caption'}]
    monkeypatch.setattr(recovery, '_recovery_block', lambda context, misses=(), check_admission=True:
                        None if check_admission else 'vision_spending_reconciliation_required')
    before = h.row()
    recovery.process(h.store, payload)
    assert len(h.ai_calls) == 1 and h.row() == before
    _assert_blocked(h, 'vision_spending_reconciliation_required')


def _assert_obsolete_after_dispatch(h, payload, code):
    """Generation ran, so whether a provider request went out is unknown: no claim either way."""
    events = h.events()
    assert events[-1]['kind'] == 'remediate.vision_retry_obsolete'
    assert events[-1]['detail'] == {'retry': payload['retry'], 'reason_code': code, 'item_id': h.item_id}
    assert 'no_ai_request' not in h.decisions('vision.recovery.obsolete')[-1]
    assert not [e for e in events if e['kind'] in ('remediate.vision_retry_blocked',
                                                    'remediate.vision_retry_recovered')]


def test_race_row_changes_before_dispatch_claims_no_request(h, monkeypatch):
    """Changed between queueing and process start: detected before any read or request."""
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    typed = json.dumps([{'locator': R1, 'proposed_value': 'A human typed caption'}])
    with h.store._db.cursor() as cur:
        h.store._db.execute(cur, 'UPDATE hitl_queue SET proposals=%s WHERE id=%s', (typed, h.item_id))
    recovery.process(h.store, payload)
    assert (h.ai_calls, h.blob_reads) == ([], [])
    assert h.row()['proposals'] == typed
    assert h.events()[-1]['detail']['no_ai_request'] is True


@pytest.mark.parametrize('transient', [False, True])
def test_race_row_changes_after_generation_is_never_overwritten(h, monkeypatch, transient):
    """Changed while the generator ran: detected at the CAS (or the retry-2 re-validation)."""
    import remediate_office
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    typed = json.dumps([{'locator': R1, 'proposed_value': 'A human typed caption'}])

    def generate(data, ext, **kwargs):
        h.ai_calls.append(kwargs)
        with h.store._db.cursor() as cur:
            h.store._db.execute(cur, 'UPDATE hitl_queue SET proposals=%s WHERE id=%s', (typed, h.item_id))
        if transient:
            recovery.record('circuit_open')
            return [], []
        return [{'locator': R1, 'proposed_value': 'A generated caption'}], []
    monkeypatch.setattr(remediate_office, 'alt_proposals_for_office', generate)
    recovery.process(h.store, payload)
    assert len(h.ai_calls) == 1
    assert h.row()['proposals'] == typed
    assert [p['retry'] for p in h.retries()] == [1]
    _assert_obsolete_after_dispatch(h, payload, 'vision_retry_review_changed')


def test_retry_limit_is_enforced_before_any_read(h):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    recovery.process(h.store, dict(payload, retry=3))
    assert (h.ai_calls, h.blob_reads) == ([], [])
    _assert_blocked(h, 'vision_recovery_unresolved')


def test_retry_2_never_schedules_a_third(h):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    h.generated = [{'locator': R1, 'proposed_value': 'A synthetic chart of values …',
                    'automatic_write_blocked': True, 'reason_code': 'incomplete_description'}]
    recovery.process(h.store, dict(payload, retry=2))
    assert len(h.ai_calls) == 1
    assert [p['retry'] for p in h.retries()] == [1]


def test_transient_miss_on_retry_1_still_queues_retry_2(h, monkeypatch):
    import remediate_office
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)

    def unavailable(data, ext, **kwargs):
        h.ai_calls.append(kwargs)
        recovery.record('circuit_open')
        return [], []
    monkeypatch.setattr(remediate_office, 'alt_proposals_for_office', unavailable)
    recovery.process(h.store, payload)
    assert sorted(p['retry'] for p in h.retries()) == [1, 2]


# ── event projection ──────────────────────────────────────────────────────────────────────────

def test_obsolete_and_recovered_projection_passes_only_safe_keys(isolated_store, monkeypatch):
    Harness(isolated_store, monkeypatch)
    recovery._decision(isolated_store, SID, 'private-name.docx', 'obsolete', run_id='run-x', retry=2,
                       reason_code='vision_retry_input_changed', no_ai_request=True,
                       reason='raw text secret', proposals_before='[{"proposed_value":"secret"}]')
    recovery._decision(isolated_store, SID, 'private-name.docx', 'obsolete', run_id='run-x', retry=1,
                       reason_code='not-a-known-code', no_ai_request='yes', item_id=7)
    recovery._decision(isolated_store, SID, 'private-name.docx', 'recovered', run_id='run-x', drafts=1,
                       awaiting_review=1, uncertain=0, missing=0, document_write=False, reason='secret',
                       proposals='secret', item_id='row-1', no_ai_request=True, drafts_text='secret')
    recovery._decision(isolated_store, SID, 'private-name.docx', 'blocked', run_id='run-x',
                       reason_code='vision_budget_exhausted', item_id='row-1', no_ai_request=True)
    events = isolated_store.list_scan_events(SID)[-4:]
    assert [e['detail'] for e in events] == [
        {'retry': 2, 'reason_code': 'vision_retry_input_changed', 'no_ai_request': True},
        {'retry': 1},
        {'drafts': 1, 'awaiting_review': 1, 'uncertain': 0, 'missing': 0, 'document_write': False,
         'item_id': 'row-1'},
        {'reason_code': 'vision_budget_exhausted', 'item_id': 'row-1'}]


def test_projection_keeps_unknown_missing_and_coverage_types(isolated_store, monkeypatch):
    Harness(isolated_store, monkeypatch)
    recovery._decision(isolated_store, SID, FILE, 'recovered', run_id='run-x', drafts=1, awaiting_review=0,
                       uncertain=0, missing=None, coverage_complete=False, document_write=False)
    recovery._decision(isolated_store, SID, FILE, 'recovered', run_id='run-x', drafts=1,
                       missing='0', coverage_complete='yes', usable='2')
    events = isolated_store.list_scan_events(SID)[-2:]
    assert events[0]['detail'] == {'drafts': 1, 'awaiting_review': 0, 'uncertain': 0, 'missing': None,
                                   'coverage_complete': False, 'document_write': False}
    assert events[1]['detail'] == {'drafts': 1}


# ── phase 3: nothing to generate is not always "needs human review" ───────────────────────────

def usable(locator):
    return {'locator': locator, 'before': '(no alt text)', 'proposed_value': 'A usable authored caption'}


def unknown_blocker(locator):
    return {'locator': locator, 'before': '(no alt text)', 'proposed_value': 'A synthetic caption',
            'automatic_write_blocked': True, 'reason_code': 'some_future_gate'}


def test_production_metadata_exactly_stops_retry_2_and_keeps_the_human_gate(h):
    """Fresh production metadata: cloud verdict 'unable', source review 'needs_manual',
    automatic_write_blocked, reason quality_source_meaning_unverified (nothing else assumed)."""
    draft = {'locator': R1, 'before': '(no alt text)', 'proposed_value': 'A synthetic description',
             'automatic_write_blocked': True, 'reason_code': 'quality_source_meaning_unverified',
             'quality_source_review': {'status': 'needs_manual', 'cloud_review': {'verdict': 'unable'}}}
    assert recovery.draft_state(draft) == 'awaiting_human' and recovery.human_gate_known(draft)
    assert not recovery.usable_draft(draft)                    # the semantic guard stays
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    h.generated = [draft]
    recovery.process(h.store, payload)
    assert len(h.ai_calls) == 1
    assert [p['retry'] for p in h.retries()] == [1]
    assert json.loads(h.row()['proposals'])[0]['automatic_write_blocked'] is True
    assert h.events()[-1]['detail'] == {'drafts': 1, 'awaiting_review': 1, 'uncertain': 0, 'missing': 0,
        'coverage_complete': True, 'document_write': False, 'item_id': h.item_id}


@pytest.mark.parametrize('proposals,code,counts', [
    ([usable(R1), usable(R2)], 'vision_retry_not_needed', {'usable': 2, 'awaiting_review': 0, 'uncertain': 0}),
    ([unknown_blocker(R1)], 'vision_retry_not_needed', {'usable': 0, 'awaiting_review': 0, 'uncertain': 1}),
    ([human_only(R1), usable(R2)], 'vision_retry_not_needed', {'usable': 1, 'awaiting_review': 1, 'uncertain': 0}),
    ([human_only(R1), unknown_blocker(R2)], 'vision_retry_not_needed', {'usable': 0, 'awaiting_review': 1, 'uncertain': 1}),
    ([human_only(R1), human_only(R2)], 'vision_retry_review_only', {'usable': 0, 'awaiting_review': 2, 'uncertain': 0}),
])
def test_nothing_to_generate_names_only_what_is_proven(h, proposals, code, counts):
    h.seed(proposals, len(proposals))
    [payload] = h.schedule(misses=('vision_timeout',))
    before = h.row()
    recovery.process(h.store, payload)
    assert (h.ai_calls, h.blob_reads) == ([], [])
    assert h.row() == before
    assert h.events()[-1]['kind'] == 'remediate.vision_retry_obsolete'
    assert h.events()[-1]['detail'] == {'retry': 1, 'reason_code': code, 'no_ai_request': True,
                                        'item_id': h.item_id, **counts}


def _unknown_count(h):
    with h.store._db.cursor() as cur:
        h.store._db.execute(cur, 'UPDATE hitl_queue SET finding_count=NULL WHERE id=%s', (h.item_id,))


def test_unknown_total_never_reads_as_complete_coverage(h):
    h.seed([missing(R1)], 1)
    _unknown_count(h)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    h.generated = [human_only(R1)]
    recovery.process(h.store, payload)
    assert len(h.ai_calls) == 1
    assert h.events()[-1]['detail'] == {'drafts': 1, 'awaiting_review': 1, 'uncertain': 0, 'missing': None,
        'coverage_complete': False, 'document_write': False, 'item_id': h.item_id}
    # Unknown total: no uncovered target can be proven, so no extra attempt is bought for it.
    assert [p['retry'] for p in h.retries()] == [1]


def test_unknown_total_with_settled_drafts_still_generates_as_before(h):
    """Coverage is unprovable, so a queued retry is not declared unnecessary."""
    h.seed([human_only(R1)], 1)
    _unknown_count(h)
    [payload] = h.schedule(misses=('vision_timeout',))
    h.generated = [{'locator': R2, 'proposed_value': 'A caption for an image the row did not list'}]
    recovery.process(h.store, payload)
    assert len(h.ai_calls) == 1 and h.ai_calls[0]['skip_locators'] == {R1}
    assert h.events()[-1]['detail']['missing'] is None
    assert h.events()[-1]['detail']['coverage_complete'] is False


def test_target_absent_from_proposals_gets_retry_2(h):
    """3 findings: one awaiting a human, one blank, one with no proposal at all."""
    h.seed([human_only(R1), missing(R2)], 3)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    h.generated = [{'locator': R2, 'proposed_value': 'A synthetic caption for the second image'}]
    recovery.process(h.store, payload)
    assert h.events()[-2]['kind'] == 'remediate.vision_retry_recovered'
    assert h.events()[-2]['detail']['missing'] == 1
    assert h.events()[-2]['detail']['coverage_complete'] is False
    retries = sorted(h.retries(), key=lambda p: p['retry'])
    assert [p['retry'] for p in retries] == [1, 2]
    # Retry 2 is the same row and allowance; no target is invented for it.
    assert {k: v for k, v in retries[1].items() if k not in ('retry', 'proposals_before')} == \
           {k: v for k, v in payload.items() if k not in ('retry', 'proposals_before')}
    assert len(json.loads(retries[1]['proposals_before'])) == 2


def test_two_findings_one_human_one_absent_gets_retry_2(h):
    """finding_count 2: one awaiting_human proposal, the other target absent from the row.
    The generator's only output is ambiguous (duplicate locator), so nothing is added."""
    h.seed([human_only(R1)], 2)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    h.generated = [{'locator': R2, 'proposed_value': 'First reading'},
                   {'locator': R2, 'proposed_value': 'Second reading'}]
    recovery.process(h.store, payload)
    assert json.loads(h.row()['proposals']) == [human_only(R1)]
    assert [p['retry'] for p in sorted(h.retries(), key=lambda p: p['retry'])] == [1, 2]
    recovered = [e for e in h.events() if e['kind'] == 'remediate.vision_retry_recovered'][-1]
    assert recovered['detail']['missing'] == 1 and recovered['detail']['coverage_complete'] is False


def test_absent_target_on_retry_2_does_not_buy_a_third(h):
    h.seed([human_only(R1)], 2)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    h.generated = [{'locator': R2, 'proposed_value': 'First reading'},
                   {'locator': R2, 'proposed_value': 'Second reading'}]
    recovery.process(h.store, dict(payload, retry=2))
    assert [p['retry'] for p in h.retries()] == [1]


# ── phase 3: blocked events bind to their review row ──────────────────────────────────────────

def test_blocked_after_validation_carries_item_id(h, monkeypatch):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    monkeypatch.setattr(recovery, '_recovery_block', lambda context, misses=(), check_admission=True:
                        'vision_budget_exhausted')
    recovery.process(h.store, payload)
    assert h.events()[-1]['detail'] == {'reason_code': 'vision_budget_exhausted', 'item_id': h.item_id}
    assert 'no_ai_request' not in h.events()[-1]['detail']


def test_blocked_retry_limit_and_denial_carry_item_id(h, monkeypatch):
    import ai_standing_approval
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    recovery.process(h.store, dict(payload, retry=3))
    assert h.events()[-1]['detail'] == {'reason_code': 'vision_recovery_unresolved', 'item_id': h.item_id}
    def deny(*a):
        raise ValueError('Review permission is required for automatic AI approval')
    monkeypatch.setattr(ai_standing_approval, 'require_access', deny)
    recovery.process(h.store, payload)
    assert h.events()[-1]['detail'] == {'reason_code': 'vision_recovery_unresolved', 'item_id': h.item_id}


def test_schedule_block_with_a_payload_carries_item_id(h):
    from ai_run_policy import run_context
    h.seed([missing(R1)], 1)
    with run_context(h.store, h.job['payload'], h.job) as context:
        context.deferred.append({'reason': 'provider_access_denied'})
        recovery.schedule(h.store, context, h.job, [], inspect_pending=True)
    assert h.events()[-1]['detail'] == {'reason_code': 'vision_provider_access_denied', 'item_id': h.item_id}


def test_blocked_after_dispatch_carries_item_id_and_no_request_claim(h, monkeypatch):
    h.seed([missing(R1)], 1)
    [payload] = h.schedule(misses=(), inspect_pending=True)
    h.generated = [{'locator': R1, 'proposed_value': 'A synthetic caption'}]
    monkeypatch.setattr(recovery, '_recovery_block', lambda context, misses=(), check_admission=True:
                        None if check_admission else 'vision_spending_reconciliation_required')
    recovery.process(h.store, payload)
    assert len(h.ai_calls) == 1
    assert h.events()[-1]['detail'] == {'reason_code': 'vision_spending_reconciliation_required',
                                        'item_id': h.item_id}
