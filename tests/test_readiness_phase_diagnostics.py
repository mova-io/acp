import asyncio
import json

import pytest
import readiness_phase_diagnostics as diag


@pytest.fixture
def diagnostics(monkeypatch):
    monkeypatch.setenv('ACP_READINESS_PHASE_DIAGNOSTICS', '1')
    monkeypatch.setattr(diag, '_start_reporter', lambda: None)
    monkeypatch.setattr(diag, 'ACTIVE', {})
    monkeypatch.setattr(diag, 'RATE_COUNT', 0)
    monkeypatch.setattr(diag, 'DIAGNOSTIC_FAILURES', 0)
    monkeypatch.setattr(diag, 'RATE_WINDOW', 0.0)
    clock = [100.0]
    monkeypatch.setattr(diag.time, 'monotonic', lambda: clock[0])
    events = []
    monkeypatch.setattr(diag.LOGGER, 'info', lambda value: events.append(json.loads(value)))
    return clock, events


@pytest.mark.parametrize('phase', ['vision_available', 'db_pool_checkout', 'request_dispatch'])
def test_pending_identifies_blocked_phase_without_payloads(diagnostics, phase):
    clock, events = diagnostics
    with diag.request('full'):
        with diag.phase(phase):
            clock[0] += 3
            diag._poll()
            assert events[-1]['phase'] == phase
            assert events[-1]['state'] == 'pending'
            assert events[-1]['elapsed_ms'] == 3000
    assert not diag.ACTIVE
    assert diag.CURRENT.get() is None
    assert all(set(e) == {'event', 'kind', 'request_id', 'phase', 'state', 'elapsed_ms', 'request_elapsed_ms', 'diagnostic_failures'} for e in events)


def test_disabled_passthrough_and_unknown_labels(diagnostics, monkeypatch):
    _, events = diagnostics
    monkeypatch.delenv('ACP_READINESS_PHASE_DIAGNOSTICS')
    with diag.request('full'):
        assert diag.run('db_query', lambda x: x, 'secret.sql') == 'secret.sql'
        assert not diag.ACTIVE
    assert events == []
    monkeypatch.setenv('ACP_READINESS_PHASE_DIAGNOSTICS', '1')
    with diag.request('full'):
        assert diag.run('secret.document', lambda: 42) == 42
    assert 'secret' not in json.dumps(events)


@pytest.mark.parametrize('error', [ValueError('secret credential'), asyncio.CancelledError('secret document')])
def test_failure_and_cancellation_cleanup(diagnostics, error):
    _, events = diagnostics
    with pytest.raises(type(error)) as caught:
        with diag.request('container'):
            with diag.phase('db_ping'):
                raise error
    assert caught.value is error
    assert not diag.ACTIVE
    assert diag.CURRENT.get() is None
    assert events[-1]['state'] == 'interrupted'
    assert 'secret' not in json.dumps(events)


def test_bounds_and_expiry_do_not_change_work(diagnostics, monkeypatch):
    clock, events = diagnostics
    monkeypatch.setattr(diag, 'MAX_ACTIVE', 1)
    with diag.request('full'):
        record = diag.CURRENT.get()
        with diag.request('container'):
            assert diag.CURRENT.get() is None
            assert diag.run('db_ping', lambda: 123) == 123
        assert diag.CURRENT.get() is record
        for _ in range(20):
            clock[0] += 3
            diag._poll()
        assert len([e for e in events if e['state'] == 'pending']) == diag.MAX_PENDING
        clock[0] += diag.MAX_SECONDS
        diag._poll()
        assert not diag.ACTIVE and record['closed']
        assert diag.run('db_query', lambda: 'result') == 'result'
    assert events[-1]['state'] == 'tracking_expired'


@pytest.mark.parametrize('limit', ['MAX_EVENTS', 'MAX_PROCESS_EVENTS'])
def test_log_budget(diagnostics, monkeypatch, limit):
    _, events = diagnostics
    monkeypatch.setattr(diag, limit, 3)
    for _ in range(4):
        with diag.request('full'):
            for _ in range(10):
                diag.run('db_query', lambda: None)
    assert len(events) == (12 if limit == 'MAX_EVENTS' else 3)


def test_asgi_dispatch_and_cancel_cleanup(diagnostics):
    clock, events = diagnostics
    async def blocked(scope, receive, send):
        clock[0] += 3
        diag._poll()
        assert events[-1]['phase'] == 'request_dispatch'
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(diag.ReadinessPhaseMiddleware(blocked)({'type': 'http', 'path': '/readyz'}, None, None))
    assert not diag.ACTIVE


def test_reporter_start_failure_preserves_request(monkeypatch):
    monkeypatch.setenv('ACP_READINESS_PHASE_DIAGNOSTICS', '1')
    monkeypatch.setattr(diag, 'REPORTER_STARTED', False)
    def fail(self):
        raise RuntimeError('cannot start thread')
    monkeypatch.setattr(diag.threading.Thread, 'start', fail)
    with diag.request('full'):
        assert diag.run('db_ping', lambda: 42) == 42
    assert not diag.ACTIVE
    assert not diag.REPORTER_STARTED


def test_real_full_route_vision_phase_and_response_compatibility(diagnostics, monkeypatch):
    import ai
    from test_readiness import _readyz
    clock, events = diagnostics
    def unavailable():
        clock[0] += 3
        diag._poll()
        assert events[-1]['phase'] == 'vision_available'
        return False
    monkeypatch.setattr(ai, 'vision_is_available', unavailable)
    monkeypatch.setattr(ai, 'vision_unavailable_reason', lambda: 'unavailable')
    with diag.request('full'):
        enabled_response = _readyz(monkeypatch, beat=None, local_pool=4, pdf_ok=True)
    monkeypatch.delenv('ACP_READINESS_PHASE_DIAGNOSTICS')
    monkeypatch.setattr(ai, 'vision_is_available', lambda: False)
    disabled_response = _readyz(monkeypatch, beat=None, local_pool=4, pdf_ok=True)
    assert enabled_response == disabled_response


def test_real_adapter_checkout_marker(diagnostics, monkeypatch):
    from test_db_mutation_reserve import _adapter
    clock, events = diagnostics
    adapter = _adapter(monkeypatch)
    pool = adapter._get_pool()
    original = pool.getconn
    def checkout():
        clock[0] += 3
        diag._poll()
        assert events[-1]['phase'] == 'db_pool_checkout'
        return original()
    monkeypatch.setattr(pool, 'getconn', checkout)
    with diag.request('full'):
        connection = adapter._getconn(read_only=True)
        adapter._putconn(connection)
    assert not pool.used


def test_logging_failure_count_is_visible_on_recovery(diagnostics, monkeypatch):
    _, events = diagnostics
    original = diag.LOGGER.info
    def fail(value):
        raise RuntimeError('secret logger error')
    with diag.request('full'):
        monkeypatch.setattr(diag.LOGGER, 'info', fail)
        assert diag.run('db_ping', lambda: 42) == 42
        monkeypatch.setattr(diag.LOGGER, 'info', original)
        diag.run('db_ping', lambda: 42)
    assert events[-1]['diagnostic_failures'] == 1
    assert 'secret' not in json.dumps(events)
