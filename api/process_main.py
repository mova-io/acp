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


def _record_fields(fields):
    revision = (os.environ.get("CONTAINER_APP_REVISION") or "unknown").strip()
    receipt = {"revision": revision, "phase": "process_startup", **fields}
    print(json.dumps({"event": "startup.process", "state": "failed", **fields}),
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


def record_failure(exc):
    _record_fields(_fields(exc))


class _UvicornFailureFilter(logging.Filter):
    recorded = False

    def filter(self, record):
        try:
            if record.exc_info and record.exc_info[1] is not None:
                record_failure(record.exc_info[1])
                self.recorded = True
                # Uvicorn would otherwise format the original exception and
                # may expose a DSN or credential. Keep the operational signal
                # while removing exception text from the centralized channel.
                record.msg = "Application startup exception captured; details redacted"
                record.args = ()
                record.exc_info = None
                record.exc_text = None
            else:
                # Starlette sends lifespan failures to Uvicorn as a traceback
                # string inside the ASGI message, so no exc_info survives.
                # Extract only the final exception class and redact the entire
                # traceback before Uvicorn's handler formats it.
                message = record.getMessage()
                if "Traceback (most recent call last)" in message:
                    match = re.search(r"\n([A-Za-z_][A-Za-z0-9_]{0,59})(?::[^\n]*)?\s*$",
                                      message)
                    _record_fields({"error_type": match.group(1) if match else "Exception",
                                    "sqlstate": None})
                    self.recorded = True
                    record.msg = "Application startup exception captured; details redacted"
                    record.args = ()
        except Exception:
            # Logging filters must never break Uvicorn's own control flow.
            return True
        return True


def main():
    import uvicorn
    config = uvicorn.Config("app:app", host="0.0.0.0",
                            port=int(os.environ.get("PORT", "8077")))
    failure_filter = _UvicornFailureFilter()
    # Config construction applies Uvicorn's dictConfig. Install after that so
    # the handler cannot be removed before import/lifespan startup.
    logging.getLogger("uvicorn.error").addFilter(failure_filter)
    try:
        config.load_app()
        server = uvicorn.Server(config=config)
        server.run()
    except BaseException as exc:
        if not failure_filter.recorded:
            record_failure(exc)
        if isinstance(exc, SystemExit):
            return exc.code if isinstance(exc.code, int) else 1
        return 3
    return 0 if server.started else 3


if __name__ == "__main__":
    raise SystemExit(main())
