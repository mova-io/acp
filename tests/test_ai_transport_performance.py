import json

from ai_transport_performance import aggregate_transport_performance, read_transport_performance
from test_scan_history_route import _seed


def event(request, elapsed=None, *, run='run', status='response_received', model='local-model', basis='http_transport_round_trip'):
    return {'detail': json.dumps({'request_id': request, 'run_id': run, 'provider': 'ollama', 'model': model,
            'processing_zone': 'local', 'surface': 'vision', 'status': status,
            'transport_elapsed_ms': elapsed, 'timing_basis': basis})}


def test_quantiles_measure_round_trips_include_failures_and_deduplicate_requests():
    result = aggregate_transport_performance([event('1', 100), event('2', 300), event('3', 500, status='failed'), event('2', 300)], run_id='run')
    row = result['rows'][0]
    assert row['finished_requests'] == 3
    assert row['measured_requests'] == 3
    assert row['average_ms'] == row['median_ms'] == 300
    assert row['p95_ms'] == 500 and row['failed_requests'] == 1
    assert result['gpu_timing'] is None and result['retry_count'] is None


def test_missing_legacy_or_invalid_duration_is_unknown_not_zero_or_success():
    result = aggregate_transport_performance([event('old'), event('bad', -1), event('clock', 10, basis='wall_clock'), event('bool', True)], run_id='run')
    row = result['rows'][0]
    assert row['measured_requests'] == 0 and row['duration_unavailable'] == 4
    assert row['median_ms'] is None and row['p95_ms'] is None


def test_other_run_and_model_do_not_enter_selected_metrics():
    result = aggregate_transport_performance([event('a', 10), event('b', 50, run='foreign'), event('c', 100, model='other')], run_id='run', model='local-model')
    assert result['rows'][0]['finished_requests'] == 1
    assert result['rows'][0]['average_ms'] == 10


def test_query_is_owner_scoped_and_never_returns_document_content(isolated_store):
    store = isolated_store
    _seed(store, 'scan')
    for owner, request, duration in [('demo', 'mine', 10), ('other', 'foreign', 900)]:
        detail = json.loads(event(request, duration)['detail'])
        detail.update(prompt='private document text')
        store.append_scan_event('scan', 'remediate.ai_request_finished', owner_email=owner, document='private.pdf', detail=detail)
    result = read_transport_performance(store._db, 'demo', 'scan', 'run')
    assert result['rows'][0]['average_ms'] == 10
    assert 'private' not in json.dumps(result)


def test_malformed_labels_do_not_escape_as_content_or_break_metrics():
    malformed = event('invalid', 10)
    detail = json.loads(malformed['detail'])
    detail['provider'] = ['private content']
    malformed['detail'] = json.dumps(detail)
    result = aggregate_transport_performance([malformed, {'detail': 'not json'}], run_id='run')
    assert result['rows'] == []
