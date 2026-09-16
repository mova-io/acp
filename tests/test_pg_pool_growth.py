"""Real PostgreSQL sockets exercise pool bookkeeping and admission invariants."""
import os
import threading
import time
import uuid

import pytest
import psycopg2
from psycopg2.extensions import TRANSACTION_STATUS_IDLE

import store
from conftest import require_disposable_postgres


@pytest.fixture
def pg_adapter():
    url = os.environ.get('DATABASE_URL')
    if not url:
        if os.environ.get('ACP_REQUIRE_PG') == '1':
            pytest.fail('real PostgreSQL coverage requires DATABASE_URL')
        pytest.skip('needs disposable PostgreSQL')
    require_disposable_postgres(url)
    check = psycopg2.connect(url)
    try:
        require_disposable_postgres(url, conn=check)
    finally:
        check.close()
    adapter = store._PgAdapter(url)
    adapter._MAX_CONN = 3
    label = 'acp-pool-' + uuid.uuid4().hex
    adapter._ssl_kwargs['application_name'] = label
    yield adapter
    if adapter._pool is not None and not adapter._pool.closed:
        adapter._pool.closeall()
    # Only inspect this fixture's sessions; never touch unrelated server clients.
    observer = psycopg2.connect(url)
    try:
        for _ in range(20):
            with observer.cursor() as cur:
                cur.execute('SELECT count(*) FROM pg_stat_activity WHERE application_name=%s', (label,))
                remaining = cur.fetchone()[0]
            observer.commit()
            if remaining == 0:
                break
            time.sleep(.02)
    finally:
        observer.close()
    assert remaining == 0, 'fixture leaked PostgreSQL sockets'
    if adapter._pool is not None:
        assert not adapter._pool._connecting


def assert_permits_returned(adapter):
    ordinary = adapter._ensure_read_gate()
    assert ordinary.acquire(blocking=False) and ordinary.acquire(blocking=False)
    assert not ordinary.acquire(blocking=False)
    ordinary.release(); ordinary.release()
    assert adapter._mutation_gate.acquire(blocking=False)
    adapter._mutation_gate.release()


def test_real_socket_growth_does_not_block_connection_return(pg_adapter, monkeypatch):
    adapter = pg_adapter
    first = adapter._getconn(read_only=True)
    connecting, release, returned = threading.Event(), threading.Event(), threading.Event()
    actual_connect = psycopg2.connect
    second, errors = [], []

    def slow_connect(*args, **kwargs):
        conn = actual_connect(*args, **kwargs)
        connecting.set()
        if not release.wait(3):
            conn.close()
            raise AssertionError('test socket was not released')
        return conn

    monkeypatch.setattr(psycopg2, 'connect', slow_connect)

    def grow():
        try:
            second.append(adapter._getconn(read_only=True))
        except BaseException as exc:
            errors.append(exc)

    def give_back():
        adapter._putconn(first)
        returned.set()

    grower = threading.Thread(target=grow)
    returner = threading.Thread(target=give_back)
    grower.start()
    try:
        assert connecting.wait(1)
        returner.start()
        assert returned.wait(.3), 'opening another real socket blocked lease return'
    finally:
        release.set()
        grower.join(2); returner.join(2)
        monkeypatch.setattr(psycopg2, 'connect', actual_connect)
        for conn in second:
            with conn.cursor() as cur:
                cur.execute('SELECT 1')
                assert cur.fetchone()[0] == 1
            conn.commit()
            adapter._putconn(conn)
    assert not errors
    assert_permits_returned(adapter)


@pytest.mark.parametrize('error_type', [psycopg2.OperationalError, KeyboardInterrupt])
def test_real_pool_failed_or_cancelled_growth_releases_capacity(pg_adapter, monkeypatch, error_type):
    adapter = pg_adapter
    first = adapter._getconn(read_only=True)
    actual_connect = psycopg2.connect

    def fail(*args, **kwargs):
        if error_type is psycopg2.OperationalError:
            return actual_connect(*args, **dict(kwargs, dbname='missing_' + uuid.uuid4().hex))
        conn = actual_connect(*args, **kwargs)
        conn.close()  # interrupted driver call has not handed a socket to the pool
        raise error_type('controlled socket establishment interruption')

    monkeypatch.setattr(psycopg2, 'connect', fail)
    with pytest.raises(error_type):
        adapter._getconn(read_only=True)
    assert not adapter._get_pool()._connecting
    monkeypatch.setattr(psycopg2, 'connect', actual_connect)
    second = adapter._getconn(read_only=True)
    token = store.DB_MUTATION_REQUEST.set(True)
    try:
        critical = adapter._getconn()
    finally:
        store.DB_MUTATION_REQUEST.reset(token)
    with critical.cursor() as cur:
        cur.execute('SELECT 1')
        assert cur.fetchone()[0] == 1
    critical.commit()
    for conn in [first, second, critical]:
        adapter._putconn(conn)
    assert_permits_returned(adapter)


def test_real_pending_sockets_respect_maximum_and_reuse_returned_lease(pg_adapter, monkeypatch):
    adapter = pg_adapter
    first = adapter._getconn(read_only=True)
    pool = adapter._get_pool()
    actual_connect = psycopg2.connect
    release, both_started = threading.Event(), threading.Event()
    lock = threading.Lock()
    created, borrowed, errors = [], [], []

    def slow_connect(*args, **kwargs):
        conn = actual_connect(*args, **kwargs)
        with lock:
            created.append(conn)
            if len(created) == 2:
                both_started.set()
        if not release.wait(3):
            conn.close()
            raise AssertionError('test socket was not released')
        return conn

    monkeypatch.setattr(psycopg2, 'connect', slow_connect)

    def grow():
        try:
            borrowed.append(pool.getconn())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=grow) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        assert both_started.wait(1)
        with pytest.raises(psycopg2.pool.PoolError):
            pool.getconn()
        assert len(created) == 2  # plus the first socket: existing max is three
        adapter._putconn(first)
        reused = adapter._getconn(read_only=True)
        assert reused is first
        adapter._putconn(reused)
    finally:
        release.set()
        for thread in threads:
            thread.join(2)
        monkeypatch.setattr(psycopg2, 'connect', actual_connect)
        for conn in borrowed:
            pool.putconn(conn)
    assert not errors
    assert not pool._connecting
    assert_permits_returned(adapter)


def test_real_shutdown_racing_growth_and_return_closes_late_socket(pg_adapter, monkeypatch):
    adapter = pg_adapter
    first = adapter._getconn(read_only=True)
    actual_connect = psycopg2.connect
    connecting, release = threading.Event(), threading.Event()
    created, errors = [], []

    def slow_connect(*args, **kwargs):
        conn = actual_connect(*args, **kwargs)
        created.append(conn)
        connecting.set()
        if not release.wait(3):
            conn.close()
            raise AssertionError('test socket was not released')
        return conn

    monkeypatch.setattr(psycopg2, 'connect', slow_connect)

    def grow():
        try:
            adapter._getconn(timeout=.05, read_only=True)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=grow)
    thread.start()
    try:
        assert connecting.wait(1)
        adapter._get_pool().closeall()
        with pytest.raises(psycopg2.pool.PoolError):
            adapter._putconn(first)
    finally:
        release.set(); thread.join(2)
        monkeypatch.setattr(psycopg2, 'connect', actual_connect)
    assert len(errors) == 1 and isinstance(errors[0], psycopg2.pool.PoolError)
    assert all(conn.closed for conn in created)
    assert first.closed
    assert_permits_returned(adapter)


def test_real_keyed_checkout_reuses_socket_and_rollback_leaves_it_healthy(pg_adapter):
    adapter = pg_adapter
    pool = adapter._get_pool()
    conn = pool.getconn('shared')
    assert pool.getconn('shared') is conn
    with conn.cursor() as cur:
        cur.execute('CREATE TEMP TABLE acp_rollback_probe (value int)')
        cur.execute('INSERT INTO acp_rollback_probe VALUES (1)')
    # inherited putconn must roll back an unfinished transaction before reuse
    pool.putconn(conn, 'shared')
    healthy = pool.getconn()
    assert healthy is conn
    assert healthy.info.transaction_status == TRANSACTION_STATUS_IDLE
    with healthy.cursor() as cur:
        cur.execute("SELECT to_regclass('pg_temp.acp_rollback_probe')")
        assert cur.fetchone()[0] is None
        cur.execute('SELECT 1')
        assert cur.fetchone()[0] == 1
    healthy.commit()
    pool.putconn(healthy)
    assert_permits_returned(adapter)
