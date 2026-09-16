"""Opt-in bounded readiness timing. Never retain payloads, SQL or error content."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import os
import threading
import time
import uuid

LOGGER = logging.getLogger('acp.readiness.phases')
LOGGER.setLevel(logging.INFO)
PHASES = frozenset({'request_dispatch', 'worker_heartbeat', 'queue_summary', 'queue_claimable',
    'role_heartbeats', 'pdf_engine', 'redis', 'vision_available', 'vision_reason', 'sources_config',
    'pdf_renderer', 'langfuse', 'db_ping', 'db_admission', 'db_pool_checkout', 'db_query',
    'db_commit', 'db_return'})
MAX_ACTIVE = 4
MAX_EVENTS = 128
MAX_PENDING = 12
MAX_SECONDS = 120.0
MAX_PROCESS_EVENTS = 256
PENDING_INTERVAL = 2.0
CURRENT = ContextVar('readiness_diagnostic', default=None)
LOCK = threading.Lock()
ACTIVE = {}
REPORTER_STARTED = False
RATE_WINDOW = 0.0
RATE_COUNT = 0
DIAGNOSTIC_FAILURES = 0


def enabled():
    return os.environ.get('ACP_READINESS_PHASE_DIAGNOSTICS') == '1'


def _record_failure():
    global DIAGNOSTIC_FAILURES
    with LOCK:
        DIAGNOSTIC_FAILURES = min(9999, DIAGNOSTIC_FAILURES + 1)


def _emit(record, phase, event, elapsed, now):
    global RATE_WINDOW, RATE_COUNT
    try:
        with LOCK:
            if now - RATE_WINDOW >= 60:
                RATE_WINDOW, RATE_COUNT = now, 0
            if record['events'] >= MAX_EVENTS or RATE_COUNT >= MAX_PROCESS_EVENTS:
                return
            record['events'] += 1
            RATE_COUNT += 1
        # No caller-provided text, error objects, route parameters or dependency URLs.
        LOGGER.info(json.dumps({'event': 'readiness.phase', 'kind': record['kind'],
            'request_id': record['id'], 'phase': phase, 'state': event,
            'diagnostic_failures': DIAGNOSTIC_FAILURES,
            'elapsed_ms': round(max(0, elapsed) * 1000, 3),
            'request_elapsed_ms': round(max(0, now - record['started']) * 1000, 3)}, sort_keys=True))
    except Exception:
        # Optional observability must never change readiness or exception behavior.
        _record_failure()


def _poll(now=None):
    now = time.monotonic() if now is None else now
    events = []
    with LOCK:
        for identity, record in list(ACTIVE.items()):
            if now - record['started'] >= MAX_SECONDS:
                record['closed'] = True
                ACTIVE.pop(identity, None)
                events.append((record, record['phase'], 'tracking_expired', now - record['phase_started']))
            elif (record['pending'] < MAX_PENDING and now - record['phase_started'] >= PENDING_INTERVAL
                    and now - record['last_pending'] >= PENDING_INTERVAL):
                record['pending'] += 1
                record['last_pending'] = now
                events.append((record, record['phase'], 'pending', now - record['phase_started']))
    for record, name, event, elapsed in events:
        _emit(record, name, event, elapsed, now)


def _start_reporter():
    global REPORTER_STARTED
    with LOCK:
        if REPORTER_STARTED:
            return
        REPORTER_STARTED = True
    def report():
        while True:
            time.sleep(1)
            try:
                _poll()
            except Exception:
                _record_failure()
    try:
        threading.Thread(target=report, daemon=True, name='readiness-phase-reporter').start()
    except Exception:
        _record_failure()
        with LOCK:
            REPORTER_STARTED = False


@contextmanager
def request(kind):
    if not enabled() or kind not in {'full', 'container'}:
        yield
        return
    now = time.monotonic()
    record = {'id': uuid.uuid4().hex[:12], 'kind': kind, 'started': now,
        'phase': 'request_dispatch', 'phase_started': now, 'events': 0, 'pending': 0,
        'last_pending': now, 'closed': False}
    with LOCK:
        admitted = len(ACTIVE) < MAX_ACTIVE
        if admitted:
            ACTIVE[record['id']] = record
    # Overflow changes only diagnostics; never delay or reject the real request.
    token = CURRENT.set(record if admitted else None)
    state = 'completed'
    try:
        if admitted:
            _start_reporter()
        yield
    except BaseException:
        state = 'interrupted'
        raise
    finally:
        CURRENT.reset(token)
        now = time.monotonic()
        with LOCK:
            tracked = ACTIVE.pop(record['id'], None)
            record['closed'] = True
        if tracked:
            _emit(record, record['phase'], state, now - record['phase_started'], now)


@contextmanager
def phase(name):
    record = CURRENT.get()
    if record is None or record['closed'] or name not in PHASES:
        yield
        return
    now = time.monotonic()
    with LOCK:
        previous = (record['phase'], record['phase_started'])
        record['phase'], record['phase_started'] = name, now
    state = 'completed'
    try:
        yield
    except BaseException:
        state = 'interrupted'
        raise
    finally:
        ended = time.monotonic()
        with LOCK:
            live = not record['closed']
            if live:
                record['phase'], record['phase_started'] = previous
        if live:
            _emit(record, name, state, ended - now, ended)


def run(name, callback, *args, **kwargs):
    with phase(name):
        return callback(*args, **kwargs)


class ReadinessPhaseMiddleware:
    """Capture dispatch wait before a synchronous readiness handler can get a slot."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = {'/readyz': 'full', '/probe/readyz': 'container'}.get(scope.get('path'))
        if scope.get('type') != 'http' or kind is None or not enabled():
            return await self.app(scope, receive, send)
        with request(kind):
            return await self.app(scope, receive, send)
