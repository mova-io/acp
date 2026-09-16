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


def test_earliest_store_failure_survives_process_restart_as_sanitized_receipt(
        monkeypatch, tmp_path):
    path = tmp_path / 'startup-failure.json'
    monkeypatch.setattr(app_module, '_STARTUP_FAILURE_PATH', str(path))
    store = FakeStore()
    attempts = []

    def fail_store():
        attempts.append(1)
        raise psycopg2.OperationalError('credential=must-not-survive')

    monkeypatch.setattr(core, 'get_store', fail_store)
    monkeypatch.setenv('CONTAINER_APP_REVISION', 'api--test')
    with pytest.raises(psycopg2.OperationalError):
        app_module._start_job_workers()
    assert len(attempts) == 1
    assert json.loads(path.read_text()) == {
        'revision': 'api--test', 'phase': 'store',
        'error_type': 'OperationalError', 'sqlstate': None}
    assert 'credential' not in path.read_text()

    monkeypatch.setattr(core, 'store', store)
    monkeypatch.setattr(core, 'get_store', lambda: store)
    monkeypatch.setattr(core, 'reload_scheduler', lambda: None)
    monkeypatch.setattr(core, 'start_scheduler', lambda: None)
    monkeypatch.setattr(core, 'start_workers', lambda: 0)
    monkeypatch.setattr(capacity_reconcile, 'start', lambda *_args: None)
    monkeypatch.setattr(app_module, '_announce_isolation_mode', lambda: None)
    app_module._start_job_workers()
    assert store.receipts['startup_failure:api--test']['phase'] == 'store'
    assert not path.exists()


def test_earliest_store_programming_failure_is_not_retried(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, '_STARTUP_FAILURE_PATH',
                        str(tmp_path / 'startup-failure.json'))
    attempts = []
    monkeypatch.setattr(core, 'get_store', lambda: attempts.append(1) or
                        (_ for _ in ()).throw(ValueError('bad configuration')))
    monkeypatch.setenv('CONTAINER_APP_REVISION', 'api--test')
    with pytest.raises(ValueError):
        app_module._start_job_workers()
    assert len(attempts) == 1


def test_diagnostic_replay_database_failure_cannot_crash_healthy_startup(
        monkeypatch, tmp_path):
    path = tmp_path / 'startup-failure.json'
    path.write_text(json.dumps({
        'revision': 'api--test', 'phase': 'store',
        'error_type': 'OperationalError', 'sqlstate': None}))
    monkeypatch.setattr(app_module, '_STARTUP_FAILURE_PATH', str(path))
    store = FakeStore()
    store.set_setting = lambda *_args: (_ for _ in ()).throw(
        psycopg2.OperationalError('diagnostic replay unavailable'))
    counts = {'scheduler': 0, 'workers': 0}
    monkeypatch.setattr(core, 'store', store)
    monkeypatch.setattr(core, 'get_store', lambda: store)
    monkeypatch.setattr(core, 'reload_scheduler',
                        lambda: counts.__setitem__('scheduler', counts['scheduler'] + 1))
    monkeypatch.setattr(core, 'start_scheduler', lambda: None)
    monkeypatch.setattr(core, 'start_workers',
                        lambda: counts.__setitem__('workers', counts['workers'] + 1) or 0)
    monkeypatch.setattr(capacity_reconcile, 'start', lambda *_args: None)
    monkeypatch.setattr(app_module, '_announce_isolation_mode', lambda: None)
    monkeypatch.setenv('CONTAINER_APP_REVISION', 'api--test')

    app_module._start_job_workers()

    assert counts == {'scheduler': 1, 'workers': 1}
    assert path.exists()  # Retained for the next process; never silently discarded.
