"""API process boundary with sanitized, durable earliest-failure evidence."""
from __future__ import annotations

import json
import logging
import os
import re
import sys


def _fields(exc):
    error = type(exc).__name__
    state = getattr(exc, "pgcode", None)
    return {
        "error_type": error if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,59}", error)
        else "Exception",
        "sqlstate": state if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state)
        else None,
    }


def record_failure(exc):
    revision = (os.environ.get("CONTAINER_APP_REVISION") or "unknown").strip()
    receipt = {"revision": revision, "phase": "process_startup", **_fields(exc)}
    print(json.dumps({"event": "startup.process", "state": "failed", **_fields(exc)}),
          file=sys.stderr, flush=True)
    dsn = os.environ.get("DATABASE_URL")
    if not dsn or revision == "unknown":
        return
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=3,
                                options="-c statement_timeout=3000")
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("INSERT INTO app_settings(key,value) VALUES(%s,%s) "
                            "ON CONFLICT(key) DO NOTHING",
                            ("startup_failure:" + revision, json.dumps(receipt)))
        finally:
            conn.close()
    except Exception:
        # The process is already failing. Evidence persistence cannot replace
        # its exit status or print a connection error that may contain a DSN.
        return


class _UvicornFailureHandler(logging.Handler):
    def emit(self, record):
        try:
            if record.exc_info and record.exc_info[1] is not None:
                record_failure(record.exc_info[1])
        except Exception:
            return


def main():
    import uvicorn
    logging.getLogger("uvicorn.error").addHandler(_UvicornFailureHandler())
    try:
        uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8077")))
    except BaseException as exc:
        record_failure(exc)
        if isinstance(exc, SystemExit):
            return exc.code if isinstance(exc.code, int) else 1
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
