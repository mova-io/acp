"""Validate a complete ACA revision snapshot and count all remaining replicas."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys


def replica_count(subscription: str, group: str, app: str, revision: str) -> int:
    result = subprocess.run([
        'az', 'containerapp', 'replica', 'list', '--subscription', subscription,
        '-g', group, '-n', app, '--revision', revision, '--query', 'length(@)', '-o', 'tsv'],
        capture_output=True, text=True, timeout=30)
    if result.returncode or not result.stdout.strip().isdigit():
        raise RuntimeError('replica snapshot unavailable')
    return int(result.stdout.strip())


def bounded_candidates(rows, required, *, exclude=None, limit=6):
    all_names = [row['name'] for row in rows]
    if (len(all_names) != len(set(all_names)) or
            any(not isinstance(name, str) or not name for name in all_names)):
        raise ValueError('invalid revision inventory')
    inventory = {row['name']: row for row in rows}
    if any(name not in inventory for name in required):
        raise ValueError('selected revision absent from inventory')
    observed = []
    for row in rows:
        properties = row.get('properties', {})
        active = properties.get('active')
        replicas = properties.get('replicas')
        if not isinstance(active, bool) or not isinstance(replicas, int) or replicas < 0:
            raise ValueError('incomplete revision inventory')
        if active or replicas > 0:
            observed.append(row['name'])
    names = list(dict.fromkeys([*required, *observed]))
    if len(names) > limit:
        raise ValueError('unbounded rollout revision set')
    return [name for name in names if name != exclude]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--subscription', required=True)
    parser.add_argument('--group', required=True)
    parser.add_argument('--app', required=True)
    parser.add_argument('--revision', action='append', required=True)
    parser.add_argument('--exclude-revision')
    args = parser.parse_args(argv)
    try:
        rows = json.load(sys.stdin)
        if not isinstance(rows, list) or not rows:
            raise ValueError('revision snapshot unavailable')
        names = bounded_candidates(rows, args.revision, exclude=args.exclude_revision)
        active = sum(1 for row in rows if row['properties']['active'] is True)
        replicas = sum(replica_count(args.subscription, args.group, args.app, name)
                       for name in names)
    except Exception:
        print('false -1 -1')
        return 1
    print(f'true {active} {replicas}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
