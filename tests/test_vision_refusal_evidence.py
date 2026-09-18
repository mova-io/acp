"""A dispatch refusal must be DIAGNOSABLE, not merely correct.

THE FINDING behind these tests. In production the user saw one thing: a 1.1.1 review pending
with no drafts and a `vision_recovery_unresolved` block. `ai_calls` was empty and
`ai_spending_attempts` was empty — not because the ledger failed, but because the refusal
happened BEFORE either was touched. The exact reason (`quality_first_cloud_endpoint_required`)
existed only in memory and was discarded with the request, so a misconfiguration, an exhausted
budget and a provider outage all presented identically and the UI could offer only a generic
action.

Two things now carry that reason out of memory:

  * `llm_waterfall_provider._record_pre_dispatch_refusal` writes ONE owner-scoped
    `remediate.ai_request_finished` event naming the exact reason, on the same owner-gated
    narration path `ai_request_activity` uses. It fires only when nothing was attempted — a
    refusal with an attempt already has its reservation and attempt-history rows.
  * `vision_recovery._decision` attaches the exact refusal reasons to the block event when, and
    only when, the block code is the catch-all. Codes that already name their cause are
    unchanged.

Neither weakens the refusal: an endpoint the configuration cannot place as cloud is still
refused, before any spend. It is now refused visibly.

Hermetic: no provider, no paid dispatch, no ledger reservation.
"""
import json

import pytest

import llm_waterfall_provider
import vision_generation as vision
import vision_recovery
from ai_attempt_history import AttemptHistory
from ai_run_policy import run_context
from test_quality_first_zone_propagation import (  # noqa: F401 - fixtures used by name
    MODELS, build_generator, image, quality_first_run)

REFUSAL = 'quality_first_cloud_endpoint_required'


def refusals(store, sid='scan'):
    return [event for event in store.list_scan_events(sid)
            if event['kind'] == 'remediate.ai_request_finished']


def refuse(store, job, monkeypatch, zone='local', *, reported=True, times=1):
    """Run the production vision path against an endpoint quality_first must refuse."""
    monkeypatch.setattr(vision, 'configured_generator',
                        lambda: build_generator(zone, reported=reported))
    with run_context(store, job['payload'], job) as ctx:
        results = [vision.generate('Describe the image', image()) for _ in range(times)]
        attempts = AttemptHistory(store._db).list_run(ctx.owner_id, ctx.scan_id, ctx.run_id)
        return results[-1], attempts, ctx.run_id


def test_a_refusal_before_any_ledger_call_leaves_a_readable_owner_scoped_record(
        quality_first_run, monkeypatch):
    """The production symptom, inverted: nothing was attempted, and it is still on the record."""
    store, job = quality_first_run
    generated, attempts, run_id = refuse(store, job, monkeypatch)

    # Both symptoms the user saw are still true — the refusal really did precede everything.
    assert generated['deferred'] is True and generated['reason'] == REFUSAL
    assert attempts == []
    assert store.list_ai_calls('scan') == []

    events = refusals(store)
    assert len(events) == 1, events
    event = events[0]
    assert event['owner_email'] == 'owner'
    assert event['document'] == 'a.docx'
    assert event['correlation_id'] == run_id
    assert event['detail']['run_id'] == run_id
    assert event['detail']['status'] == 'failed'
    assert event['detail']['dispatched'] is False
    # Never claimed as an AI call that happened: no model ran and nothing was charged.
    assert event['detail']['model'] == 'not-dispatched'
    assert event['detail']['processing_zone'] == 'unknown'

    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT detail FROM decision_log WHERE action=%s AND scan_id=%s",
                          ('remediate.ai_request_finished', 'scan'))
        rows = store._db.fetchall(cur)
    assert [json.loads(row['detail'])['reason'] for row in rows] == [REFUSAL]


def test_the_recorded_reason_is_the_exact_one_not_a_generic_block(quality_first_run, monkeypatch):
    """A block CODE is a category. The refusal reason is the configuration fact behind it."""
    store, job = quality_first_run
    refuse(store, job, monkeypatch)
    recorded = refusals(store)[0]['detail']['reason']
    assert recorded == REFUSAL
    assert recorded not in vision_recovery.BLOCK_CODES
    assert recorded != 'vision_recovery_unresolved'


@pytest.mark.parametrize('zone,reported,label', [
    ('local', True, 'an endpoint on our own infrastructure'),
    (None, True, 'a configuration that reports no zone for the endpoint'),
    (None, False, 'a configuration with no zone function at all'),
])
def test_an_unplaceable_or_local_zone_still_refuses_and_is_now_diagnosable(
        quality_first_run, monkeypatch, zone, reported, label):
    """The zones fix must not mask the next misconfiguration — it must name it."""
    store, job = quality_first_run
    generated, attempts, _ = refuse(store, job, monkeypatch, zone, reported=reported)
    assert generated['reason'] == REFUSAL, label          # still refused, before any spend
    assert attempts == [] and store.list_ai_calls('scan') == []
    assert [event['detail']['reason'] for event in refusals(store)] == [REFUSAL], label


def test_one_refusal_is_recorded_once_however_many_images_hit_it(quality_first_run, monkeypatch):
    """A repeat is the same configuration fact; this path fans out per image."""
    store, job = quality_first_run
    _, _, _ = refuse(store, job, monkeypatch, times=3)
    assert len(refusals(store)) == 1


def test_evidence_is_not_written_for_a_run_the_owner_no_longer_holds(quality_first_run, monkeypatch):
    """The write is owner-gated on a CURRENT, uncancelled execution, like every other narration."""
    store, job = quality_first_run
    with store._db.cursor() as cur:
        store._db.execute(cur, "UPDATE stage_executions SET cancel_requested_at='now' WHERE execution_id=%s",
                          (job['batch_id'],))
    generated, _, _ = refuse(store, job, monkeypatch)
    assert generated['reason'] == REFUSAL      # the refusal itself is unchanged
    assert refusals(store) == []


def test_one_owners_refusal_is_not_readable_as_another_owners(quality_first_run, monkeypatch):
    """Owner scoping holds at the write AND at the read."""
    from remediation_activity_history import read_recent_activity
    store, job = quality_first_run
    refuse(store, job, monkeypatch)

    mine = read_recent_activity(store, 'scan', 'owner')
    assert mine['available'] is True
    assert [event['detail']['reason'] for event in mine['events']
            if event['kind'] == 'remediate.ai_request_finished'] == [REFUSAL]

    theirs = read_recent_activity(store, 'scan', 'someone-else@example.test')
    assert theirs == {'available': False, 'reason': 'scan_not_found'}
    assert all(event['owner_email'] == 'owner' for event in refusals(store))


def test_the_catch_all_block_now_names_its_cause(isolated_store):
    """`vision_recovery_unresolved` said only that something was unresolved. Now it says what."""
    vision_recovery._decision(isolated_store, 'scan', 'a.docx', 'blocked', run_id='run',
                              reason_code='vision_recovery_unresolved',
                              reason=vision_recovery._block_description('vision_recovery_unresolved'),
                              cause=[REFUSAL])
    assert isolated_store.list_scan_events('scan')[-1]['detail'] == {
        'reason_code': 'vision_recovery_unresolved', 'cause': [REFUSAL]}


def test_a_block_code_that_already_names_its_cause_is_unchanged(isolated_store):
    vision_recovery._decision(isolated_store, 'scan', 'a.docx', 'blocked', run_id='run',
                              reason_code='vision_provider_access_denied',
                              reason='...', cause=['provider_access_denied'])
    assert isolated_store.list_scan_events('scan')[-1]['detail'] == {
        'reason_code': 'vision_provider_access_denied'}


def test_the_cause_carries_fixed_codes_only_never_provider_or_document_text(isolated_store):
    vision_recovery._decision(isolated_store, 'scan', 'a.docx', 'blocked', run_id='run',
                              reason_code=None, reason='...',
                              cause=['sensitive-filename.docx', 'Provider said: no', REFUSAL, None])
    assert isolated_store.list_scan_events('scan')[-1]['detail'] == {
        'reason_code': 'vision_recovery_unresolved', 'cause': [REFUSAL]}


def test_the_scheduled_block_carries_the_cause_end_to_end(isolated_store):
    """The user-visible block event, through the real `schedule`, on real seeded work."""
    from test_vision_recovery import FILE, SID, seed
    job, payload = seed(isolated_store)
    with run_context(isolated_store, job['payload'], job) as context:
        context.deferred.append({'reason': REFUSAL, 'kind': 'text'})
        vision_recovery.schedule(isolated_store, context, job, ['vision_timeout'])
    event = isolated_store.list_scan_events(SID)[-1]
    assert event['kind'] == 'remediate.vision_retry_blocked'
    assert event['document'] == FILE
    # item_id binds the block to its review row (internal id, not document content).
    assert event['detail'] == {'reason_code': 'vision_recovery_unresolved', 'cause': [REFUSAL],
                               'item_id': payload['item_id']}


def test_a_refusal_that_already_reached_the_ledger_is_not_recorded_twice(quality_first_run):
    """An attempt of its own carries the ledger and attempt-history rows; this adds nothing."""
    store, job = quality_first_run
    with run_context(store, job['payload'], job):
        llm_waterfall_provider.defer_managed('budget_admission_denied',
                                             attempts=[{'attempt_id': 'text:op:1:0'}])
    assert refusals(store) == []
