"""Bounded read-only drawer charts. No prompts, outputs or filenames leave this view."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import statistics

MAX_RECORDS = 1000
STAGES = frozenset(('primary', 'fallback_1', 'fallback_2', 'review', 'final_review'))


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def read_metrics(store, owner, scan_id, run_id, *, stage=None, provider=None, model=None):
    if stage is not None and stage not in STAGES:
        raise ValueError('unsupported chart stage')
    if any(value is not None and (not isinstance(value, str) or not value or len(value) > 256) for value in (provider, model)):
        raise ValueError('invalid model filter')
    execution = store.get_stage_execution(run_id, owner=owner) if owner else None
    if (not execution or execution.get('owner_email') != owner or execution.get('scan_id') != scan_id
            or execution.get('stage') != 'remediate' or store.get_scan(scan_id, owner=owner) is None):
        raise LookupError('remediation execution not found')
    db = store._db
    with db.cursor() as cur:
        db.execute(cur, '''SELECT h.attempt_id,h.provider,h.model,h.purpose,h.status,h.created_at,h.updated_at,h.result_json,
            a.state AS spending_state,a.actual_cost_units,a.max_cost_units,
            (SELECT MIN(c.timing) FROM ai_attempt_trace_links l JOIN ai_calls c ON c.id=l.trace_call_id
             WHERE l.owner_id=h.owner_id AND l.run_id=h.run_id AND l.attempt_id=h.attempt_id
               AND c.scan_id=h.scan_id AND c.file=h.file AND c.provider=h.provider AND c.model=h.model
             HAVING COUNT(*)=1) AS measured_timing FROM ai_attempt_history h
            LEFT JOIN ai_spending_attempts a ON a.owner_id=h.owner_id AND a.run_id=h.run_id AND a.attempt_id=h.attempt_id
            WHERE h.owner_id=%s AND h.scan_id=%s AND h.run_id=%s ORDER BY h.created_at DESC,h.attempt_id DESC LIMIT %s''',
            (owner, scan_id, run_id, MAX_RECORDS + 1))
        records = db.fetchall(cur)
        db.execute(cur, 'SELECT currency FROM ai_spending_budgets WHERE owner_id=%s AND run_id=%s', (owner, run_id))
        currency = (db.fetchone(cur) or {}).get('currency')
        db.execute(cur, 'SELECT COUNT(*) AS n FROM ai_spending_attempts WHERE owner_id=%s AND run_id=%s', (owner, run_id))
        ledger_count = (db.fetchone(cur) or {}).get('n', 0)
    result = aggregate_metrics(records[:MAX_RECORDS], stage=stage, provider=provider, model=model,
        now=datetime.now(timezone.utc), terminal=execution.get('state') in ('completed', 'succeeded', 'failed', 'cancelled'),
        complete=len(records) <= MAX_RECORDS, currency=currency, ledger_complete=ledger_count == len(records))
    from ai_local_activity import read_local_activity
    result['local_activity'] = read_local_activity(db, owner, scan_id, run_id)
    from ai_transport_performance import read_transport_performance
    result['transport_timing'] = read_transport_performance(db, owner, scan_id, run_id, provider=provider, model=model)
    result['transport_timing']['generation_stage'] = None
    result['transport_timing']['generation_stage_reason'] = 'Transport timing is run-wide; individual generation steps were not attributed.'
    result.update(scan_id=scan_id, run_id=run_id, batch_id=run_id)
    result['revision'] = hashlib.sha256(json.dumps({k:v for k,v in result.items() if k != 'generated_at'}, sort_keys=True).encode()).hexdigest()[:20]
    return result


def aggregate_metrics(records, *, stage=None, provider=None, model=None, now, terminal=False,
                      complete=True, currency=None, ledger_complete=True):
    selected, missing_lineage = [], False
    for row in records:
        try:
            result = json.loads(row.get('result_json') or '{}')
            result = result if isinstance(result, dict) else {}
        except (TypeError, ValueError):
            result = {}
        lineage = result.get('execution') or {}
        lineage = lineage if isinstance(lineage, dict) else {}
        position = lineage.get('generation_position')
        step = lineage.get('step_id')
        valid = step in ('primary', 'fallback_1', 'fallback_2') and type(position) is int and position == ('primary', 'fallback_1', 'fallback_2').index(step)
        key = step if valid else row.get('purpose') if row.get('purpose') in ('review', 'final_review') else None
        if key is None:
            missing_lineage = True
        if stage and key != stage or provider and row.get('provider') != provider or model and row.get('model') != model:
            continue
        selected.append({**row, 'validation': result.get('validation_outcome'),
                         'prompt_tokens': result.get('prompt_tokens'),
                         'completion_tokens': result.get('completion_tokens')})
    covered = complete and not (stage and missing_lineage)
    completions = [r for r in selected if r.get('status') != 'started' and r.get('spending_state') == 'settled']
    dated = [(r, _time(r.get('updated_at'))) for r in completions]
    timestamps_complete = all(t is not None for _, t in dated)
    dated = [(r, t) for r, t in dated if t is not None]
    end = max((t for _, t in dated), default=now) if terminal else now
    start = end - timedelta(minutes=10)
    starts = [_time(r.get('created_at')) for r in selected]
    valid_starts = [t for t in starts if t is not None]
    observed = max(0, (end - min(valid_starts)).total_seconds()) if valid_starts else 0
    rate_available = covered and timestamps_complete and observed >= 60
    points = []
    for i in range(10):
        lower, upper = start + timedelta(minutes=i), start + timedelta(minutes=i+1)
        value = sum(lower < t <= upper for _, t in dated) if covered and timestamps_complete and valid_starts and lower >= min(valid_starts) else None
        points.append({'timestamp': upper.isoformat(), 'value': value})
    costs, unknown_cost = {}, False
    for row in selected:
        if row.get('spending_state') != 'settled':
            continue
        amount = row.get('actual_cost_units')
        if type(amount) is not int or amount < 0 or not row.get('provider') or not row.get('model'):
            unknown_cost = True
            continue
        key = (row['provider'], row['model'])
        costs[key] = costs.get(key, 0) + amount
    spend_complete = covered and ledger_complete and not unknown_cost and currency is not None
    cost_rows = [{'id': p + ':' + m, 'label': p + ' · ' + m, 'value': n / 1_000_000} for (p,m),n in sorted(costs.items(), key=lambda item: -item[1])]
    if len(cost_rows) > 6:
        cost_rows = cost_rows[:5] + [{'id':'other','label':'Other','value':sum(r['value'] for r in cost_rows[5:])}]
    usable = sum(r.get('validation') == 'usable' for r in completions)
    no_output = sum(r.get('validation') != 'usable' and r.get('status') in ('empty_response','unusable_response','refused') for r in completions)
    unknown = len(completions) - usable - sum(r.get('validation') != 'usable' and r.get('status') in ('empty_response','unusable_response','refused') for r in completions)
    model_rows = {}
    durations = []
    for row in selected:
        if row.get('spending_state') in ('reserved', 'released') or not row.get('provider') or not row.get('model'):
            continue
        identity = (row['provider'], row['model'])
        item = model_rows.setdefault(identity, {'id': ':'.join(identity), 'label': ' · '.join(identity),
            'value': 0, 'input_tokens': 0, 'output_tokens': 0, 'tokens_complete': True,
            'completed': 0, 'active': 0, 'timed_attempts': 0, 'duration_seconds': 0,
            'durations': [], 'usable_outputs': 0, 'no_usable_output': 0,
            'validation_unavailable': 0, 'provider_timing': {}})
        item['value'] += 1
        item['active'] += row.get('status') == 'started' and row.get('spending_state') == 'dispatched'
        if row.get('status') == 'started':
            continue
        item['completed'] += 1
        from ollama_runtime import safe_timings
        try:
            measured = safe_timings(json.loads(row.get('measured_timing') or '{}'))
        except (TypeError, ValueError):
            measured = {}
        for field, value in measured.items():
            item['provider_timing'].setdefault(field, []).append(value)
        # Match settled contribution outcomes; uncertain usage is not validation.
        if row.get('spending_state') == 'settled':
            if row.get('validation') == 'usable':
                item['usable_outputs'] += 1
            elif row.get('status') in ('empty_response', 'unusable_response', 'refused'):
                item['no_usable_output'] += 1
            else:
                item['validation_unavailable'] += 1
        else:
            item['validation_unavailable'] += 1
        for source, target in (('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens')):
            tokens = row.get(source)
            if type(tokens) is int and tokens >= 0:
                item[target] += tokens
            else:
                item['tokens_complete'] = False
        begin, finish = _time(row.get('created_at')), _time(row.get('updated_at'))
        if begin is not None and finish is not None and finish >= begin:
            duration = (finish - begin).total_seconds()
            durations.append(duration)
            item['duration_seconds'] += duration
            item['durations'].append(duration)
            item['timed_attempts'] += 1
    for item in model_rows.values():
        item['provider_timing'] = {field: {'average': sum(values) / len(values), 'measured_attempts': len(values)}
                                   for field, values in item['provider_timing'].items()}
        samples = sorted(item.pop('durations'))
        item['median_seconds'] = statistics.median(samples) if samples else None
        item['p95_seconds'] = samples[math.ceil(len(samples) * .95) - 1] if samples else None
        known = item['usable_outputs'] + item['no_usable_output']
        item['validated_attempts'] = known
        item['usable_percent'] = 100 * item['usable_outputs'] / known if known else None
        item['quality_complete'] = covered and item['validation_unavailable'] == 0
        item['average_seconds'] = item.pop('duration_seconds') / item['timed_attempts'] if item['timed_attempts'] else None
        if not item['tokens_complete'] or not item['completed']:
            item['input_tokens'] = item['output_tokens'] = None
    reason = None if covered else 'Complete stage attribution is unavailable for this retained record window.'
    return {'contract_version':'waterfall-drawer-metrics.v1', 'generated_at':now.isoformat(),
        'scope':{'stage':stage,'provider':provider,'model':model}, 'complete':covered, 'record_limit':MAX_RECORDS,
        'mode':'recorded' if terminal else 'live',
        'models': {'title': 'Recorded calls by model', 'unit': 'attempts',
                   'basis': 'Recorded dispatches, including retries; reservations are excluded.',
                   'complete': covered, 'rows': sorted(model_rows.values(), key=lambda r: (-r['value'], r['label'])),
                   'reason': reason},
        'timing': {'average_seconds': sum(durations) / len(durations) if durations else None,
                   'measured_attempts': len(durations), 'basis': 'Attempt lifecycle: dispatch, response, validation and settlement; not pure model latency.'},
        'pace':{'value':sum(end-timedelta(seconds=60) < t <= end for _,t in dated) if rate_available else None,
                'unit':'recorded completions/min','windowLabel':'Final recorded 60 seconds' if terminal else 'Last 60 seconds',
                'observedSeconds':observed,'reason':reason or (None if rate_available else 'Collecting pace data; a complete 60-second observation is required.')},
        'trend':{'unit':'recorded completions/min','windowLabel':'Final recorded 10 minutes' if terminal else 'Last 10 minutes',
                 'bucketLabel':'60-second buckets','timeZone':'UTC','points':points,'reason':reason},
        'contribution':{'title':'Recorded attempt outcomes','unit':'attempts','basis':'Settled attempts; usable validation is not a count of proposals or findings.',
            'rows': [{'id':'usable','label':'Validated usable', 'value':usable}, {'id':'no_output','label':'No usable output','value':no_output},
                     {'id':'unknown','label':'Validation unavailable','value':unknown}] if covered else [], 'reason':reason},
        'spend':{'unit':currency,'complete':spend_complete,'partition':True,'total':sum(r['value'] for r in cost_rows) if spend_complete else None,
                 'rows':cost_rows,'reason':None if spend_complete else 'Complete model-linked settled charges are unavailable. Reservations are excluded.'}}
