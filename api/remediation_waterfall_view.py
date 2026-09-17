"""Read-only, owner-scoped presentation of durable waterfall activity.

A settled provider charge is NOT a usable suggestion. The current ledger records
attempt identity and cost; immutable history supplies recorded model identities.
Keep that contribution explicitly unavailable instead of deriving it from calls.
"""
import datetime as dt
import hashlib
import json
import math
import re

from remediation_run_graph import read_run_graph

_TEXT_ATTEMPT = re.compile(r'^text:([0-9a-f]{64}):([12]):([0-9]+)$')


def read_waterfall(store, owner, scan_id, batch_id):
    if store.get_scan(scan_id, owner=owner) is None:
        raise LookupError('scan not found')
    db = store._db
    # One SELECT supplies policy, costs and attempts from the same database snapshot.
    # No ledger admission, provider call, source content, prompt hash or pricing ref is exposed.
    with db.cursor() as cur:
        db.execute(cur, """SELECT p.policy_json,b.cap_units,b.currency,
            a.attempt_id,a.state,a.max_cost_units,a.actual_cost_units,h.provider,h.model
            FROM ai_spending_run_policies p
            JOIN scan_runs s ON s.id=p.scan_id AND s.owner_email=p.owner_id
            JOIN stage_executions e ON e.execution_id=p.run_id AND e.scan_id=p.scan_id
                AND e.owner_email=p.owner_id AND e.stage='remediate'
            JOIN ai_spending_budgets b ON b.owner_id=p.owner_id AND b.run_id=p.run_id
            LEFT JOIN ai_spending_attempts a ON a.owner_id=p.owner_id AND a.run_id=p.run_id
            LEFT JOIN ai_attempt_history h ON h.owner_id=a.owner_id AND h.run_id=a.run_id
                AND h.attempt_id=a.attempt_id AND h.scan_id=p.scan_id
            WHERE p.owner_id=%s AND p.scan_id=%s AND p.run_id=%s""",
            (owner, scan_id, batch_id))
        rows = db.fetchall(cur)
    result = {'scan_id': scan_id, 'batch_id': batch_id,
              'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'available': bool(rows), 'contribution_available': False,
              'contribution_reason': 'AI step breakdown unavailable for this run. Recorded calls are not evidence of usable suggestions.',
              'reviewer_available': False,
              'proposal_model_scope': 'current_scan_proposals',
              'models': _proposal_models(store, scan_id, batch_id)}
    if rows:
        policy = json.loads(rows[0]['policy_json'])
        stages = {tier: {'tier': tier, 'operations': 0, 'active': 0, 'settled': 0,
                         'reserved': 0, 'uncertain': 0, 'released': 0, 'breached': 0,
                         'spent_units': 0, 'held_units': 0, 'models': []}
                  for tier in (1, 2)}
        operations = {1: set(), 2: set()}
        model_counts = {1: {}, 2: {}}
        spent = held = unknown = other = 0
        blocked = False
        for row in rows:
            if row['attempt_id'] is None:
                continue
            state = row['state']
            if state in ('settled', 'breached'):
                spent += row['actual_cost_units']
            if state in ('reserved', 'dispatched', 'uncertain'):
                held += row['max_cost_units']
            if state == 'uncertain':
                unknown += 1
            blocked |= state in ('uncertain', 'breached')
            match = _TEXT_ATTEMPT.fullmatch(row['attempt_id'])
            if not match:
                other += 1
                continue
            tier = int(match[2])
            operations[tier].add(match[1])
            # A reservation names a proposed model, not proof it was called.
            if row['provider'] and row['model'] and state not in ('reserved', 'released'):
                key = (row['provider'], row['model'])
                model_counts[tier][key] = model_counts[tier].get(key, 0) + 1
            stages[tier]['active' if state == 'dispatched' else state] += 1
            if state in ('settled', 'breached'):
                stages[tier]['spent_units'] += row['actual_cost_units']
            if state in ('reserved', 'dispatched', 'uncertain'):
                stages[tier]['held_units'] += row['max_cost_units']
        for tier in stages:
            stages[tier]['operations'] = len(operations[tier])
            stages[tier]['models'] = [dict(provider=provider, model=model, recorded_attempts=total)
                                      for (provider, model), total in sorted(model_counts[tier].items())]
        result.update(saved_ai_policy={
                          'quality_first': policy.get('quality_first') if type(policy.get('quality_first')) is bool else None,
                          'level': policy.get('ai') if type(policy.get('ai')) is int else None,
                          'zone': policy.get('ai_zone') if policy.get('ai_zone') in ('local', 'any') else None,
                      }, stages=list(stages.values()), other_attempts=other,
                      ai_enabled=policy['ai'] > 0 and rows[0]['cap_units'] > 0,
                      spending={'cap_units': rows[0]['cap_units'], 'currency': rows[0]['currency'],
                                'spent_units': spent, 'held_units': held,
                                'available_units': max(0, rows[0]['cap_units'] - spent - held),
                                'unknown_charges': unknown, 'blocked': blocked})
    execution = store.get_stage_execution(batch_id, owner=owner)
    if execution and execution.get('scan_id') == scan_id and execution.get('stage') == 'remediate':
        result['run_graph'] = read_run_graph(store, owner, scan_id, batch_id)
    result['revision'] = hashlib.sha256(json.dumps(
        {k: v for k, v in result.items() if k != 'generated_at'}, sort_keys=True).encode()).hexdigest()[:20]
    return result


def _proposal_models(store, scan_id, batch_id):
    """Actual call identities behind the current scan proposals, not run attribution.

    This is deliberately not a tier attribution or a claim that every attempt is
    linked. Queue proposals are mutable and can be inherited across runs. The
    caller labels this separately from run activity/cost and older batches receive
    no proposal models. Never guess from configuration, pricing URL, or call time.
    """
    db = store._db
    with db.cursor() as cur:
        db.execute(cur, """SELECT DISTINCT h.id,h.file,h.proposals FROM hitl_queue h
            JOIN finding_disposition d ON d.review_item_id=h.id AND d.scan_id=h.scan_id
                AND d.file=h.file WHERE d.scan_id=%s AND d.batch_id=%s
                AND d.batch_id=(SELECT execution_id FROM stage_executions WHERE scan_id=%s
                    AND stage='remediate' AND is_current=1
                    ORDER BY created_at DESC,execution_id DESC LIMIT 1)""",
                   (scan_id, batch_id, scan_id))
        linked = {}
        for row in db.fetchall(cur):
            try:
                proposals = json.loads(row['proposals'] or '[]')
            except (TypeError, ValueError):
                continue
            if not isinstance(proposals, list):
                continue
            for proposal in proposals:
                call_id = proposal.get('model_call_id') if isinstance(proposal, dict) else None
                if isinstance(call_id, str) and call_id:
                    linked.setdefault(call_id, set()).add(row['file'])
        if not linked:
            return []
        # Restrict before aggregation; an untrusted proposal cannot borrow a call
        # from another scan/file or multiply a call by citing it many times.
        models = {}
        ids = list(linked)
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ','.join(['%s'] * len(chunk))
            db.execute(cur, f"""SELECT id,file,provider,model,cost_usd FROM ai_calls
                WHERE scan_id=%s AND id IN ({marks})""", (scan_id, *chunk))
            for call in db.fetchall(cur):
                if call['file'] not in linked[call['id']] or not call['provider'] or not call['model']:
                    continue
                key = (call['provider'], call['model'])
                model = models.setdefault(key, {'provider': key[0], 'model': key[1],
                                                'linked_calls': 0, 'recorded_cost_usd': 0.0})
                model['linked_calls'] += 1
                cost = call['cost_usd']
                # Legacy cloud traces defaulted a missing charge to zero. Preserve
                # uncertainty; the separate budget ledger remains the cost authority.
                known = isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0
                if not known or (cost == 0 and call['provider'] != 'ollama'):
                    model['recorded_cost_usd'] = None
                elif model['recorded_cost_usd'] is not None:
                    model['recorded_cost_usd'] += cost
    return sorted(models.values(), key=lambda item: (item['provider'], item['model']))
