"""Compare the actual build pin with live stamped provenance before staging writes."""
import argparse
import json
import re
import subprocess
import sys


def decision(repo, pin, health, mode, rollback=False):
    if mode not in ('automatic', 'manual'):
        raise ValueError('unknown staging deployment intent')
    if rollback and mode != 'manual':
        raise ValueError('automatic deployment cannot authorize rollback')
    if rollback:
        return 'deploy'  # Explicit recovery also works when the live app is unavailable.
    live = health.get('commit', '')
    if health.get('version_stamped') is not True or not isinstance(live, str) or not re.fullmatch('[0-9a-f]{40}', live):
        raise ValueError('live staging commit is unverifiable; explicit manual rollback/recovery required')
    def git(*args):
        return subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)
    for sha in (pin, live):
        if not re.fullmatch('[0-9a-f]{40}', sha) or git('cat-file', '-e', sha + '^{commit}').returncode:
            raise ValueError('deployment ancestry is unavailable')
    if pin == live:
        return 'deploy'  # Repairing this same build is allowed.
    if git('merge-base', '--is-ancestor', pin, live).returncode == 0:
        if mode == 'automatic':
            return 'skip'
        raise ValueError('manual rollback requires explicit rollback/recovery intent')
    if git('merge-base', '--is-ancestor', live, pin).returncode == 0:
        return 'deploy'
    raise ValueError('deployment commits diverge; explicit manual rollback/recovery required')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--repo', required=True)
    p.add_argument('--pin', required=True)
    p.add_argument('--mode', required=True)
    p.add_argument('--rollback', action='store_true')
    a = p.parse_args()
    try:
        try:
            health = json.load(sys.stdin)
        except (ValueError, TypeError):
            health = {}
        print(decision(a.repo, a.pin, health, a.mode, a.rollback))
    except ValueError as exc:
        sys.exit(str(exc))
