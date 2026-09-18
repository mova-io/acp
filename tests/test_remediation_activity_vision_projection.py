"""The remediation activity projection tells an obsolete image-description retry apart from a
genuine failure, and says which stage of the work every event belongs to (contract V1/V3).

THE PRODUCTION SEQUENCE THIS REPRODUCES (sanitized, synthetic ids only). A DOCX's single 1.1.1
image target got an AI draft (`recovered`, drafts=1). A second retry was queued for it. A human
then approved a 1.4.5 replacement of the SAME image, which was applied and verified — so the saved
document legitimately changed. Retry 2 then ran, found its input stale and stopped before any AI
request with 'The authorized run or saved input changed.'. That was recorded as a generic
`remediate.vision_retry_blocked {reason_code: 'vision_recovery_unresolved'}`, which the activity
panel rendered as "AI generation paused · check the recorded failure" — a failure that never
happened.

Rows already written stay exactly as written. The correction is READ-TIME: the stored blocked
event is paired with its decision_log row, and only the two exact "your input moved" reasons are
re-labelled — with provenance, and without ever putting the raw reason text on the wire.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from test_scan_history_route import client, _seed  # noqa: E402,F401  (fixtures)
from test_remediation_progress_wire import gated_client, _drain, _events, OWNER  # noqa: E402,F401

SID = 'scan-vr'
DOC = 'synthetic-report.docx'
RUN = 'run-synthetic-1'
INPUT_CHANGED = 'The authorized run or saved input changed.'
REVIEW_CHANGED = 'The review changed; its current decision is preserved.'
T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def _decision(store, action, detail, *, ts, file=DOC, sid=SID):
    """A decision_log row at a CONTROLLED time — log_decision always stamps now()."""
    with store._db.cursor() as cur:
        store._db.execute(cur,
            'INSERT INTO decision_log(id,ts,actor,action,scan_id,file,rule_id,detail) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
            (uuid.uuid4().hex[:12], ts, 'system', action, sid, file, None,
             json.dumps(detail, sort_keys=True, separators=(',', ':'))))


def _blocked(store, *, at, reason, reason_code=None, file=DOC, run=RUN, event_code=None,
             decision_skew=-0.01, sid=SID):
    """The historical pair vision_recovery._decision wrote: decision row first, then the event."""
    _decision(store, 'vision.recovery.blocked',
              {'cause': None, 'reason': reason, 'reason_code': reason_code, 'run_id': run},
              ts=_at(at + decision_skew), file=file, sid=sid)
    return store.append_scan_event(sid, 'remediate.vision_retry_blocked', phase='remediate',
        document=file, correlation_id=run, occurred_at=_at(at),
        detail={'reason_code': event_code or 'vision_recovery_unresolved'})


def _production_sequence(store, sid=SID, owner='demo'):
    """recovered{drafts:1} → pending retry 2 → 1.4.5 accepted/applied/verified → blocked."""
    if owner is not None:
        _seed(store, sid, owner=owner)
    _decision(store, 'vision.recovery.recovered', {'drafts': 1, 'run_id': RUN}, ts=_at(0), sid=sid)
    store.append_scan_event(sid, 'remediate.vision_retry_recovered', phase='remediate',
                            document=DOC, correlation_id=RUN, occurred_at=_at(0.01),
                            detail={'drafts': 1})
    _decision(store, 'vision.recovery.pending', {'retry': 2, 'run_id': RUN}, ts=_at(1), sid=sid)
    store.append_scan_event(sid, 'remediate.vision_retry_pending', phase='remediate',
                            document=DOC, correlation_id=RUN, occurred_at=_at(1.01),
                            detail={'retry': 2, 'run_after': _at(60)})
    for i, kind in enumerate(('remediate.accepted', 'remediate.fix_applied', 'remediate.verified')):
        store.append_scan_event(sid, kind, phase='remediate', document=DOC, correlation_id=RUN,
                                occurred_at=_at(10 + i), detail={'rule_id': '1.4.5'})
    return _blocked(store, at=70, reason=INPUT_CHANGED, sid=sid)


def _history(store, sid=SID, **kw):
    from remediation_activity_history import read_recent_activity
    body = read_recent_activity(store, sid, 'demo', **kw)
    assert body['available'] is True, body
    return body['events']


def _by_kind(events, kind):
    return [e for e in events if e['kind'] == kind]


# ── reproduction ──────────────────────────────────────────────────────────────

def test_the_obsolete_retry_in_the_production_sequence_is_not_a_generic_block(isolated_store):
    seq = _production_sequence(isolated_store)
    [blocked] = _by_kind(_history(isolated_store), 'remediate.vision_retry_blocked')
    assert blocked['seq'] == seq
    assert blocked['detail'] == {
        'reason_code': 'vision_retry_input_changed',
        'recorded_reason_code': 'vision_recovery_unresolved',
        'projection': 'historical_obsolete_retry',
    }
    assert blocked['activity_stage'] == 'draft_generation'


def test_the_stored_row_is_untouched(isolated_store):
    _production_sequence(isolated_store)
    _history(isolated_store)
    [raw] = [e for e in isolated_store.list_scan_events(SID)
             if e['kind'] == 'remediate.vision_retry_blocked']
    assert raw['detail'] == {'reason_code': 'vision_recovery_unresolved'}


def test_the_review_changed_reason_is_remapped_likewise(isolated_store):
    _seed(isolated_store, SID)
    _blocked(isolated_store, at=5, reason=REVIEW_CHANGED)
    [blocked] = _history(isolated_store)
    assert blocked['detail']['reason_code'] == 'vision_retry_review_changed'
    assert blocked['detail']['recorded_reason_code'] == 'vision_recovery_unresolved'
    assert blocked['detail']['projection'] == 'historical_obsolete_retry'


# ── only the two exact reasons, only on an unambiguous pairing ────────────────

@pytest.mark.parametrize('reason', [
    'The authorized run or saved input changed',          # no period — not exact
    'the authorized run or saved input changed.',         # case
    'A stored corrected copy and pending review are required.',
    'The review changed while vision was recovering.',
    None,
])
def test_any_other_reason_stays_a_genuine_block(isolated_store, reason):
    _seed(isolated_store, SID)
    _blocked(isolated_store, at=5, reason=reason)
    [blocked] = _history(isolated_store)
    assert blocked['detail'] == {'reason_code': 'vision_recovery_unresolved'}


def test_a_named_block_code_is_never_remapped(isolated_store):
    """The exact reason with a specific recorded code is not the historical catch-all shape."""
    _seed(isolated_store, SID)
    _blocked(isolated_store, at=5, reason=INPUT_CHANGED, reason_code='vision_budget_exhausted',
             event_code='vision_budget_exhausted')
    [blocked] = _history(isolated_store)
    assert blocked['detail'] == {'reason_code': 'vision_budget_exhausted'}


def test_a_decision_outside_the_window_does_not_pair(isolated_store):
    _seed(isolated_store, SID)
    _blocked(isolated_store, at=5, reason=INPUT_CHANGED, decision_skew=-6)
    [blocked] = _history(isolated_store)
    assert 'projection' not in blocked['detail']


def test_a_decision_for_another_file_or_run_or_scan_does_not_pair(isolated_store):
    _seed(isolated_store, SID)
    _decision(isolated_store, 'vision.recovery.blocked',
              {'reason': INPUT_CHANGED, 'reason_code': None, 'run_id': RUN}, ts=_at(5), file='other.docx')
    _decision(isolated_store, 'vision.recovery.blocked',
              {'reason': INPUT_CHANGED, 'reason_code': None, 'run_id': RUN}, ts=_at(5), sid='elsewhere')
    isolated_store.append_scan_event(SID, 'remediate.vision_retry_blocked', document=DOC,
                                     correlation_id=RUN, occurred_at=_at(5),
                                     detail={'reason_code': 'vision_recovery_unresolved'})
    _decision(isolated_store, 'vision.recovery.blocked',
              {'reason': INPUT_CHANGED, 'reason_code': None, 'run_id': 'another-run'},
              ts=_at(30), file=DOC)
    isolated_store.append_scan_event(SID, 'remediate.vision_retry_blocked', document=DOC,
                                     correlation_id=RUN, occurred_at=_at(30),
                                     detail={'reason_code': 'vision_recovery_unresolved'})
    assert [e['detail'] for e in _history(isolated_store)] == [
        {'reason_code': 'vision_recovery_unresolved'}] * 2


@pytest.mark.parametrize('event_run,decision_run', [
    (None, RUN),          # the event names no run
    (RUN, None),          # the decision names no run
    (RUN, 'run-other'),   # cross-run
])
def test_an_unknown_or_different_run_never_pairs(isolated_store, event_run, decision_run):
    _seed(isolated_store, SID)
    _decision(isolated_store, 'vision.recovery.blocked',
              {'reason': INPUT_CHANGED, 'reason_code': None, 'run_id': decision_run}, ts=_at(4.99))
    isolated_store.append_scan_event(SID, 'remediate.vision_retry_blocked', document=DOC,
        correlation_id=event_run, occurred_at=_at(5), detail={'reason_code': 'vision_recovery_unresolved'})
    [only] = _history(isolated_store)
    assert only['detail'] == {'reason_code': 'vision_recovery_unresolved'}


def test_a_historical_projection_never_claims_no_ai_request(isolated_store):
    """The decision row proves why the retry stopped, not that no provider was called first."""
    _production_sequence(isolated_store)
    _blocked(isolated_store, at=200, reason=REVIEW_CHANGED)
    remapped = [e for e in _history(isolated_store) if e['detail'].get('projection')]
    assert len(remapped) == 2
    assert all('no_ai_request' not in e['detail'] for e in remapped)


def test_two_candidate_decisions_are_ambiguous_and_left_unmapped(isolated_store):
    """Two blocks for the same file inside one window: which reason belongs to which event cannot
    be proven, so neither is re-labelled — even though one of them really was obsolete."""
    _seed(isolated_store, SID)
    _blocked(isolated_store, at=5, reason=INPUT_CHANGED)
    _blocked(isolated_store, at=6, reason='Vision provider refused the request.')
    events = _history(isolated_store)
    assert [e['detail'] for e in events] == [{'reason_code': 'vision_recovery_unresolved'}] * 2


def test_one_event_with_two_decisions_in_reach_is_ambiguous_even_if_one_is_nearer(isolated_store):
    """A decision row can exist without its event (the append is best-effort and swallowed), so
    one event may see two decisions. Nearest-wins would guess; the rule refuses to."""
    _seed(isolated_store, SID)
    _decision(isolated_store, 'vision.recovery.blocked',
              {'reason': INPUT_CHANGED, 'reason_code': None, 'run_id': RUN}, ts=_at(4.9))
    _decision(isolated_store, 'vision.recovery.blocked',
              {'reason': 'Vision provider refused the request.', 'reason_code': None, 'run_id': RUN},
              ts=_at(7))
    isolated_store.append_scan_event(SID, 'remediate.vision_retry_blocked', document=DOC,
        correlation_id=RUN, occurred_at=_at(5), detail={'reason_code': 'vision_recovery_unresolved'})
    [only] = _history(isolated_store)
    assert only['detail'] == {'reason_code': 'vision_recovery_unresolved'}


def test_a_decision_claimed_by_two_events_is_ambiguous(isolated_store):
    """One decision row, two events within reach of it: the reverse direction of the same rule."""
    _seed(isolated_store, SID)
    _decision(isolated_store, 'vision.recovery.blocked',
              {'reason': INPUT_CHANGED, 'reason_code': None, 'run_id': RUN}, ts=_at(5))
    for at in (4, 6):
        isolated_store.append_scan_event(SID, 'remediate.vision_retry_blocked', document=DOC,
                                         correlation_id=RUN, occurred_at=_at(at),
                                         detail={'reason_code': 'vision_recovery_unresolved'})
    assert [e['detail'] for e in _history(isolated_store)] == [
        {'reason_code': 'vision_recovery_unresolved'}] * 2


def test_pairing_does_not_depend_on_the_page(isolated_store):
    """A page holding only ONE of two competing events must reach the same verdict as a page
    holding both — otherwise the stream (one event per tick) and the history would disagree."""
    _seed(isolated_store, SID)
    _decision(isolated_store, 'vision.recovery.blocked',
              {'reason': INPUT_CHANGED, 'reason_code': None, 'run_id': RUN}, ts=_at(5))
    first = isolated_store.append_scan_event(SID, 'remediate.vision_retry_blocked', document=DOC,
        correlation_id=RUN, occurred_at=_at(4), detail={'reason_code': 'vision_recovery_unresolved'})
    isolated_store.append_scan_event(SID, 'remediate.vision_retry_blocked', document=DOC,
        correlation_id=RUN, occurred_at=_at(6), detail={'reason_code': 'vision_recovery_unresolved'})
    [only] = _history(isolated_store, after_seq=first - 1, limit=1)
    assert only['seq'] == first and 'projection' not in only['detail']


def test_the_mapping_uses_one_decision_query_per_page(isolated_store, monkeypatch):
    _seed(isolated_store, SID)
    for i in range(6):
        _blocked(isolated_store, at=100 * i, reason=INPUT_CHANGED, file=f'd{i}.docx')
    seen = []
    original = isolated_store._db.execute

    def counting(cur, sql, params=()):
        seen.append(sql)
        return original(cur, sql, params)
    monkeypatch.setattr(isolated_store._db, 'execute', counting)
    events = _history(isolated_store)
    assert all(e['detail']['projection'] == 'historical_obsolete_retry' for e in events)
    assert sum('FROM decision_log' in sql for sql in seen) == 1


# ── privacy ───────────────────────────────────────────────────────────────────

def test_suppression_still_strips_the_name_from_a_remapped_event(isolated_store):
    _production_sequence(isolated_store)
    isolated_store.set_setting('remediation_filename_privacy', 'suppressed')
    events = _history(isolated_store)
    [blocked] = _by_kind(events, 'remediate.vision_retry_blocked')
    assert blocked['detail']['reason_code'] == 'vision_retry_input_changed'
    assert blocked['document'] is None and blocked['document_suppressed'] is True
    assert DOC not in json.dumps(events)


def test_no_raw_reason_text_reaches_any_projected_event(isolated_store):
    _production_sequence(isolated_store)
    _blocked(isolated_store, at=200, reason=REVIEW_CHANGED)
    _blocked(isolated_store, at=300, reason='Some provider said something private.')
    wire = json.dumps(_history(isolated_store))
    for text in (INPUT_CHANGED, REVIEW_CHANGED, 'provider said', 'authorized run'):
        assert text not in wire


# ── activity stage ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('kind,stage', [
    ('remediate.vision_retry_pending', 'draft_generation'),
    ('remediate.vision_retry_recovered', 'draft_generation'),
    ('remediate.vision_retry_blocked', 'draft_generation'),
    ('remediate.vision_retry_obsolete', 'draft_generation'),
    ('remediate.ai_request_started', 'draft_generation'),
    ('remediate.ai_request_finished', 'draft_generation'),
    ('remediate.accepted', 'review'),
    ('remediate.review_requested', 'review'),
    ('remediate.review_target_replaced', 'review'),
    ('remediate.fix_applied', 'document_write'),
    ('remediate.verified', 'verification'),
    ('remediate.verification_failed', 'verification'),
    ('remediate.delivered', 'delivery'),
    ('remediate.delivery_failed', 'delivery'),
    ('remediate.delivery_retry_requested', 'delivery'),
    ('remediate.delivery_retry_refused', 'delivery'),
    ('remediate.document_completed', 'run'),
    ('remediate.paused', 'run'),
    ('scan.interrupted', 'run'),
])
def test_every_projected_event_names_its_stage(isolated_store, kind, stage):
    _seed(isolated_store, SID)
    isolated_store.append_scan_event(SID, kind, document=DOC)
    [event] = _history(isolated_store)
    assert event['activity_stage'] == stage


def test_a_draft_a_document_write_and_a_verification_are_distinguished(isolated_store):
    _production_sequence(isolated_store)
    stages = {e['kind']: e['activity_stage'] for e in _history(isolated_store)}
    assert stages['remediate.vision_retry_recovered'] == 'draft_generation'
    assert stages['remediate.fix_applied'] == 'document_write'
    assert stages['remediate.verified'] == 'verification'


def test_a_historical_recovered_event_stays_as_recorded(isolated_store):
    """No awaiting_review/missing keys are invented: an unknown count is not zero."""
    _production_sequence(isolated_store)
    [recovered] = _by_kind(_history(isolated_store), 'remediate.vision_retry_recovered')
    assert recovered['detail'] == {'drafts': 1}


# ── new kinds ─────────────────────────────────────────────────────────────────

NEW_KINDS = ('remediate.vision_retry_obsolete', 'remediate.review_target_replaced')


def test_the_new_kinds_are_declared_but_are_not_progress(isolated_store):
    import remediation_activity_history as history
    for kind in NEW_KINDS:
        assert kind in isolated_store.SCAN_EVENT_KINDS
        assert kind in history.KINDS
        assert kind not in isolated_store.MATERIAL_SCAN_EVENT_KINDS


def test_the_new_kinds_reach_the_activity_history(isolated_store):
    _seed(isolated_store, SID)
    isolated_store.append_scan_event(SID, 'remediate.vision_retry_obsolete', document=DOC,
        detail={'retry': 2, 'reason_code': 'vision_retry_input_changed', 'no_ai_request': True})
    isolated_store.append_scan_event(SID, 'remediate.review_target_replaced', document=DOC,
        detail={'rule_id': '1.1.1', 'removed_by_rule_id': '1.4.5', 'finding_count': 1})
    events = _history(isolated_store)
    assert [e['kind'] for e in events] == list(NEW_KINDS)
    assert [e['activity_stage'] for e in events] == ['draft_generation', 'review']
    assert [e['material'] for e in events] == [False, False]


# ── the live stream and the polling fallback agree with the history ──────────

def test_the_stream_replay_and_the_history_route_project_identically(gated_client, isolated_store):
    sid, _ = isolated_store.enqueue_scan('s-vr-stream', 'local', OWNER, 'scan_discover', {})
    _production_sequence(isolated_store, sid=sid, owner=None)
    isolated_store.append_scan_event(sid, 'remediate.vision_retry_obsolete', document=DOC,
        detail={'retry': 2, 'reason_code': 'vision_retry_review_only', 'no_ai_request': True})
    isolated_store.append_scan_event(sid, 'remediate.review_target_replaced', document=DOC,
        detail={'rule_id': '1.1.1', 'removed_by_rule_id': '1.4.5', 'finding_count': 1})
    client = gated_client(OWNER)
    streamed = [json.loads(f['data']) for f in _events(_drain(client, sid, {'Last-Event-ID': '0'}, stop_after=12))]
    polled = client.get(f'/scans/{sid}/history').json()['events']
    from remediation_activity_history import read_recent_activity
    recent = read_recent_activity(isolated_store, sid, OWNER)['events']

    def shape(events):
        return [(e['seq'], e['kind'], e['activity_stage'], e['detail']) for e in events]
    assert shape(streamed) == shape(polled) == shape(recent)
    [blocked] = _by_kind(streamed, 'remediate.vision_retry_blocked')
    assert blocked['detail']['reason_code'] == 'vision_retry_input_changed'
    assert {e['kind'] for e in streamed} >= set(NEW_KINDS)


def test_a_live_tick_projects_the_mapping_too(isolated_store, monkeypatch):
    """The live loop projects one fresh batch at a time; the batch helper is what it calls."""
    import core
    from routes import scans
    monkeypatch.setattr(core, 'store', isolated_store)
    seq = _production_sequence(isolated_store)
    fresh = isolated_store.list_scan_events(SID, after_seq=seq - 1)
    [live] = scans._project_events(fresh, SID, 'visible')
    assert live['detail']['projection'] == 'historical_obsolete_retry'
    assert live['activity_stage'] == 'draft_generation'


def test_a_mapping_failure_never_fails_the_projection(isolated_store, monkeypatch):
    import remediation_activity_history as history
    _production_sequence(isolated_store)
    monkeypatch.setattr(history, '_blocked_decisions',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db down')))
    [blocked] = _by_kind(_history(isolated_store), 'remediate.vision_retry_blocked')
    assert blocked['detail'] == {'reason_code': 'vision_recovery_unresolved'}


# ── remediate.review_target_replaced is emitted by the retirement itself ─────

def _replaced(store, sid):
    return [e for e in store.list_scan_events(sid) if e['kind'] == 'remediate.review_target_replaced']


def _ts_of(value):
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def test_a_verified_target_replacement_is_narrated_once_with_a_safe_detail(isolated_store):
    from test_review_target_reconciliation import World, SID as RSID, FILE as RFILE
    w = World(isolated_store)
    w.reconcile()
    [event] = _replaced(isolated_store, RSID)
    assert event['detail'] == {'item_id': str(w.alt), 'rule_id': '1.1.1',
                               'removed_by_rule_id': '1.4.5', 'finding_count': 1}
    assert event['document'] == RFILE and event['correlation_id'] == w.batch
    assert event['phase'] == 'remediate'
    # After the decision line it narrates, never before it.
    [line] = w.lines()
    assert _ts_of(event['occurred_at']) >= _ts_of(line['ts'])


def test_the_replacement_names_its_rule_in_dotted_form_whatever_the_row_stores(isolated_store):
    from test_review_target_reconciliation import World, SID as RSID
    w = World(isolated_store)
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, "UPDATE hitl_queue SET rule_id='SC_1_1_1' WHERE id=%s", (w.alt,))
    w.reconcile()
    [event] = _replaced(isolated_store, RSID)
    assert event['detail']['rule_id'] == '1.1.1'
    assert event['detail']['item_id'] == str(w.alt)


def test_a_replayed_retirement_does_not_narrate_twice(isolated_store):
    from test_review_target_reconciliation import World, SID as RSID
    w = World(isolated_store)
    w.reconcile()
    second = w.reconcile()
    assert not second['superseded']
    assert len(_replaced(isolated_store, RSID)) == 1


def test_a_refused_retirement_narrates_nothing(isolated_store):
    from test_review_target_reconciliation import World, SID as RSID
    w = World(isolated_store, replaced=())          # the fix never removed the target
    w.reconcile()
    assert _replaced(isolated_store, RSID) == []


def test_a_failing_append_never_fails_the_retirement(isolated_store, monkeypatch):
    from test_review_target_reconciliation import World
    w = World(isolated_store)
    real = isolated_store.append_scan_event

    def broken(sid, kind, **kw):
        if kind == 'remediate.review_target_replaced':
            raise RuntimeError('event log down')
        return real(sid, kind, **kw)
    monkeypatch.setattr(isolated_store, 'append_scan_event', broken)
    import swallowed
    reported = []
    monkeypatch.setattr(swallowed, 'swallowed', lambda op, scan_id=None: reported.append((op, scan_id)))
    result = w.reconcile()
    assert [s['item_id'] for s in result['superseded']] == [w.alt]
    assert len(w.lines()) == 1
    # Best-effort, but never silent: the failed narration is reported through the rate-limited
    # diagnostic (tests/test_no_handler_fails_silently.py forbids a bare `pass`).
    assert [op for op, _ in reported] == [
        'review_target_reconciliation._narrate_replacements: appending the '
        'review_target_replaced event failed']
    assert reported[0][1] is not None


def test_the_replacement_event_projects_as_review_and_suppresses_the_name(isolated_store):
    from test_review_target_reconciliation import World, SID as RSID, FILE as RFILE, OWNER as ROWNER
    from remediation_activity_history import read_recent_activity
    w = World(isolated_store)
    w.reconcile()
    isolated_store.set_setting('remediation_filename_privacy', 'suppressed')
    body = read_recent_activity(isolated_store, RSID, ROWNER)
    [event] = [e for e in body['events'] if e['kind'] == 'remediate.review_target_replaced']
    assert event['activity_stage'] == 'review' and event['material'] is False
    assert RFILE not in json.dumps(body)
