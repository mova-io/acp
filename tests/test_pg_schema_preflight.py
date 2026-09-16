"""Actual PostgreSQL lock admission, deadline rollback and serialized pinned migrations."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlparse, urlunparse
import uuid

import psycopg2
from psycopg2 import sql
import pytest

import store
from conftest import require_disposable_postgres

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/public'))
import schema_preflight as gate


@pytest.fixture
def disposable_database():
    root = os.environ.get('DATABASE_URL')
    if not root:
        if os.environ.get('ACP_REQUIRE_PG') == '1':
            pytest.fail('required PostgreSQL preflight validation has no DATABASE_URL')
        pytest.skip('requires disposable PostgreSQL')
    require_disposable_postgres(root)
    name = 'test_schema_preflight_' + uuid.uuid4().hex
    admin = psycopg2.connect(root)
    require_disposable_postgres(root, conn=admin)
    admin.rollback()
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    parsed = urlparse(root)
    url = urlunparse(parsed._replace(path='/' + name))
    try:
        yield url
    finally:
        with admin.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM pg_stat_activity WHERE datname=%s', (name,))
            assert cur.fetchone()[0] == 0, 'preflight leaked a migration session/process'
            # Never force-drop or terminate an unrelated client.
            cur.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
        admin.close()


def execute(url, query, params=()):
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            result = cur.fetchall() if cur.description else None
        conn.commit()
        return result
    finally:
        conn.close()


def previous_schema(url):
    store._PgAdapter(url).init_schema()
    for name in ('execution_context', 'execution_revision', 'scan_id', 'execution_deadline',
                 'execution_failures', 'execution_job_id'):
        execute(url, sql.SQL('ALTER TABLE schedule_occurrences DROP COLUMN {}').format(sql.Identifier(name)))
    execute(url, 'ALTER TABLE jobs DROP COLUMN scheduled_execution_binding')
    execute(url, 'DELETE FROM acp_schema_version WHERE version=57')
    execute(url, "INSERT INTO acp_schema_version(version,checksum) VALUES (56,'colliding-production-schema')")
    execute(url, "CREATE TABLE customer_probe(id INT PRIMARY KEY,value TEXT); INSERT INTO customer_probe VALUES (1,'keep')")


def assert_previous_intact(url):
    assert gate.schema_marker(url) == (56, 'colliding-production-schema')
    assert execute(url, 'SELECT value FROM customer_probe') == [('keep',)]
    assert execute(url, "SELECT COUNT(*) FROM information_schema.columns WHERE table_name='jobs' AND column_name='scheduled_execution_binding'") == [(0,)]
    assert execute(url, 'SELECT pg_try_advisory_lock(%s)', (store._PgAdapter._MIGRATION_ADVISORY_KEY,)) == [(True,)]
    # execute() closes its connection, releasing the session advisory lock.


def test_fresh_database_prepared_before_any_role_boot(disposable_database):
    url = disposable_database
    result = gate.prepare(url, ROOT / 'api')
    assert result['state'] == 'prepared' and result['observed_version'] == 57
    assert gate.schema_marker(url) == (57, store._PgAdapter._SCHEMA_CHECKSUM_AT_VERSION)


def test_equal_version_wrong_checksum_refused_without_replay(disposable_database, monkeypatch):
    url = disposable_database
    store._PgAdapter(url).init_schema()
    execute(url, "UPDATE acp_schema_version SET checksum='unexpected' WHERE version=57")
    monkeypatch.setattr(store._PgAdapter, '_apply_schema', lambda *args: pytest.fail('unexpected marker replayed DDL'))
    with pytest.raises(gate.PreflightRefused, match='target_schema_checksum_mismatch'):
        gate.prepare(url, ROOT / 'api')
    assert gate.schema_marker(url) == (57, 'unexpected')


def test_held_customer_reader_refused_without_ddl_or_cancel_then_release_succeeds(disposable_database):
    url = disposable_database
    previous_schema(url)
    reader = psycopg2.connect(url)
    try:
        with reader.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM jobs')
        started = time.monotonic()
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            gate.prepare(url, ROOT / 'api')
        assert time.monotonic() - started < 2
        # The held reader still owns its transaction and remains usable.
        with reader.cursor() as cur:
            cur.execute('SELECT value FROM customer_probe')
            assert cur.fetchone()[0] == 'keep'
        assert_previous_intact(url)
    finally:
        reader.close()
    assert gate.prepare(url, ROOT / 'api')['state'] == 'prepared'


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_active_durable_job_refused_and_unchanged(disposable_database, status):
    url = disposable_database
    previous_schema(url)
    execute(url, 'INSERT INTO jobs(id,type,status,payload) VALUES (%s,%s,%s,%s)', ('customer-job', 'scan_file', status, '{}'))
    with pytest.raises(gate.PreflightRefused, match='active_jobs'):
        gate.prepare(url, ROOT / 'api')
    assert execute(url, 'SELECT status,payload FROM jobs WHERE id=%s', ('customer-job',)) == [(status, '{}')]
    assert_previous_intact(url)


def test_new_job_after_commit_preserved_and_warm_old_and_current_boots_issue_no_ddl(disposable_database, monkeypatch):
    url = disposable_database
    previous_schema(url)
    gate.prepare(url, ROOT / 'api')
    execute(url, "INSERT INTO jobs(id,type,status,payload) VALUES ('new-after-commit','scan_file','queued','{}')")
    def forbidden(*args):
        raise AssertionError('prepared schema replayed DDL')
    monkeypatch.setattr(store._PgAdapter, '_apply_schema', forbidden)
    assert gate.prepare(url, ROOT / 'api')['state'] == 'current'
    store._PgAdapter(url).init_schema()
    class Older(store._PgAdapter):
        _SCHEMA_VERSION = 55
    Older(url).init_schema()
    assert execute(url, "SELECT status FROM jobs WHERE id='new-after-commit'") == [('queued',)]


def test_late_ddl_error_rolls_back_entire_attempt_and_marker(disposable_database, monkeypatch):
    url = disposable_database
    previous_schema(url)
    monkeypatch.setattr(store, '_SCHEMA', [*store._SCHEMA, 'CREATE TABLE doomed_preflight(value INT)', 'SELECT 1/0'])
    monkeypatch.setattr(store._PgAdapter, '_SCHEMA_CHECKSUM_AT_VERSION', store._PgAdapter._schema_checksum())
    with pytest.raises(psycopg2.errors.DivisionByZero):
        gate.prepare(url, ROOT / 'api')
    assert execute(url, "SELECT to_regclass('public.doomed_preflight')") == [(None,)]
    assert_previous_intact(url)


def test_whole_sql_deadline_cancels_running_statement_and_rolls_back(disposable_database, monkeypatch):
    url = disposable_database
    previous_schema(url)
    monkeypatch.setattr(store, '_SCHEMA', [*store._SCHEMA, 'SELECT pg_sleep(3)'])
    monkeypatch.setattr(store._PgAdapter, '_SCHEMA_CHECKSUM_AT_VERSION', store._PgAdapter._schema_checksum())
    started = time.monotonic()
    with pytest.raises((psycopg2.errors.QueryCanceled, TimeoutError)):
        gate.prepare(url, ROOT / 'api', timeout_seconds=.4)
    assert time.monotonic() - started < 2
    assert_previous_intact(url)


def test_advisory_admission_has_whole_deadline_and_no_migration(disposable_database):
    url = disposable_database
    previous_schema(url)
    blocker = psycopg2.connect(url)
    try:
        with blocker.cursor() as cur:
            cur.execute('SELECT pg_advisory_lock(%s)', (store._PgAdapter._MIGRATION_ADVISORY_KEY,))
        started = time.monotonic()
        with pytest.raises(psycopg2.errors.QueryCanceled):
            gate.prepare(url, ROOT / 'api', timeout_seconds=.25)
        assert time.monotonic() - started < 2
        assert gate.schema_marker(url) == (56, 'colliding-production-schema')
    finally:
        blocker.close()
    assert_previous_intact(url)


def test_deferred_commit_work_cancelled_and_transaction_rolled_back(disposable_database):
    from schema_boot_deadline import DeadlineConnection
    url = disposable_database
    execute(url, '''CREATE TABLE commit_probe(value INT);
        CREATE FUNCTION slow_deferred_commit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN PERFORM pg_sleep(3); RETURN NEW; END $$;
        CREATE CONSTRAINT TRIGGER slow_commit AFTER INSERT ON commit_probe
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION slow_deferred_commit()''')
    raw = psycopg2.connect(url)
    started = time.monotonic()
    try:
        bounded = DeadlineConnection(raw, started + .25)
        with bounded.cursor() as cur:
            cur.execute('INSERT INTO commit_probe VALUES (1)')
        with pytest.raises(psycopg2.errors.QueryCanceled):
            bounded.commit()
        raw.rollback()
    finally:
        raw.close()
    assert time.monotonic() - started < 2
    assert execute(url, 'SELECT * FROM commit_probe') == []


def test_concurrent_gate_candidates_commit_once_and_recheck_after_lock(disposable_database, monkeypatch):
    url = disposable_database
    previous_schema(url)
    barrier = threading.Barrier(2)
    local = threading.local()
    original_marker, original_apply = gate.schema_marker, store._PgAdapter._apply_schema
    def marker(dsn):
        value = original_marker(dsn)
        if not getattr(local, 'first', False):
            local.first = True
            barrier.wait(timeout=5)
        return value
    calls = []
    def apply(self, conn, checksum):
        calls.append(1)
        return original_apply(self, conn, checksum)
    monkeypatch.setattr(gate, 'schema_marker', marker)
    monkeypatch.setattr(store._PgAdapter, '_apply_schema', apply)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: gate.prepare(url, ROOT / 'api'), range(2)))
    assert all(r['observed_version'] == 57 for r in results) and len(calls) == 1


def hanging_migration_child(pipe, dsn, api_path, timeout_seconds):
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("UPDATE customer_probe SET value='must-rollback'")
        # Simulate a stuck client/receipt after a statement, retaining its transaction.
        # Running server-query cancellation is independently tested above.
        time.sleep(60)


def test_hard_process_timeout_stops_child_and_uncommitted_work(disposable_database, monkeypatch):
    url = disposable_database
    previous_schema(url)
    monkeypatch.setattr(gate, 'migration_child', hanging_migration_child)
    started = time.monotonic()
    with pytest.raises(gate.PreflightRefused, match='process_deadline'):
        gate.bounded_prepare(url, ROOT / 'api', timeout_seconds=2, receipt_grace_seconds=.2)
    assert time.monotonic() - started < 8
    assert_previous_intact(url)


def test_real_spawned_gate_receipt_verified_and_no_child_session_left(disposable_database):
    url = disposable_database
    result = gate.bounded_prepare(url, ROOT / 'api')
    assert result['ok'] and result['state'] == 'prepared'
