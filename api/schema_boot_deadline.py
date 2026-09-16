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
        # COMMIT must also fit the remaining server-side statement budget.
        with self._connection.cursor() as cur:
            cur.execute("SELECT set_config('statement_timeout', %s, false)",
                        (str(self.remaining_ms()),))
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
