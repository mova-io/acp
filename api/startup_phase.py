"""Sanitized, bounded evidence for main-thread application startup phases."""
from __future__ import annotations

import json
import re
import time


def _error_fields(exc: BaseException) -> dict:
    error = type(exc).__name__
    state = getattr(exc, "pgcode", None)
    return {
        "error_type": error if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,59}", error) else "Exception",
        "sqlstate": state if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state) else None,
    }


def is_transient_database_error(exc: BaseException) -> bool:
    """Recognize connection/admission failures; never retry data or programming errors."""
    try:
        import psycopg2
        import psycopg2.pool
        return isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError,
                                psycopg2.pool.PoolError))
    except ImportError:
        return False


def run(name: str, operation, *, retry_transient_database: bool = False,
        attempts: int = 1, delay_seconds: float = .25, sleep=time.sleep,
        on_final_failure=None):
    """Run one startup phase with allowlisted evidence and narrowly scoped retry."""
    if not re.fullmatch(r"[a-z_]{1,60}", name):
        raise ValueError("startup phase name must be allowlisted")
    attempts = max(1, int(attempts))
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        print(json.dumps({"event": "startup.phase", "phase": name, "state": "started",
                          "attempt": attempt}), flush=True)
        try:
            result = operation()
        except BaseException as exc:
            retry = (retry_transient_database and attempt < attempts
                     and is_transient_database_error(exc))
            fields = _error_fields(exc)
            print(json.dumps({"event": "startup.phase", "phase": name, "state": "failed",
                              "attempt": attempt, "will_retry": retry, **fields,
                              "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}),
                  flush=True)
            if retry:
                sleep(delay_seconds)
                continue
            if on_final_failure is not None:
                try:
                    on_final_failure(name, fields)
                except BaseException:
                    # Evidence persistence must never replace the original startup failure.
                    pass
            raise
        print(json.dumps({"event": "startup.phase", "phase": name, "state": "completed",
                          "attempt": attempt,
                          "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}),
              flush=True)
        return result
