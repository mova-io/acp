"""Durable activity is scoped and never promoted to finding contribution."""
import json
import pytest
from ai_spending_budget import BudgetLedger
from remediation_waterfall_view import read_waterfall


def seed(store, owner='owner', scan='scan', ai=1):
    with store._db.cursor() as cur:
        store._db.execute(cur, "INSERT INTO scan_runs(id,owner_email,status) VALUES(%s,%s,'done')", (scan, owner))
    run = store.enqueue_stage_batch(scan, 'remediate', 'remediate_file', [
        {'owner': owner, 'scan_id': scan, 'file': 'a.html',
         'remediation_impact_policy': {'ai': ai, 'rule_based': 2, 'ai_budget_usd': '5.00'}}],
        snapshot_id='snapshot', request_fingerprint='fixture')
    return run['batch_id']


def test_scope_legacy_and_read_only(isolated_store):
    s = isolated_store
    batch = seed(s)
    with pytest.raises(LookupError):
        read_waterfall(s, 'intruder', 'scan', batch)
    assert not read_waterfall(s, 'owner', 'scan', 'other-batch')['available']
    view = read_waterfall(s, 'owner', 'scan', batch)
    assert view['available'] and view['ai_enabled']
    assert not view['contribution_available'] and not view['reviewer_available']
    assert view['spending']['available_units'] == 5000000
    assert all(stage['operations'] == 0 for stage in view['stages'])
    assert read_waterfall(s, 'owner', 'scan', batch)['revision'] == view['revision']


def test_costs_retries_unknown_and_tier_identity(isolated_store):
    s = isolated_store
    batch = seed(s)
    ledger = BudgetLedger(s._db)
    operation = 'a' * 64
    def reserve(tier, retry=0):
        aid = f'text:{operation}:{tier}:{retry}'
        ledger.reserve('owner', batch, aid, 200000, 'private-pricing-reference')
        return aid
    released = reserve(1)
    ledger.release('owner', batch, released)
    first = reserve(1, 1)
    assert ledger.claim_dispatch('owner', batch, first)
    ledger.settle('owner', batch, first, 1200)
    second = reserve(2)
    assert ledger.claim_dispatch('owner', batch, second)
    ledger.mark_uncertain('owner', batch, second)
    view = read_waterfall(s, 'owner', 'scan', batch)
    assert view['stages'][0]['operations'] == 1  # retry is not another operation
    assert view['stages'][0]['released'] == view['stages'][0]['settled'] == 1
    assert view['stages'][1]['uncertain'] == 1
    assert view['spending'] == {'cap_units': 5000000, 'currency': 'USD', 'spent_units': 1200,
                                'held_units': 200000, 'available_units': 4798800,
                                'unknown_charges': 1, 'blocked': True}
    assert not view['contribution_available']
    assert operation not in json.dumps(view)
    assert 'private-pricing-reference' not in json.dumps(view)


def test_rules_only_and_other_attempts(isolated_store):
    s = isolated_store
    batch = seed(s, ai=0)
    ledger = BudgetLedger(s._db)
    ledger.reserve('owner', batch, 'unrecognized-format', 10, 'test')
    view = read_waterfall(s, 'owner', 'scan', batch)
    assert not view['ai_enabled']
    assert view['other_attempts'] == 1
    assert view['spending']['held_units'] == 10
    assert sum(stage['operations'] for stage in view['stages']) == 0


def test_route_owner_and_no_store(isolated_store, monkeypatch):
    from starlette.requests import Request
    from starlette.responses import Response
    from fastapi import HTTPException
    import core
    from routes.remediation_waterfall import waterfall_status
    monkeypatch.setattr(core, 'store', isolated_store)
    batch = seed(isolated_store)
    request = Request({'type': 'http', 'state': {'user_email': 'owner'}})
    response = Response()
    assert waterfall_status('scan', batch, request, response)['available']
    assert response.headers['Cache-Control'] == 'no-store'
    request.state.user_email = 'intruder'
    with pytest.raises(HTTPException) as error:
        waterfall_status('scan', batch, request, Response())
    assert error.value.status_code == 404


def test_actual_models_require_exact_proposal_call_links_and_do_not_double_count(isolated_store):
    s = isolated_store
    batch = seed(s)
    calls = [
        ('good', 'scan', 'a.html', 'anthropic', 'recorded-model-v1', 0.000042),
        ('wrong-file', 'scan', 'b.html', 'openai', 'wrong-model', 8.0),
        ('wrong-scan', 'other', 'a.html', 'openai', 'other-model', 9.0),
        ('unknown-cost', 'scan', 'a.html', 'openai', 'recorded-model-v2', 0.0),
    ]
    with s._db.cursor() as cur:
        for call in calls:
            s._db.execute(cur, 'INSERT INTO ai_calls(id,scan_id,file,provider,model,cost_usd) VALUES(%s,%s,%s,%s,%s,%s)', call)
        proposals = json.dumps([{'model_call_id': call[0], 'proposed_value': 'PRIVATE CONTENT'} for call in calls] + [{'model_call_id': 'good'}])
        s._db.execute(cur, 'INSERT INTO hitl_queue(id,scan_id,file,proposals) VALUES(%s,%s,%s,%s)', ('review', 'scan', 'a.html', proposals))
        for fid in ['finding-1', 'finding-2']:
            s._db.execute(cur, 'INSERT INTO finding_disposition(scan_id,batch_id,finding_id,file,review_item_id) VALUES(%s,%s,%s,%s,%s)', ('scan', batch, fid, 'a.html', 'review'))
    view = read_waterfall(s, 'owner', 'scan', batch)
    assert view['models'] == [
        {'provider': 'anthropic', 'model': 'recorded-model-v1', 'linked_calls': 1, 'recorded_cost_usd': 0.000042},
        {'provider': 'openai', 'model': 'recorded-model-v2', 'linked_calls': 1, 'recorded_cost_usd': None},
    ]
    assert 'PRIVATE CONTENT' not in json.dumps(view)
    assert read_waterfall(s, 'owner', 'scan', 'older-batch')['models'] == []


def test_new_proposals_cannot_relabel_a_historical_run(isolated_store):
    s = isolated_store
    batch = seed(s)
    with s._db.cursor() as cur:
        s._db.execute(cur, 'INSERT INTO ai_calls(id,scan_id,file,provider,model,cost_usd) VALUES(%s,%s,%s,%s,%s,%s)', ('old-call', 'scan', 'a.html', 'anthropic', 'old-model', 0.1))
        s._db.execute(cur, 'INSERT INTO hitl_queue(id,scan_id,file,proposals) VALUES(%s,%s,%s,%s)', ('review', 'scan', 'a.html', json.dumps([{'model_call_id': 'old-call'}])))
        s._db.execute(cur, 'INSERT INTO finding_disposition(scan_id,batch_id,finding_id,file,review_item_id) VALUES(%s,%s,%s,%s,%s)', ('scan', batch, 'finding', 'a.html', 'review'))
    assert read_waterfall(s, 'owner', 'scan', batch)['models'][0]['model'] == 'old-model'
    with s._db.cursor() as cur:
        s._db.execute(cur, "UPDATE jobs SET status='done' WHERE batch_id=%s", (batch,))
    new = s.enqueue_stage_batch('scan', 'remediate', 'remediate_file', [
        {'owner': 'owner', 'scan_id': 'scan', 'file': 'a.html',
         'remediation_impact_policy': {'ai': 1, 'rule_based': 2, 'ai_budget_usd': '5.00'}}],
        snapshot_id='snapshot-2', request_fingerprint='fixture-2')
    with s._db.cursor() as cur:
        s._db.execute(cur, 'UPDATE jobs SET created_at=%s WHERE batch_id=%s', ('2099-01-01T00:00:00Z', batch))
        s._db.execute(cur, 'INSERT INTO ai_calls(id,scan_id,file,provider,model,cost_usd) VALUES(%s,%s,%s,%s,%s,%s)', ('new-call', 'scan', 'a.html', 'openai', 'new-model', 0.2))
        s._db.execute(cur, 'UPDATE hitl_queue SET proposals=%s WHERE id=%s', (json.dumps([{'model_call_id': 'new-call'}]), 'review'))
        s._db.execute(cur, 'INSERT INTO finding_disposition(scan_id,batch_id,finding_id,file,review_item_id) VALUES(%s,%s,%s,%s,%s)', ('scan', new['batch_id'], 'finding', 'a.html', 'review'))
    assert read_waterfall(s, 'owner', 'scan', batch)['models'] == []
    current = read_waterfall(s, 'owner', 'scan', new['batch_id'])
    assert current['models'][0]['model'] == 'new-model'
    assert current['proposal_model_scope'] == 'current_scan_proposals'


def test_stage_models_require_same_run_owner_scan_and_dispatched_attempt(isolated_store):
    from ai_attempt_history import AttemptHistory
    s = isolated_store
    batch = seed(s)
    ledger = BudgetLedger(s._db)
    history = AttemptHistory(s._db)
    operation = 'd' * 64
    def attempt(tier, retry, provider, model, state='settled', scan='scan'):
        aid = f'text:{operation}:{tier}:{retry}'
        ledger.reserve('owner', batch, aid, 1000, 'private-pricing')
        history.begin('owner', scan, batch, operation, aid, file='a.html',
                      input_sha256='b' * 64, model=model, provider=provider,
                      purpose='draft' if tier == 1 else 'fallback')
        if state != 'reserved':
            ledger.claim_dispatch('owner', batch, aid)
            if state == 'settled':
                ledger.settle('owner', batch, aid, 100)
        return aid
    attempt(1, 0, 'openai', 'gpt-recorded')
    attempt(1, 1, 'openai', 'gpt-recorded')
    attempt(2, 0, 'anthropic', 'claude-recorded', state='dispatched')
    attempt(2, 1, 'private', 'never-called', state='reserved')
    attempt(2, 2, 'private', 'wrong-scan', scan='another-scan')
    result = read_waterfall(s, 'owner', 'scan', batch)
    assert result['stages'][0]['models'] == [dict(provider='openai', model='gpt-recorded', recorded_attempts=2)]
    assert result['stages'][1]['models'] == [dict(provider='anthropic', model='claude-recorded', recorded_attempts=1)]
    assert 'never-called' not in json.dumps(result)
    assert 'wrong-scan' not in json.dumps(result)
    assert read_waterfall(s, 'owner', 'scan', 'different-run').get('stages', []) == []


def test_saved_ai_policy_is_allowlisted_and_bound_to_run(isolated_store):
    s = isolated_store
    batch = seed(s)
    with s._db.cursor() as cur:
        s._db.execute(cur, 'UPDATE ai_spending_run_policies SET policy_json=%s WHERE owner_id=%s AND run_id=%s',
                      (json.dumps({'ai': 1, 'quality_first': True, 'ai_zone': 'any',
                                   'secret': 'private-endpoint-and-key'}), 'owner', batch))
    view = read_waterfall(s, 'owner', 'scan', batch)
    assert view['saved_ai_policy'] == {'level': 1, 'quality_first': True, 'zone': 'any'}
    assert 'private-endpoint-and-key' not in json.dumps(view)
    assert 'saved_ai_policy' not in read_waterfall(s, 'owner', 'scan', 'other-batch')


def test_legacy_policy_does_not_invent_quality_mode(isolated_store):
    batch = seed(isolated_store)
    view = read_waterfall(isolated_store, 'owner', 'scan', batch)
    assert view['saved_ai_policy']['quality_first'] is None
