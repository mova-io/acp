"""Fail-closed PostgreSQL connection budgets for a Container Apps rollout."""
from __future__ import annotations

import argparse
import json


def plan(*, maximum: int, reserved: int, external: int,
         current: dict[str, int], target: dict[str, int]) -> dict:
    values = [maximum, reserved, external, *current.values(), *target.values()]
    if any(not isinstance(value, int) or value < 0 for value in values):
        raise ValueError("connection budgets must be non-negative integers")
    usable = maximum - reserved
    if usable <= 0 or not target or set(current) != set(target):
        raise ValueError("invalid server capacity or role inventory")
    target_steady = sum(target.values())
    largest_target = max(target.values())
    sequential_peak = target_steady + largest_target + external
    current_rolling_peak = sum(current.values()) + largest_target + external
    if sequential_peak > usable:
        raise ValueError(
            f"target rollout needs {sequential_peak} ordinary connections; server has {usable}")
    return {
        "usable": usable,
        "target_steady": target_steady,
        "largest_cohort": largest_target,
        "external": external,
        "sequential_peak": sequential_peak,
        "current_rolling_peak": current_rolling_peak,
        "bootstrap_required": current_rolling_peak > usable,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--maximum", type=int, required=True)
    parser.add_argument("--reserved", type=int, required=True)
    parser.add_argument("--external", type=int, required=True)
    parser.add_argument("--current", type=json.loads, required=True)
    parser.add_argument("--target", type=json.loads, required=True)
    args = parser.parse_args(argv)
    try:
        result = plan(maximum=args.maximum, reserved=args.reserved,
                      external=args.external, current=args.current, target=args.target)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
