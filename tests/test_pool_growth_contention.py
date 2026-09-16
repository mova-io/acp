"""A real psycopg2 pool must return existing leases while a new socket opens."""
import threading
from types import SimpleNamespace

import pytest
import psycopg2
from psycopg2.extensions import TRANSACTION_STATUS_IDLE

import store


class Connection:
    def __init__(self):
        self.closed = False
        self.info = SimpleNamespace(transaction_status=TRANSACTION_STATUS_IDLE)

    def close(self):
        self.closed = True

    def rollback(self):
        pass


def test_cold_probe_checkout_does_not_block_existing_connection_return(monkeypatch):
    connecting, release, returned = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def connect(*args, **kwargs):
        conn = Connection()
        calls.append(conn)
        if len(calls) == 2:
            connecting.set()
            assert release.wait(3)
        return conn

    monkeypatch.setattr(psycopg2, 'connect', connect)
    adapter = store._PgAdapter('postgresql://offline')
    adapter._MAX_CONN = 3
    first = adapter._getconn(read_only=True)
    second, errors = [], []

    def checkout():
        try:
            second.append(adapter._getconn(read_only=True))
        except BaseException as exc:
            errors.append(exc)

    def return_first():
        adapter._putconn(first)
        returned.set()

    grower = threading.Thread(target=checkout)
    returner = threading.Thread(target=return_first)
    grower.start()
    try:
        assert connecting.wait(1)
        returner.start()
        responsive = returned.wait(.2)
    finally:
        release.set()
        grower.join(2)
        returner.join(2)
        if second:
            adapter._putconn(second[0])
        adapter._get_pool().closeall()
    assert not errors
    assert responsive, 'pool socket creation held bookkeeping lock and blocked lease return'
    gate = adapter._ensure_read_gate()
    assert gate.acquire(blocking=False) and gate.acquire(blocking=False)
    assert not gate.acquire(blocking=False)
    gate.release(); gate.release()


def test_pending_sockets_count_toward_the_existing_pool_cap(monkeypatch):
    from responsive_connection_pool import ResponsiveConnectionPool
    release, growing = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls = []

    def connect(*args, **kwargs):
        conn = Connection()
        with lock:
            calls.append(conn)
            number = len(calls)
            if number == 3:
                growing.set()
        if number > 1:
            assert release.wait(3)
        return conn

    monkeypatch.setattr(psycopg2, 'connect', connect)
    pool = ResponsiveConnectionPool(1, 3, 'postgresql://offline')
    first = pool.getconn()
    borrowed = []
    threads = [threading.Thread(target=lambda: borrowed.append(pool.getconn())) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        assert growing.wait(1)
        with pytest.raises(psycopg2.pool.PoolError):
            pool.getconn()
        assert len(calls) == 3
        pool.putconn(first)
        assert pool.getconn() is first  # a returned socket remains reusable during growth
    finally:
        release.set()
        for thread in threads:
            thread.join(2)
        pool.putconn(first)
        for conn in borrowed:
            pool.putconn(conn)
        pool.closeall()
    assert not pool._connecting
    assert all(conn.closed for conn in calls)


@pytest.mark.parametrize('error_type', [psycopg2.OperationalError, KeyboardInterrupt])
def test_failed_socket_growth_releases_reservation_and_admission(monkeypatch, error_type):
    calls = []

    def connect(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise error_type('offline socket failure')
        return Connection()

    monkeypatch.setattr(psycopg2, 'connect', connect)
    adapter = store._PgAdapter('postgresql://offline')
    adapter._MAX_CONN = 3
    first = adapter._getconn(read_only=True)
    with pytest.raises(error_type):
        adapter._getconn(read_only=True)
    assert not adapter._get_pool()._connecting
    replacement = adapter._getconn(read_only=True)
    token = store.DB_MUTATION_REQUEST.set(True)
    try:
        critical = adapter._getconn()
    finally:
        store.DB_MUTATION_REQUEST.reset(token)
    for conn in [first, replacement, critical]:
        adapter._putconn(conn)
    adapter._get_pool().closeall()


def test_shutdown_during_growth_closes_late_socket_and_releases_permit(monkeypatch):
    connecting, release = threading.Event(), threading.Event()
    connections, errors = [], []

    def connect(*args, **kwargs):
        conn = Connection()
        connections.append(conn)
        if len(connections) == 2:
            connecting.set()
            assert release.wait(3)
        return conn

    monkeypatch.setattr(psycopg2, 'connect', connect)
    adapter = store._PgAdapter('postgresql://offline')
    adapter._MAX_CONN = 3
    first = adapter._getconn(read_only=True)

    def checkout():
        try:
            adapter._getconn(timeout=.05, read_only=True)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=checkout)
    thread.start()
    try:
        assert connecting.wait(1)
        adapter._get_pool().closeall()
    finally:
        release.set()
        thread.join(2)
    assert len(errors) == 1 and isinstance(errors[0], psycopg2.pool.PoolError)
    assert all(conn.closed for conn in connections)
    assert not adapter._get_pool()._connecting
    with pytest.raises(psycopg2.pool.PoolError):
        adapter._putconn(first)
    gate = adapter._ensure_read_gate()
    assert gate.acquire(blocking=False) and gate.acquire(blocking=False)
    gate.release(); gate.release()


def test_keyed_checkout_reuses_pending_socket_without_blocking_returns(monkeypatch):
    from responsive_connection_pool import ResponsiveConnectionPool
    connecting, release = threading.Event(), threading.Event()
    connections, borrowed = [], []

    def connect(*args, **kwargs):
        conn = Connection()
        connections.append(conn)
        if len(connections) == 2:
            connecting.set()
            assert release.wait(3)
        return conn

    monkeypatch.setattr(psycopg2, 'connect', connect)
    pool = ResponsiveConnectionPool(1, 3, 'postgresql://offline')
    first = pool.getconn()
    threads = [threading.Thread(target=lambda: borrowed.append(pool.getconn('shared'))) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        assert connecting.wait(1)
        pool.putconn(first)
        assert len(connections) == 2
    finally:
        release.set()
        for thread in threads:
            thread.join(2)
    assert borrowed[0] is borrowed[1]
    pool.putconn(borrowed[0], 'shared')
    pool.closeall()
