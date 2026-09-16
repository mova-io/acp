"""HTTP timing is measured separately from validation, GPU work and settled billing."""
import json
import math
import statistics
from ai_request_activity import _identifier

MAX_EVENTS = 2000


def read_transport_performance(db, owner, scan_id, run_id, *, provider=None, model=None):
    try:
        with db.cursor() as cur:
            db.execute(cur, """SELECT detail FROM scan_events WHERE owner_email=%s AND scan_id=%s
                AND kind='remediate.ai_request_finished' ORDER BY seq DESC LIMIT %s""",
                       (owner, scan_id, MAX_EVENTS + 1))
            records = db.fetchall(cur)
    except Exception:
        result = aggregate_transport_performance([], run_id=run_id, complete=False)
        result.update(available=False, reason='transport_history_unavailable')
        return result
    # run_id lives in the safe transport identity; apply it before admitting metrics.
    return aggregate_transport_performance(records[:MAX_EVENTS], run_id=run_id, provider=provider,
                                           model=model, complete=len(records) <= MAX_EVENTS)


def aggregate_transport_performance(records, *, run_id, provider=None, model=None, complete=True):
    groups, seen = {}, set()
    for row in records:
        try:
            detail = json.loads(row['detail']) if isinstance(row.get('detail'), str) else row.get('detail')
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict) or detail.get('run_id') != run_id:
            continue
        if provider and detail.get('provider') != provider or model and detail.get('model') != model:
            continue
        request_id = detail.get('request_id')
        if not isinstance(request_id, str) or not request_id or request_id in seen:
            continue
        if (not _identifier(detail.get('provider')) or not _identifier(detail.get('model'))
                or not isinstance(detail.get('processing_zone'), str)
                or detail.get('processing_zone') not in {'local', 'cloud', 'tenant', 'unknown'}
                or not isinstance(detail.get('surface'), str)
                or detail.get('surface') not in {'vision', 'text'}):
            continue
        seen.add(request_id)
        key = (detail.get('provider'), detail.get('model'), detail.get('processing_zone'), detail.get('surface'))
        group = groups.setdefault(key, {'provider': key[0], 'model': key[1], 'processing_zone': key[2],
            'surface': key[3], 'finished_requests': 0, 'http_responses_received': 0, 'failed_requests': 0,
            'duration_unavailable': 0, '_durations': []})
        group['finished_requests'] += 1
        status = detail.get('status')
        if status == 'response_received':
            group['http_responses_received'] += 1
        elif status == 'failed':
            group['failed_requests'] += 1
        elapsed = detail.get('transport_elapsed_ms')
        if (detail.get('timing_basis') == 'http_transport_round_trip'
                and type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0):
            group['_durations'].append(elapsed)
        else:
            group['duration_unavailable'] += 1
    rows = []
    for group in groups.values():
        samples = sorted(group.pop('_durations'))
        group.update(measured_requests=len(samples),
                     average_ms=statistics.mean(samples) if samples else None,
                     median_ms=statistics.median(samples) if samples else None,
                     p95_ms=samples[math.ceil(len(samples) * .95) - 1] if samples else None)
        rows.append(group)
    return {'contract_version': 'ai-transport-performance.v1', 'available': True, 'complete': complete,
            'scope': 'authenticated remediation run',
            'record_limit': MAX_EVENTS, 'basis': 'Monotonic HTTP round-trip duration; includes network and provider queueing. HTTP success is not a usable suggestion.',
            'rows': sorted(rows, key=lambda row: str((row['provider'], row['model'], row['processing_zone'], row['surface']))),
            'gpu_timing': None, 'gpu_timing_reason': 'GPU execution was not independently measured.',
            'retry_count': None, 'retry_count_reason': 'Transport requests alone do not identify retries of the same operation.'}
