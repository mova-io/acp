"""Bound schema SQL without a background migrator or connection-pool admission."""
from __future__ import annotations

import math
import time


class SchemaDeadlineExceeded(TimeoutError):
    pass


class DeadlineConnection:
    def __init__(self, connection, deadline):
        object.__setattr__(self, '_connection', connection)
        object.__setattr__(self, '_deadline', deadline)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        setattr(self._connection, name, value)

    def remaining_ms(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise SchemaDeadlineExceeded('schema initialization deadline exceeded')
        return max(1, math.ceil(remaining * 1000))

    def cursor(self):
        return DeadlineCursor(self, self._connection.cursor())

    def commit(self):
        # Run deferred constraint work as an ordinary bounded statement before
        # entering COMMIT. PostgreSQL does not apply statement_timeout to all
        # durability waits inside COMMIT, so the process supervisor remains the
        # hard outer bound and a lost receipt is treated as an ambiguous failure.
        with self.cursor() as cur:
            cur.execute('SET CONSTRAINTS ALL IMMEDIATE')
            cur.execute('COMMIT')
        # Synchronize psycopg2's transaction bookkeeping after explicit COMMIT.
        # There is no remaining transaction or deferred work at this point.
        self._connection.commit()


class DeadlineCursor:
    def __init__(self, connection, cursor):
        self._connection = connection
        self._cursor = cursor

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self._cursor.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def execute(self, query, params=None):
        self._cursor.execute("SELECT set_config('statement_timeout', %s, false)",
                             (str(self._connection.remaining_ms()),))
        # Account for the configuration round trip as part of the whole attempt.
        self._connection.remaining_ms()
        return self._cursor.execute(query, params) if params is not None else self._cursor.execute(query)
