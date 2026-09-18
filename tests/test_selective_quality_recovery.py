"""Offline recovery must preserve usable content and terminal finding identities."""
from copy import deepcopy
import pytest
import vision_recovery as recovery
from test_document_wide_workflow import setup
import document_wide_workflow as workflow


def test_selective_merge_preserves_source_evidence_and_usable_draft():
    prior = [dict(locator='source', proposed_value='Author supplied caption', source='author',
                  source_sha256='frozen', chart_review={'status':'validated'}),
             dict(locator='missing', proposed_value=''),
             # Known contradiction with the pixels: repairable, so a regeneration may replace it.
             dict(locator='uncertain', proposed_value='A blue square on a white background',
                  automatic_write_blocked=True, caption_validation={'status': 'rejected',
                      'reason_codes': ['pixel_shape_or_color_contradiction']}),
             # Only a human meaning check outstanding: never replaced by another generation.
             dict(locator='human', proposed_value='A bridge over water', automatic_write_blocked=True,
                  review_status='needs_review', reason_code='quality_source_meaning_unverified')]
    original = deepcopy(prior)
    result = recovery.merge_recovered(prior, [
        dict(locator='source', proposed_value='Hallucinated replacement'),
        dict(locator='missing', proposed_value='New draft'),
        dict(locator='uncertain', proposed_value='Corrected draft', requires_semantic_review=True),
        dict(locator='human', proposed_value='Regenerated over a human-awaiting draft')])
    assert result[0] == original[0]
    assert result[1]['proposed_value'] == 'New draft'
    assert result[2]['requires_semantic_review'] is True
    assert result[3] == original[3]
    assert prior == original


def test_duplicate_generated_locator_does_not_choose_arbitrary_caption():
    prior = [dict(locator='image', proposed_value='')]
    assert recovery.merge_recovered(prior, [dict(locator='image', proposed_value=v)
                                           for v in ['Year 2023', 'Year 2024']]) == prior


def test_retained_office_locator_does_not_call_image_model(monkeypatch):
    import remediate_office as office
    import ai
    monkeypatch.setattr(office, '_image_bytes_for', lambda *a: ('r1', b'image'))
    monkeypatch.setattr(ai, 'describe_image_structured', lambda *a, **k: pytest.fail('paid call'))
    budget = [3]
    result = office._vision_alt('', None, '', '', None, {'media': b'image'},
                               'word/document.xml', True, 'file.docx', '', 'scan', budget,
                               skip_locators={'word/document.xml#r1'})
    assert result is None and budget == [3]


@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('disposition', ['resolved_verified', 'excluded_by_policy', 'superseded_by_reassessment'])
def test_terminal_findings_cannot_be_reintroduced_at_persistence(monkeypatch, cached, disposition):
    store, ctx, calls, logs, queued = setup(monkeypatch)
    store.list_finding_dispositions = lambda *a: [dict(finding_id='real-finding',
        file=ctx.file, rule_id='4.1.2', disposition=disposition)]
    if cached:
        monkeypatch.setattr(workflow, '_saved', lambda *a: {'proposals': {'4.1.2': [
            dict(locator='field', finding_ids=['real-finding'], proposed_value='Name')]}})
    workflow.process_file(store, ctx)
    assert not queued
    assert bool(calls) is (not cached)


def test_recovery_excludes_usable_locator_before_generation(isolated_store, monkeypatch):
    from test_vision_recovery import seed, draft, SID
    _, payload = seed(isolated_store)
    draft(monkeypatch)
    import remediate_office
    seen = []
    def generate(*args, **kwargs):
        seen.append(kwargs['skip_locators'])
        return [dict(locator='word/document.xml#r2', proposed_value='Replacement'),
                dict(locator='word/document.xml#r1', proposed_value='Recovered')], []
    monkeypatch.setattr(remediate_office, 'alt_proposals_for_office', generate)
    recovery.process(isolated_store, payload)
    assert seen == [{'word/document.xml#r2'}]
    item = isolated_store.get_hitl_item(payload['item_id'])
    assert item['proposals'][1]['proposed_value'] == 'Author supplied caption'
