"""Keep sanitized startup receipts and refuse crash-and-retry deployment success."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

STARTUP_PHASES = {'scheduler_reload', 'scheduler_start', 'capacity_reconcile_start',
                  'workers_start', 'worker_reporter_start'}
OBSERVATION_SECONDS = 120


class StartupFailure(RuntimeError):
    """A proved process failure that retries must never turn into success."""

    REASONS = {'container_restart', 'container_not_ready', 'system_terminated',
               'liveness_restart', 'console_phase_failed', 'durable_failure',
               'revision_changed', 'image_changed'}

    def __init__(self, reason):
        if reason not in self.REASONS:
            raise ValueError('invalid startup failure reason')
        self.reason = reason
        super().__init__(reason)


def read(subscription, *args, lines=False, deadline=None):
    """Retry Azure's eventually consistent read endpoints within a small budget."""
    for attempt in range(3):
        remaining = 30 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            break
        result = subprocess.run(['az', *args, '--subscription', subscription,
                                 '--only-show-errors', '-o', 'json'],
                                capture_output=True, text=True,
                                timeout=max(.1, min(30, remaining)))
        if not result.returncode:
            if lines:
                rows = []
                for line in result.stdout.splitlines():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
                return rows
            try:
                return json.loads(result.stdout)
            except ValueError:
                pass
        if attempt < 2:
            delay = 2 ** attempt
            if deadline is not None:
                delay = min(delay, max(0, deadline - time.monotonic()))
            if delay:
                time.sleep(delay)
    raise RuntimeError('startup evidence unavailable')


def sanitized_events(system, console, revision):
    events = []
    for row in system:
        if row.get('RevisionName') != revision:
            continue
        reason = row.get('Reason')
        if reason not in ('ContainerStarted', 'ContainerTerminated', 'ProbeFailed'):
            continue
        message = row.get('Msg', '')
        match = re.search(r"exit code '([0-9]{1,3})'", message)
        events.append({'time': row.get('TimeStamp'), 'reason': reason,
                       'exit_code': int(match[1]) if match else None,
                       'liveness_restart': 'will be restarted' in message,
                       'count': row.get('Count')})
    for row in console:
        message = row.get('Log', '')
        try:
            phase = json.JSONDecoder().raw_decode(message[message.index('{'):])[0]
        except (ValueError, TypeError):
            continue
        if phase.get('event') not in ('schema.boot', 'startup.phase'):
            continue
        code = phase.get('sqlstate')
        error = phase.get('error_type')
        reason = phase.get('event')
        events.append({'time': row.get('TimeStamp'), 'reason': reason,
                       'phase': phase.get('phase') if phase.get('phase') in STARTUP_PHASES else None,
                       'state': phase.get('state') if phase.get('state') in ('started', 'completed', 'failed') else None,
                       'version': phase.get('version') if isinstance(phase.get('version'), int) else None,
                       'elapsed_ms': phase.get('elapsed_ms') if isinstance(phase.get('elapsed_ms'), (int, float)) else None,
                       'sqlstate': code if isinstance(code, str) and re.fullmatch('[A-Z0-9]{5}', code) else None,
                       'error_type': error if isinstance(error, str) and re.fullmatch('[A-Za-z_]{1,60}', error) else None})
    return events


def durable_failure(app, subscription, group, name, revision):
    """Read an allowlisted failed-process receipt without exposing its connection string."""
    import psycopg2
    from schema_preflight import connection_string
    dsn = connection_string(app, subscription, group, name)
    conn = psycopg2.connect(dsn, connect_timeout=5,
                           options='-c default_transaction_read_only=on -c statement_timeout=5000')
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute('SELECT value FROM app_settings WHERE key=%s',
                        ('startup_failure:' + revision,))
            row = cur.fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        error = value.get('error_type')
        state = value.get('sqlstate')
        phase = value.get('phase')
        if value.get('revision') != revision or phase not in STARTUP_PHASES:
            return {'reason': 'startup.failure', 'state': 'failed',
                    'phase': None, 'error_type': 'InvalidReceipt', 'sqlstate': None}
        return {'reason': 'startup.failure', 'state': 'failed', 'phase': phase,
                'error_type': error if isinstance(error, str) and re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,59}', error) else None,
                'sqlstate': state if isinstance(state, str) and re.fullmatch('[A-Z0-9]{5}', state) else None}
    finally:
        conn.close()


def receipt(app, replicas, system, console, image, durable=None):
    properties = app['properties']
    revision = properties['latestRevisionName']
    selected = [c for c in properties['template']['containers'] if c.get('name') == app['name']]
    rows = [{'ready': c.get('ready') is True, 'started': c.get('started') is True,
             'restart_count': c.get('restartCount')} for replica in replicas
            for c in replica.get('properties', {}).get('containers', [])]
    events = sanitized_events(system, console, revision)
    if durable is not None:
        events.append(durable)
    ok = (properties.get('latestReadyRevisionName') == revision
          and len(selected) == 1 and selected[0]['image'] == image
          and bool(rows) and all(c['ready'] and c['started'] and c['restart_count'] == 0 for c in rows)
          and not any(e.get('reason') == 'ContainerTerminated' or e.get('liveness_restart')
                      or e.get('state') == 'failed' for e in events))
    return {'app': app['name'], 'revision': revision, 'ok': ok,
            'containers': rows, 'events': events,
            'phase_history_complete': False}  # No claim to recover deleted prior-container stderr.


def _system_failure(events):
    if any(event.get('reason') == 'ContainerTerminated' for event in events):
        return 'system_terminated'
    if any(event.get('liveness_restart') for event in events):
        return 'liveness_restart'
    return None


def _post_ready_containers(replicas):
    rows = [container for replica in replicas
            for container in replica.get('properties', {}).get('containers', [])]
    if not rows:
        return False
    incomplete = False
    for container in rows:
        ready = container.get('ready')
        started = container.get('started')
        restart = container.get('restartCount')
        if ready is False or started is False:
            raise StartupFailure('container_not_ready')
        if ready is not None and ready is not True:
            raise StartupFailure('container_not_ready')
        if started is not None and started is not True:
            raise StartupFailure('container_not_ready')
        if restart is not None:
            if type(restart) is not int or restart < 0 or restart > 0:
                raise StartupFailure('container_restart')
        if ready is None or started is None or restart is None:
            incomplete = True
    return not incomplete


def _logs_complete(receipt_row):
    events = receipt_row.get('events', [])
    return (any(event.get('reason') == 'ContainerStarted' for event in events)
            and any(event.get('reason') == 'schema.boot'
                    and event.get('state') == 'completed' for event in events)
            and any(event.get('reason') == 'startup.phase'
                    and event.get('state') == 'completed' for event in events))


def _revision_image(document, name):
    try:
        containers = document['properties']['template']['containers']
        selected = [container for container in containers if container.get('name') == name]
        if len(selected) != 1 or not isinstance(selected[0].get('image'), str):
            raise ValueError()
        return selected[0]['image']
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('revision image unavailable') from None


def collect(subscription, group, name, image, *, timeout=OBSERVATION_SECONDS):
    deadline = time.monotonic() + timeout
    revision = None
    while True:
        try:
            app = read(subscription, 'containerapp', 'show', '-g', group, '-n', name,
                       deadline=deadline)
            current = app['properties']['latestRevisionName']
            revision_document = read(subscription, 'containerapp', 'revision', 'show',
                                     '-g', group, '-n', name, '--revision', current,
                                     deadline=deadline)
            current_image = _revision_image(revision_document, name)
            if revision is None:
                if current_image == image:
                    revision = current
                else:
                    # The update was accepted but Azure still exposes the prior
                    # latest revision. It is not the observation target yet.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError('startup evidence unavailable within observation budget')
                    time.sleep(min(5, remaining))
                    continue
            elif current != revision:
                raise StartupFailure('revision_changed')
            elif current_image != image:
                raise StartupFailure('image_changed')
            replicas = read(subscription, 'containerapp', 'replica', 'list', '-g', group,
                            '-n', name, '--revision', revision, deadline=deadline)
            post_ready = app['properties'].get('latestReadyRevisionName') == revision
            containers_complete = _post_ready_containers(replicas) if post_ready else False
            ready = post_ready and containers_complete
            if ready:
                system = read(subscription, 'containerapp', 'logs', 'show', '-g', group,
                              '-n', name, '--type', 'system', '--tail', '300',
                              '--format', 'json', lines=True, deadline=deadline)
                system_failure = _system_failure(sanitized_events(system, [], revision))
                if system_failure:
                    raise StartupFailure(system_failure)
                console = read(subscription, 'containerapp', 'logs', 'show', '-g', group,
                               '-n', name, '--revision', revision, '--tail', '100',
                               '--format', 'json', lines=True, deadline=deadline)
                if any(event.get('state') == 'failed'
                       for event in sanitized_events([], console, revision)):
                    raise StartupFailure('console_phase_failed')
                final = read(subscription, 'containerapp', 'show', '-g', group, '-n', name,
                             deadline=deadline)
                if final['properties']['latestRevisionName'] != revision:
                    raise StartupFailure('revision_changed')
                final_revision = read(subscription, 'containerapp', 'revision', 'show',
                                      '-g', group, '-n', name, '--revision', revision,
                                      deadline=deadline)
                if _revision_image(final_revision, name) != image:
                    raise StartupFailure('image_changed')
                replicas = read(subscription, 'containerapp', 'replica', 'list', '-g', group,
                                '-n', name, '--revision', revision, deadline=deadline)
                # This revision was already observed post-ready above. A later
                # control-plane wobble cannot downgrade bad container evidence
                # back into a retryable pre-ready state.
                _post_ready_containers(replicas)
                persisted = durable_failure(final, subscription, group, name, revision)
                if persisted is not None and persisted.get('state') == 'failed':
                    raise StartupFailure('durable_failure')
                result = receipt(final, replicas, system, console, image, persisted)
                if result['ok'] and _logs_complete(result):
                    return result
        except StartupFailure:
            raise
        except Exception:
            # Azure log/control-plane reads and an absent durable receipt can
            # lag a ready revision. Retry the complete snapshot under one
            # deadline; no partial observation authorizes success.
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('startup evidence unavailable within observation budget')
        time.sleep(min(5, remaining))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--subscription', required=True)
    parser.add_argument('--group', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('apps', nargs='+')
    args = parser.parse_args(argv)
    rows = []
    with ThreadPoolExecutor(max_workers=min(5, len(args.apps))) as executor:
        futures = [(name, executor.submit(collect, args.subscription, args.group, name, args.image))
                   for name in args.apps]
        for name, future in futures:
            try:
                rows.append(future.result())
            except StartupFailure as exc:
                rows.append({'app': name, 'ok': False, 'reason': exc.reason})
            except Exception:
                rows.append({'app': name, 'ok': False, 'reason': 'startup_evidence_unavailable'})
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=output.parent, prefix='.startup-')
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump({'image': args.image, 'roles': rows}, handle, indent=2)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    ok = all(row['ok'] for row in rows)
    print(json.dumps({'event': 'deployment.startup', 'ok': ok, 'roles': len(rows)}), flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
