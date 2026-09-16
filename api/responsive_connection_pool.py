"""Keep slow socket creation outside psycopg2 pool bookkeeping.

The adapter's admission gates and this pool's pending reservations retain the
existing physical connection cap. Connection failures still reach the caller.
"""
import threading

import psycopg2
from psycopg2.pool import PoolError, ThreadedConnectionPool


class ResponsiveConnectionPool(ThreadedConnectionPool):
    def __init__(self, *args, **kwargs):
        self._connecting = {}
        super().__init__(*args, **kwargs)

    def getconn(self, key=None):
        # psycopg2's public getconn holds _lock across _connect. Its private
        # bookkeeping fields are reused deliberately; real-library regressions
        # cover their contract and the unchanged putconn/closeall behavior.
        while True:
            with self._lock:
                if self.closed:
                    raise PoolError('connection pool is closed')
                if key is None:
                    key = self._getkey()
                if key in self._used:
                    return self._used[key]
                pending = self._connecting.get(key)
                if pending is None:
                    if self._pool:
                        conn = self._pool.pop()
                        self._used[key] = conn
                        self._rused[id(conn)] = key
                        return conn
                    if len(self._used) + len(self._connecting) >= self.maxconn:
                        raise PoolError('connection pool exhausted')
                    pending = threading.Event()
                    self._connecting[key] = pending
                    break
            # Preserve keyed-lease reuse without parking under the pool lock.
            pending.wait()

        conn = None
        try:
            conn = psycopg2.connect(*self._args, **self._kwargs)
            with self._lock:
                closed = self.closed
                if not closed:
                    self._used[key] = conn
                    self._rused[id(conn)] = key
                self._connecting.pop(key).set()
            if closed:
                conn.close()
                raise PoolError('connection pool is closed')
            return conn
        finally:
            with self._lock:
                pending = self._connecting.pop(key, None)
                if pending is not None:
                    pending.set()
