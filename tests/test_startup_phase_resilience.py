import json

import psycopg2
import psycopg2.pool
import pytest

import app as app_module
import capacity_reconcile
import core
import startup_phase


def test_phase_retries_only_transient_database_errors(capsys):
    calls = []

    def operation():
        calls.append(1)
        if len(calls) == 1:
            raise psycopg2.OperationalError('credential=must-not-be-logged')
        return 'ready'

    assert startup_phase.run('scheduler_reload', operation,
                             retry_transient_database=True, attempts=2,
                             delay_seconds=0, sleep=lambda _: None) == 'ready'
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(calls) == 2 and events[1]['will_retry'] is True
    assert 'credential' not in json.dumps(events)


@pytest.mark.parametrize('error', [ValueError('bad config'), RuntimeError('programming fault')])
def test_phase_never_retries_non_database_failures(error):
    calls = []
    with pytest.raises(type(error)):
        startup_phase.run('scheduler_reload',
                          lambda: calls.append(1) or (_ for _ in ()).throw(error),
                          retry_transient_database=True, attempts=2,
                          delay_seconds=0, sleep=lambda _: None)
    assert len(calls) == 1


class FakeStore:
    def __init__(self):
        self.receipts = {}

    def record_deployment_audit(self, **kwargs):
        return False

    def set_setting(self, key, value):
        self.receipts[key] = json.loads(value)


def configure_startup(monkeypatch, reload_operation):
    store = FakeStore()
    counts = {'scheduler_start': 0, 'capacity': 0, 'workers': 0}
    monkeypatch.setattr(core, 'store', store)
    monkeypatch.setattr(core, 'get_store', lambda: store)
    monkeypatch.setattr(core, 'reload_scheduler', reload_operation)
    monkeypatch.setattr(core, 'start_scheduler',
                        lambda: counts.__setitem__('scheduler_start', counts['scheduler_start'] + 1))
    monkeypatch.setattr(core, 'start_workers',
                        lambda: counts.__setitem__('workers', counts['workers'] + 1) or 0)
    monkeypatch.setattr(capacity_reconcile, 'start',
                        lambda *args: counts.__setitem__('capacity', counts['capacity'] + 1))
    monkeypatch.setattr(app_module, '_announce_isolation_mode', lambda: None)
    monkeypatch.setenv('CONTAINER_APP_REVISION', 'api--test')
    return store, counts


def test_full_startup_retries_one_scheduler_read_then_starts_each_side_effect_once(monkeypatch):
    attempts = []

    def reload():
        attempts.append(1)
        if len(attempts) == 1:
            raise psycopg2.pool.PoolError('temporary admission')

    store, counts = configure_startup(monkeypatch, reload)
    app_module._start_job_workers()
    assert len(attempts) == 2
    assert counts == {'scheduler_start': 1, 'capacity': 1, 'workers': 1}
    assert store.receipts == {}


def test_full_startup_persistent_transient_rethrows_and_retains_sanitized_receipt(monkeypatch):
    attempts = []

    def reload():
        attempts.append(1)
        raise psycopg2.OperationalError('dsn=secret')

    store, counts = configure_startup(monkeypatch, reload)
    with pytest.raises(psycopg2.OperationalError):
        app_module._start_job_workers()
    assert len(attempts) == 2
    assert counts == {'scheduler_start': 0, 'capacity': 0, 'workers': 0}
    assert store.receipts['startup_failure:api--test'] == {
        'revision': 'api--test', 'phase': 'scheduler_reload',
        'error_type': 'OperationalError', 'sqlstate': None}
    assert 'secret' not in json.dumps(store.receipts)


def test_full_startup_configuration_error_is_not_retried(monkeypatch):
    attempts = []

    def reload():
        attempts.append(1)
        raise ValueError('invalid interval')

    store, counts = configure_startup(monkeypatch, reload)
    with pytest.raises(ValueError):
        app_module._start_job_workers()
    assert len(attempts) == 1
    assert counts == {'scheduler_start': 0, 'capacity': 0, 'workers': 0}
    assert store.receipts['startup_failure:api--test']['error_type'] == 'ValueError'
