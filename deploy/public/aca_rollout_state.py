"""Parse the exact ACA rollout fields without depending on CLI table layout."""
from __future__ import annotations

import json
import sys


def parse(value) -> tuple[str, int, str]:
    if not isinstance(value, dict) or set(value) != {'provisioning', 'min_replicas', 'revision'}:
        raise ValueError('invalid rollout state shape')
    provisioning = value['provisioning']
    minimum = value['min_replicas']
    revision = value['revision']
    if (not isinstance(provisioning, str) or not provisioning or
            not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0 or
            not isinstance(revision, str) or not revision):
        raise ValueError('invalid rollout state values')
    return provisioning, minimum, revision


def main() -> int:
    try:
        provisioning, minimum, revision = parse(json.load(sys.stdin))
    except (ValueError, TypeError, json.JSONDecodeError):
        return 1
    print(json.dumps([provisioning, minimum, revision], separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
