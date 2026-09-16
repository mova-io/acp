"""Read a strict readyz queue activity count; missing evidence is an error."""
from __future__ import annotations

import json
import sys


def parse(payload) -> int:
    active = payload['queue']['active']
    if not isinstance(active, int) or isinstance(active, bool) or active < 0:
        raise ValueError('queue.active must be a non-negative integer')
    return active


def main() -> int:
    try:
        print(parse(json.load(sys.stdin)))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
