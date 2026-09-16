"""A dashboard GET burst must not starve mutating requests of every DB connection.

Production symptom (7 September 2026): PUT /hitl/queue returned 503 while simultaneous Live
Operations reads occupied the API replica's Postgres pool. Pool sizing alone cannot provide a
priority guarantee: however large the pool is, enough reads can consume all of it.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
import store  # noqa: E402


class _PoolError(Exception):
    pass


class _CapacityPool:
    """The relevant ThreadedConnectionPool behavior: exhaustion raises; it does not wait."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.used: set[object] = set()
        self.lock = threading.Lock()

    def getconn(self):
        with self.lock:
            if len(self.used) >= self.capacity:
                raise _PoolError("connection pool exhausted")
            conn = object()
            self.used.add(conn)
            return conn

    def putconn(self, conn):
        with self.lock:
            self.used.remove(conn)


def _adapter(monkeypatch, capacity: int = 3):
    import types

    fake_pool_module = types.ModuleType("psycopg2.pool")
    fake_pool_module.PoolError = _PoolError
    fake_pg = types.ModuleType("psycopg2")
    fake_pg.pool = fake_pool_module
    monkeypatch.setitem(sys.modules, "psycopg2", fake_pg)
    monkeypatch.setitem(sys.modules, "psycopg2.pool", fake_pool_module)

    adapter = store._PgAdapter.__new__(store._PgAdapter)
    adapter._MAX_CONN = capacity
    pool = _CapacityPool(capacity)
    monkeypatch.setattr(adapter, "_get_pool", lambda: pool)
    return adapter


def test_get_burst_leaves_a_physical_connection_for_mutation(monkeypatch):
    """Reads stop at pool-minus-reserve while a PUT-equivalent checkout still succeeds."""
    adapter = _adapter(monkeypatch, capacity=3)

    # Explicit classification is also useful for non-HTTP callers and makes the adapter contract
    # directly testable without manufacturing a FastAPI request.
    reads = [adapter._getconn(timeout=0.4, read_only=True) for _ in range(2)]
    started = threading.Event()
    finished = threading.Event()
    acquired: list[object] = []

    def third_read():
        started.set()
        acquired.append(adapter._getconn(timeout=0.5, read_only=True))
        finished.set()

    thread = threading.Thread(target=third_read)
    thread.start()
    assert started.wait(timeout=0.2)
    assert not finished.wait(timeout=0.1), "a third GET consumed the mutation reserve"

    mutation = adapter._getconn(timeout=0.1, read_only=False)
    assert mutation is not None

    adapter._putconn(mutation)
    adapter._putconn(reads.pop())
    assert finished.wait(timeout=0.3), "a returned read slot did not wake the queued GET"
    thread.join(timeout=0.2)

    adapter._putconn(acquired.pop())
    adapter._putconn(reads.pop())


def test_request_context_defaults_background_work_to_ordinary_gate(monkeypatch):
    """Only explicitly classified HTTP mutations may consume the critical reserve.

    ContextVar is important here: a module global would let concurrent GET and PUT requests
    overwrite each other's classification in FastAPI's thread pool.
    """
    adapter = _adapter(monkeypatch, capacity=3)
    assert store.DB_MUTATION_REQUEST.get() is False

    ordinary = [adapter._getconn(timeout=0.3) for _ in range(2)]
    token = store.DB_MUTATION_REQUEST.set(True)
    try:
        mutation = adapter._getconn(timeout=0.1)
    finally:
        store.DB_MUTATION_REQUEST.reset(token)

    adapter._putconn(mutation)
    for conn in ordinary:
        adapter._putconn(conn)


def test_release_pool_admits_three_handlers_and_both_heartbeat_writers(monkeypatch):
    """The dedicated Release shape needs five ordinary permits plus the mutation reserve."""
    adapter = _adapter(monkeypatch, capacity=6)
    ordinary = [adapter._getconn(timeout=0.2) for _ in range(5)]
    token = store.DB_MUTATION_REQUEST.set(True)
    try:
        reserve = adapter._getconn(timeout=0.2)
    finally:
        store.DB_MUTATION_REQUEST.reset(token)

    assert len(adapter._get_pool().used) == 6
    adapter._putconn(reserve)
    for conn in ordinary:
        adapter._putconn(conn)


def test_overlapping_mutations_borrow_idle_ordinary_capacity(monkeypatch):
    """The reserve is a guaranteed floor, not a one-request mutation ceiling.

    POST /scans/{sid}/remediate overlaps naturally with other user decisions and performs several
    short DB transactions. #1783 initially routed every mutation through the single reserved
    permit, so a second mutation returned DB_CAPACITY_BUSY after five seconds even when both
    ordinary physical connections were idle.
    """
    adapter = _adapter(monkeypatch, capacity=3)
    token = store.DB_MUTATION_REQUEST.set(True)
    try:
        first = adapter._getconn(timeout=0.2)
        second = adapter._getconn(timeout=0.1)
    finally:
        store.DB_MUTATION_REQUEST.reset(token)

    assert first is not None and second is not None
    assert len(adapter._get_pool().used) == 2
    adapter._putconn(second)
    adapter._putconn(first)


def test_waiting_mutation_takes_reserve_when_its_holder_finishes(monkeypatch):
    """Waiting on ordinary capacity must not make a newly free reserve invisible."""
    adapter = _adapter(monkeypatch, capacity=3)
    token = store.DB_MUTATION_REQUEST.set(True)
    try:
        first_mutation = adapter._getconn(timeout=0.2)
    finally:
        store.DB_MUTATION_REQUEST.reset(token)
    reads = [adapter._getconn(timeout=0.2, read_only=True) for _ in range(2)]

    started = threading.Event()
    finished = threading.Event()
    acquired: list[object] = []

    def waiting_mutation():
        local_token = store.DB_MUTATION_REQUEST.set(True)
        try:
            started.set()
            acquired.append(adapter._getconn(timeout=0.4))
            finished.set()
        finally:
            store.DB_MUTATION_REQUEST.reset(local_token)

    thread = threading.Thread(target=waiting_mutation)
    thread.start()
    assert started.wait(timeout=0.2)
    assert not finished.wait(timeout=0.05)
    adapter._putconn(first_mutation)
    assert finished.wait(timeout=0.25), "mutation ignored the newly available reserved slot"
    thread.join(timeout=0.2)

    adapter._putconn(acquired.pop())
    for conn in reads:
        adapter._putconn(conn)


def test_mutation_reserve_is_bounded_and_never_zero():
    """Protect writes without serializing a read-heavy API."""
    assert 1 <= store._MUTATION_RESERVE_CONN < store._PgAdapter._MAX_CONN


def test_connection_returns_to_the_exact_pool_that_issued_it(monkeypatch):
    """Concurrent lazy initialization must not return a checkout to another pool.

    Two first-use threads can each construct a pool before one wins the ``self._pool`` race.
    The cursor retains its issuing pool, so returning the connection must use that reference
    instead of resolving ``self._pool`` again.
    """
    adapter = _adapter(monkeypatch, capacity=2)
    issuing_pool = _CapacityPool(2)
    replacement_pool = _CapacityPool(2)
    conn = issuing_pool.getconn()
    monkeypatch.setattr(adapter, "_get_pool", lambda: replacement_pool)

    adapter._putconn(conn, issuing_pool)

    assert conn not in issuing_pool.used
    assert replacement_pool.used == set()


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"),
                    reason="needs the Postgres integration service")
def test_real_postgres_pool_keeps_critical_mutation_capacity_isolated():
    """Exercise both admission classes against psycopg2's real ThreadedConnectionPool.

    This is non-destructive: the integration job supplies a disposable server, but the test only
    checks out connections and executes SELECT 1.
    """
    adapter = store._PgAdapter(os.environ["DATABASE_URL"])
    adapter._MAX_CONN = 3
    ordinary = [adapter._getconn(timeout=0.5) for _ in range(2)]
    token = store.DB_MUTATION_REQUEST.set(True)
    try:
        critical = adapter._getconn(timeout=0.5)
    finally:
        store.DB_MUTATION_REQUEST.reset(token)

    with critical.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1

    adapter._putconn(critical)
    for conn in ordinary:
        adapter._putconn(conn)
    adapter._get_pool().closeall()


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"),
                    reason="needs the Postgres integration service")
def test_real_release_pool_runs_five_ordinary_clients_and_keeps_the_reserve():
    """Reproduce the Release worker's three handlers plus its two DB heartbeat writers."""
    adapter = store._PgAdapter(os.environ["DATABASE_URL"])
    adapter._MAX_CONN = 6
    ordinary = [adapter._getconn(timeout=0.5) for _ in range(5)]
    token = store.DB_MUTATION_REQUEST.set(True)
    try:
        critical = adapter._getconn(timeout=0.5)
    finally:
        store.DB_MUTATION_REQUEST.reset(token)

    for conn in [*ordinary, critical]:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1

    adapter._putconn(critical)
    for conn in ordinary:
        adapter._putconn(conn)
    adapter._get_pool().closeall()
