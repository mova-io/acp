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


def read(subscription, *args, lines=False):
    result = subprocess.run(['az', *args, '--subscription', subscription,
                             '--only-show-errors', '-o', 'json'],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('startup evidence unavailable')
    if lines:
        rows = []
        for line in result.stdout.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows
    return json.loads(result.stdout)


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
        if phase.get('event') != 'schema.boot':
            continue
        code = phase.get('sqlstate')
        error = phase.get('error_type')
        events.append({'time': row.get('TimeStamp'), 'reason': 'schema.boot',
                       'state': phase.get('state') if phase.get('state') in ('started', 'completed', 'failed') else None,
                       'version': phase.get('version') if isinstance(phase.get('version'), int) else None,
                       'elapsed_ms': phase.get('elapsed_ms') if isinstance(phase.get('elapsed_ms'), (int, float)) else None,
                       'sqlstate': code if isinstance(code, str) and re.fullmatch('[A-Z0-9]{5}', code) else None,
                       'error_type': error if isinstance(error, str) and re.fullmatch('[A-Za-z_]{1,60}', error) else None})
    return events


def receipt(app, replicas, system, console, image):
    properties = app['properties']
    revision = properties['latestRevisionName']
    selected = [c for c in properties['template']['containers'] if c.get('name') == app['name']]
    rows = [{'ready': c.get('ready') is True, 'started': c.get('started') is True,
             'restart_count': c.get('restartCount')} for replica in replicas
            for c in replica.get('properties', {}).get('containers', [])]
    events = sanitized_events(system, console, revision)
    ok = (properties.get('latestReadyRevisionName') == revision
          and len(selected) == 1 and selected[0]['image'] == image
          and bool(rows) and all(c['ready'] and c['started'] and c['restart_count'] == 0 for c in rows)
          and not any(e.get('reason') == 'ContainerTerminated' or e.get('liveness_restart')
                      or e.get('state') == 'failed' for e in events))
    return {'app': app['name'], 'revision': revision, 'ok': ok,
            'containers': rows, 'events': events,
            'phase_history_complete': False}  # No claim to recover deleted prior-container stderr.


def collect(subscription, group, name, image):
    deadline = time.monotonic() + 120
    while True:
        app = read(subscription, 'containerapp', 'show', '-g', group, '-n', name)
        revision = app['properties']['latestRevisionName']
        replicas = read(subscription, 'containerapp', 'replica', 'list', '-g', group,
                        '-n', name, '--revision', revision)
        if (app['properties'].get('latestReadyRevisionName') == revision
                and replicas and all(c.get('ready') for r in replicas
                                     for c in r.get('properties', {}).get('containers', []))):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('startup did not become ready within observation budget')
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    system = read(subscription, 'containerapp', 'logs', 'show', '-g', group, '-n', name,
                  '--type', 'system', '--tail', '300', '--format', 'json', lines=True)
    console = read(subscription, 'containerapp', 'logs', 'show', '-g', group, '-n', name,
                   '--revision', revision, '--tail', '100', '--format', 'json', lines=True)
    final = read(subscription, 'containerapp', 'show', '-g', group, '-n', name)
    if final['properties']['latestRevisionName'] != revision:
        raise RuntimeError('startup target revision changed during observation')
    replicas = read(subscription, 'containerapp', 'replica', 'list', '-g', group,
                    '-n', name, '--revision', revision)
    return receipt(final, replicas, system, console, image)


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
