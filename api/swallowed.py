"""Report a failure that is deliberately not re-raised.

WHY THIS EXISTS. This codebase is full of calls that must not take a scan, a remediation or a
request down with them — a timing row, a progress update, a marker, an evidence thumbnail, a
telemetry span. There were 169 of them written as `except Exception: pass`, which is not "best
effort" but "no effort observable by anyone": no log, no metric, no row.

WHAT THAT COSTS, concretely. `store.touch_job` gained required arguments in #1075. A test double
kept the old signature, and the worker heartbeat wraps its call in exactly this construct
(api/worker.py) — so a TypeError was raised and swallowed on every heartbeat of every test using
that double, for five PRs, with the suite green throughout. See
tests/test_store_doubles_match_the_real_store.py for that history, and #1108 for the first 40
conversions. A single line of output would have ended it on the first run.

WHY A HELPER RATHER THAN A BARE logger.warning AT EACH SITE. Volume. Many of these sit inside a
per-file loop over the whole estate, and some fire once per (file, criterion) — so a
systematically failing store on a 6,000-file scan would emit ~70,000 tracebacks and bury the
signal it exists to provide. Occurrences are counted per (scan_id, operation) and reported at
powers of two: the first failure is always logged, and a persistent one still escalates visibly
(1, 2, 4, 8 ...) without flooding.

WHY exc_info RATHER THAN str(e). `exc_info=True` inside an `except` block picks up the live
exception on its own, so no call site needs `as e`. That matters beyond tidiness: a nested
best-effort handler whose enclosing handler bound `as e` and still reads it afterwards would have
that name DELETED on exit — Python's `except ... as e` does an implicit `del e` — raising
NameError out of a block whose entire job is to swallow, and turning an observability change into
a control-flow one. api/handlers.py's `_scan_discover` listing path is exactly that shape.
exc_info also carries the traceback, which is what identifies a signature drift AS a drift.

THIS FUNCTION MUST NOT RAISE. Every caller is a handler that previously could not.
`logger.warning` does not raise (logging routes its own errors through Handler.handleError), and
the counter is a plain dict under a lock.

NOT EVERY SILENT HANDLER IS A BUG. A handler that NAMES its exception types has made a bounded
decision about an expected condition — a binary docx part that will not decode, an optional
attribute that will not parse, an absent optional import. Those are left alone deliberately;
logging them would be noise, and several run per row or per cell. It is the blanket
`except Exception:` that swallows everything including the failures nobody predicted, and it is
those this exists for. tests/test_no_handler_fails_silently.py draws that line.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_COUNTS: dict[tuple[str, str], int] = {}
_MAX_KEYS = 2048


def swallowed(op: str, scan_id: str | None = None, *, include_exception: bool = True) -> None:
    """Report a best-effort failure. Call from inside an `except` block, in place of `pass`.

    `op` reads "<function>: <what was being attempted> failed", matching the logger.warning
    calls already scattered through these modules. Rate-limited per (scan_id, op)."""
    key = (scan_id or "-", op)
    with _LOCK:
        # A long-lived worker accumulates one key per (scan, site). Dropping the whole table when
        # it grows past a few dozen scans re-reports some failures at their first occurrence
        # again, which is the harmless direction to be wrong in.
        if len(_COUNTS) >= _MAX_KEYS:
            _COUNTS.clear()
        n = _COUNTS.get(key, 0) + 1
        _COUNTS[key] = n
    if n & (n - 1):          # not a power of two — already reported at a lower count
        return
    try:
        logger.warning("%s for %s%s", op, scan_id or "-",
                       "" if n == 1 else f" (occurrence {n}; reported at powers of two)",
                       exc_info=include_exception)
    except Exception:
        # Observability is always secondary to the caller's control flow. A
        # broken custom logging handler must not turn best-effort reporting
        # into the failure the caller deliberately contained.
        return


def reset() -> None:
    """Forget every occurrence count. For tests, so one does not leak into the next."""
    with _LOCK:
        _COUNTS.clear()
